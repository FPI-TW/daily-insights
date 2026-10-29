# 重點新聞維運手冊

本手冊處理重點新聞（newsroom）管線的日常檢查、失敗恢復、Slack 通知處置與版次發布
問題。管線架構見[重點新聞架構](../architecture/daily-news.md)。

## 對讀者的承諾

- 讀者端只顯示已發布版次中完成分析的項目。當日版次尚未發布或沒有可見項目時，
  `GET /api/newsroom/editions/latest` 回傳最近一個有可見項目的版次，`is_today = false`，
  頁面明確標示日期。
- 英文頁只顯示已翻譯且翻譯內容與目前繁中一致的項目；翻譯落後時該則暫不顯示，不以
  繁中替代。
- 管線故障不會讓已發布的內容消失；恢復作業也不需要清空或重建既有版次。

## 入口與工具

| 用途           | 位置                                                                                          |
| -------------- | --------------------------------------------------------------------------------------------- |
| 審核與編修     | `/admin/newsroom`（「重點新聞審核」），可用 `?date=YYYY-MM-DD` 指定版次日期                   |
| 來源健康與設定 | `/admin/newsroom/sources`（「新聞來源管理」）                                                 |
| 08:00 組稿紀錄 | orchestration 後台的 `newsroom_daily_assemble` JobRun 與 `newsroom_assemble` function attempt |
| 管理員動作紀錄 | `newsroom_edit_log`                                                                           |
| LLM 呼叫稽核   | `newsroom_llm_calls`（stage、model、錯誤碼、request id、延遲，不含 prompt 與正文）            |

容器與日誌：

```sh
# 本機
docker compose ps newsroom-worker orchestration-worker
docker compose logs --tail=200 newsroom-worker

# 正式環境（EC2）
docker inspect --format '{{.State.Status}} {{.State.Health.Status}}' daily-insights-newsroom-worker
docker logs --tail=200 daily-insights-newsroom-worker
```

`newsroom-worker` 每次迴圈都更新 `/tmp/newsroom-worker-heartbeat`，超過 90 秒未更新即
unhealthy。log 中的 `newsroom worker idle: DAILY_INSIGHTS_NEWSROOM_ENABLED is false`
代表管線停用中，屬正常狀態。`newsroom.stage` 記錄每次處理的 stage、資料列與結果
（`done`、`pending`、`failed`、`fatal`、`fenced`）；`newsroom.poll_failed`、
`newsroom.fetch_unavailable`、`newsroom.editor_failed` 分別對應來源、全文與精選問題。

SQL 範例使用 PostgreSQL。本機以 `docker compose exec postgres psql -U daily_insights -d daily_insights`
執行；正式環境以具權限的 client 連線 RDS，先執行唯讀查詢確認範圍，再執行更新。

## 佇列狀態語意

每個 stage 在資料列上有 `<stage>_status`、`<stage>_attempts`、`<stage>_next_attempt_at`
、`<stage>_error_code` 與 `<stage>_claim_token`：

`claim_token` 是每次領取獨立產生的 UUID，與可重設的重試次數分開。重新排入或暫停工作時
會清除 token；舊 worker 回傳 `fenced` 時，其產品資料修改會整筆回滾，LLM 呼叫紀錄仍保留。
過期工作的錯誤不覆寫新工作，也不發送失敗／額度通知。優先使用下方的復原指令，避免只修改 attempts。

| 資料表                   | Stage（欄位前綴）                            |
| ------------------------ | -------------------------------------------- |
| `newsroom_articles`      | `fetch`、`embed`、`triage`                   |
| `newsroom_events`        | `analysis`、`en`（英文翻譯）                 |
| `newsroom_edition_items` | `why`（另有 `why_en_status` 只反映翻譯結果） |

| 狀態                                          | 意義                                                                                                              |
| --------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `idle`                                        | 尚未輪到（例如文章還沒 embedding、事件尚未入選）                                                                  |
| `pending`，`next_attempt_at` 為 NULL 或已到期 | 等待 worker 領取                                                                                                  |
| `pending`，`next_attempt_at` 在未來           | 正在處理（10 分鐘租約）或在退避等待下一次嘗試；`error_code` 有值代表上一次失敗                                    |
| `done`／`ready`                               | 完成                                                                                                              |
| `needs_body`（只有 `analysis`）               | 事件沒有任何可用全文，等管理員貼上全文；不是失敗                                                                  |
| `failed`                                      | 可重試錯誤已用完 `max_attempts`（fetch 4、embed 6、triage 6、analysis 5、why 5、translate 6），或遇到不可重試錯誤 |

- **可重試**：逾時、連線錯誤、429、5xx、模型回傳 JSON 或 schema 不合法、未預期例外。
  依 1、2、4、8… 分鐘退避（上限 60 分鐘，`Retry-After` 較長時最多 6 小時），用完次數才
  轉 `failed`。這類錯誤通常不需要人工介入。
- **不可重試**：provider 回 400／401／402／403／404，或 key 未設定
  （`newsroom_llm_configuration_missing`、`newsroom_embedding_configuration_missing`）。
  資料列直接 `failed` 並發出 `stage_fatal` 通知。
- `failed` 永遠不會自動恢復，必須修正原因後人工重新排入。
- 全文抓取的「抓不到」不是失敗：付費牆、403、404、robots 禁止、非 HTML 等會得到
  `body_status = unavailable`（或品質不合格的 `rejected`），而 `fetch_status = done`。

## 查詢佇列狀態

以下以版次日期 `2026-10-02` 為例。

文章三個 stage 的整體分布：

```sql
SELECT stage, status, count(*) AS rows,
       count(*) FILTER (WHERE next_at > now()) AS leased_or_backing_off
FROM (
  SELECT 'fetch' AS stage, fetch_status AS status, fetch_next_attempt_at AS next_at
  FROM newsroom_articles WHERE edition_date = DATE '2026-10-02'
  UNION ALL
  SELECT 'embed', embed_status, embed_next_attempt_at
  FROM newsroom_articles WHERE edition_date = DATE '2026-10-02'
  UNION ALL
  SELECT 'triage', triage_status, triage_next_attempt_at
  FROM newsroom_articles WHERE edition_date = DATE '2026-10-02'
) AS queue
GROUP BY stage, status
ORDER BY stage, status;
```

各 stage 的錯誤碼（含仍在退避中的錯誤）：

```sql
SELECT 'fetch' AS stage, fetch_status AS status, fetch_error_code AS error_code, count(*)
FROM newsroom_articles
WHERE edition_date = DATE '2026-10-02' AND fetch_error_code IS NOT NULL
GROUP BY 1, 2, 3
UNION ALL
SELECT 'embed', embed_status, embed_error_code, count(*)
FROM newsroom_articles
WHERE edition_date = DATE '2026-10-02' AND embed_error_code IS NOT NULL
GROUP BY 1, 2, 3
UNION ALL
SELECT 'triage', triage_status, triage_error_code, count(*)
FROM newsroom_articles
WHERE edition_date = DATE '2026-10-02' AND triage_error_code IS NOT NULL
GROUP BY 1, 2, 3
ORDER BY 1, 4 DESC;
```

全文結果分布（判斷是否大量 `unavailable`／`rejected`）：

```sql
SELECT s.key, a.body_status, a.body_quality_reason, count(*)
FROM newsroom_articles AS a
JOIN newsroom_sources AS s ON s.id = a.source_id
WHERE a.edition_date = DATE '2026-10-02'
GROUP BY 1, 2, 3
ORDER BY 4 DESC;
```

版次中的事件分析、翻譯與項目「為何重要」：

```sql
SELECT ed.market_code, ed.status AS edition_status, i.rank, ev.working_title,
       ev.analysis_status, ev.analysis_attempts, ev.analysis_error_code,
       i.why_status, i.why_error_code, ev.en_status, ev.en_error_code,
       i.removed_at IS NOT NULL AS removed, i.hidden_at IS NOT NULL AS hidden,
       i.abandoned_at IS NOT NULL AS abandoned
FROM newsroom_edition_items AS i
JOIN newsroom_editions AS ed ON ed.id = i.edition_id
JOIN newsroom_events AS ev ON ev.id = i.event_id
WHERE ed.edition_date = DATE '2026-10-02'
ORDER BY ed.market_code, i.rank;
```

最近一小時的 LLM／embedding 錯誤：

```sql
SELECT stage, model, error_code, count(*), max(created_at) AS last_seen
FROM newsroom_llm_calls
WHERE created_at >= now() - interval '1 hour' AND error_code IS NOT NULL
GROUP BY 1, 2, 3
ORDER BY 4 DESC;
```

## 重新排入

原則：

1. **先修正原因**（金鑰、額度、model 名稱、來源網址、封鎖網域），再重新排入；否則
   資料列只會再次 `failed`。
2. **只重新排入 `failed` 的資料列**，或刻意要重跑的 `done`／`ready`。不要改動
   `pending` 且 `next_attempt_at` 在未來的資料列：它可能正在處理，重設 `attempts` 會讓
   進行中的結果被 fence 掉並重複呼叫 LLM。
3. 能用後台動作就用後台動作：後台會寫 `newsroom_edit_log`，並同時處理相依的資料列。
4. 批次重新排入使用[復原工具](#復原工具)，它先列出影響範圍，確認後才寫入；工具無法
   使用時才以 [SQL](#sql-備援) 處理。兩者的重設相同：`status = 'pending'`、
   `attempts = 0`、`next_attempt_at = NULL`、`error_code = NULL`。worker 會在下一次輪詢
   （預設 2 秒）領取，不需要重啟。

| 狀況                                       | 優先做法                                                                                                                           |
| ------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------- |
| 事件「分析失敗」或項目「『為何重要』失敗」 | 審核頁該則的「重新分析」：事件 `analysis` 與其所有未移除、未隱藏、未放棄項目的 `why` 重新排入；大量時用 `requeue --stage analysis` |
| 「缺全文」（`needs_body`）                 | 原文對照中對某篇文章「貼上全文」，事件自動重新排入分析                                                                             |
| 文章 `fetch_status = failed`               | `requeue --stage fetch`；全文取得後若事件仍是 `needs_body` 或要用新全文重寫，再按「重新分析」                                      |
| 文章 `embed_status = failed`               | `requeue --stage embed`；成功後自動排入初篩                                                                                        |
| 文章 `triage_status = failed`              | `requeue --stage triage`；沒有 embedding 或 embedding 來自未翻譯標題的文章會自動改排 `embed`                                       |
| 外文標題因翻譯失敗以原文 embedding         | `reembed-untranslated`（只處理尚未完成初篩的文章）                                                                                 |
| 事件 `en_status = failed`                  | 改任何繁中文字會自動重新排入；否則 `requeue --stage en`                                                                            |
| 事件分到錯的群                             | 後台「合併其他事件到此」或「拆分選取的文章」，受影響事件自動重新分析；不要用 SQL 改 `event_id`                                     |

已經在佇列中的 provider 設定錯誤：只改 GitHub Secret 或 `apps/api/.env` 不會影響執行中
的容器；正式環境需重新部署（本機需重新建立容器）讓新值生效，之後再重新排入。

### 復原工具

`run_newsroom_recover` 在 `newsroom-worker` 的環境執行（讀取同一個資料庫與 embedding
設定）。**預設只列出會影響的筆數（dry run，唯讀交易），加 `--apply` 才寫入。**

```sh
# 本機（CMD 為子指令，ARGS 為其參數，APPLY=1 才寫入）
make newsroom-recover CMD=requeue ARGS='--stage triage --error-code triage_provider_http_402 --from 2026-10-25'
make newsroom-recover CMD=requeue ARGS='--stage triage --error-code triage_provider_http_402 --from 2026-10-25' APPLY=1

# 正式環境（EC2）
docker exec daily-insights-newsroom-worker python -m daily_insights_api.scripts.run_newsroom_recover \
  requeue --stage triage --error-code triage_provider_http_402 --from 2026-10-25
```

| 子指令                 | 參數                                                                                              | 作用                                                                                                                                                                                                          |
| ---------------------- | ------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `requeue`              | `--stage {fetch,embed,triage,analysis,why,en}`、`--error-code <code>`（可重複）、`--from`、`--to` | 重新排入該 stage 中錯誤碼相符的 `failed` 資料列；日期為版次日期（含頭尾），省略則不限                                                                                                                         |
| `reembed-untranslated` | `--from`、`--to`                                                                                  | 找出 embed 前翻譯因 provider 錯誤被略過、以原文 embedding 的文章（`newsroom_llm_calls` 中 `stage = 'embed'`、非 embedding model、`embed_provider_*`，且之後沒有成功的翻譯），重新排入 `embed`，成功後自動初篩 |
| `reopen`               | `--edition-date`、`--market`、`--force`、`--extend-late-fill`、`--actor`                          | 把已發布的版次退回 draft 等待重新組稿，見[重開已發布的版次](#重開已發布的版次)                                                                                                                                |

輸出為 JSON：`groups` 依版次日期列出每組資料列的去向（`queue`）、原因（`reason`）與
筆數，`queued` 為會重新排入的總數，`queue` 為 `null` 的組只列出、不會改動：

| `reason`                 | 意義                                                                                              |
| ------------------------ | ------------------------------------------------------------------------------------------------- |
| `failed`                 | 重新排入原 stage                                                                                  |
| `embedding_missing`      | 初篩失敗但沒有 embedding：改排 `embed`，初篩暫停（`idle`）到新 embedding 完成                     |
| `untranslated_embedding` | embedding 來自未翻譯的外文標題：改排 `embed`（重新翻譯後 embedding），初篩暫停到新 embedding 完成 |
| `embed_in_flight`        | 要改排 `embed` 但該文章的 `embed` 正在處理或退避中，不動                                          |
| `triage_in_flight`       | （`reembed-untranslated`）初篩正在處理或退避中，不動；結束後若仍需要再執行一次                    |
| `already_triaged`        | （`reembed-untranslated`）已完成初篩、已歸入事件，不重新 embedding                                |
| `event_merged`           | 事件已合併到其他事件，不重新分析或翻譯                                                            |
| `item_not_live`          | 項目已移除、隱藏或放棄，不重新排入 `why`                                                          |
| `event_not_ready`        | 事件尚未分析完成；`why` 會隨分析一起寫入，先重新排入 `analysis`                                   |

工具不寫 `newsroom_edit_log`（`reopen` 加 `--actor` 時例外），請把 `--apply` 的輸出貼在
處置紀錄中。

### 供應商停擺後的復原順序

LLM 或 embedding 供應商停擺（例如 2026-10-24 起的 `HTTP 402`）時，`embed` 前的翻譯、
初篩、精選、分析與翻譯全部失敗：文章停在 `triage_status = failed`，外文文章以原文
embedding，09:00 的版次沒有任何項目而被[暫停自動發布](#版次發布與晚到補上)。這個機制
加入前，2026-10-25 至 10-27 的空版次都已照常發布，需要重開。修復依下列順序，每一步都
先 dry run 確認範圍：

1. **修正金鑰或改用其他供應商**並重新部署 `newsroom-worker` 與 `orchestration-worker`
   （見「切換 LLM 供應商」）。以最近的 `newsroom_llm_calls` 確認新呼叫已成功。
2. **重新 embedding 未翻譯的文章**：`reembed-untranslated --from <第一天>`。先做這步，
   避免初篩用原文向量歸群。
3. **重新排入 embed 與初篩**：`requeue --stage embed --error-code embed_provider_http_402`
   （若有），再 `requeue --stage triage --error-code triage_provider_http_402`。等初篩
   完成（[查詢佇列狀態](#查詢佇列狀態)中 `triage` 沒有 `pending`）。
4. **重開空的已發布版次**（沒有項目，或項目全被晚到補上放棄）：
   `reopen --edition-date <日期> --extend-late-fill <HH:MM>`，每個受影響日期各一次。
   仍是 draft（被暫停）的版次不需要重開。
5. **重新組稿**：對每個重開或被暫停的日期執行[重新組稿](#重新組稿)。組稿會重新排入
   失敗的事件分析。
6. **確認發布**：下一輪 sweep（一分鐘內）發布有項目的市場；仍沒有項目的市場會發
   `版次沒有任何項目 暫停自動發布`。分析在新的晚到補上期限前完成的項目即對讀者可見。
7. **其他失敗**：仍在已發布版次中的事件以 `requeue --stage analysis`、`--stage why`、
   `--stage en` 處理對應錯誤碼。

### SQL 備援

復原工具無法使用時，以 SQL 重新排入。重新排入某版次所有 `failed` 的文章 stage（以
`embed` 為例，`fetch`、`triage` 只需替換欄位前綴）：

```sql
UPDATE newsroom_articles
SET embed_status = 'pending', embed_attempts = 0,
    embed_claim_token = NULL, embed_next_attempt_at = NULL, embed_error_code = NULL
WHERE edition_date = DATE '2026-10-02' AND embed_status = 'failed';
```

初篩失敗且沒有 embedding 的文章改排 `embed`，初篩暫停到新 embedding 完成：

```sql
UPDATE newsroom_articles
SET embed_status = 'pending', embed_attempts = 0,
    embed_claim_token = NULL, embed_next_attempt_at = NULL, embed_error_code = NULL,
    triage_status = 'idle', triage_attempts = 0,
    triage_claim_token = NULL, triage_next_attempt_at = NULL, triage_error_code = NULL
WHERE edition_date = DATE '2026-10-02' AND triage_status = 'failed' AND embedding IS NULL;
```

修正金鑰後，重新排入同一錯誤碼造成的事件分析失敗：

```sql
UPDATE newsroom_events
SET analysis_status = 'pending', analysis_attempts = 0,
    analysis_claim_token = NULL, analysis_next_attempt_at = NULL, analysis_error_code = NULL
WHERE analysis_status = 'failed'
  AND analysis_error_code = 'analysis_provider_http_402'
  AND edition_date >= DATE '2026-10-01';
```

以 SQL 重新排入 `analysis` 時，項目的「為何重要」會隨分析一起寫入，不需另外排入。
單獨重新排入項目 `why` 只適用於事件已是 `ready` 的情況（`why` stage 只領取已分析事件
的項目）。翻譯以事件為單位：

```sql
UPDATE newsroom_events
SET en_status = 'pending', en_attempts = 0, en_claim_token = NULL,
    en_next_attempt_at = NULL, en_error_code = NULL
WHERE id = '<event uuid>' AND en_status = 'failed';
```

### 重新組稿

08:00 組稿失敗時，統一 orchestration 每 30 分鐘重試到 10:00；精選失敗則已自動退回
粗分（`fallback`）。需要在修正後重新組稿某日草稿時：

- 本機：`make assemble-newsroom EDITION_DATE=2026-10-02`（省略時為當日）。
- 正式環境：在已具備組稿設定的 `orchestration-worker` 容器內執行同一個 script：
  `docker exec daily-insights-orchestration-worker python -m daily_insights_api.scripts.run_newsroom_assemble`，
  日期參數與 `make assemble-newsroom` 傳入的相同。

組稿是冪等的：只重建仍為 `draft` 的市場，保留管理員加入與移除的項目；已發布的
市場不會被改動（要重建已發布的版次，先[重開](#重開已發布的版次)）。重新組稿會再發一次
「草稿完成」通知，並重新排入入選事件中失敗的分析。

## Slack 通知與處置

通知由 `DAILY_INSIGHTS_NEWSROOM_SLACK_WEBHOOK_URL` 送出（未設定時只在 log 出現
`newsroom.notice`）。標題以粗體顯示，帶連結的訊息最後一行為「開啟後台」。

| 通知標題（範例）                                           | 內容                                                                                                                                                                         | 處置                                                                                                                                                                                                                                                                                                                         |
| ---------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `2026-10-02 重點新聞草稿完成`                              | 各市場則數（精選失敗者附「精選失敗 改用粗分排序」）、5 星事件標題、「組稿時仍有 N 篇文章未完成初篩 已忽略」                                                                  | 09:00 前開啟後台審核、改字、核准。有大量未初篩文章時檢查 `embed`／`triage` 佇列與 LLM 錯誤；修正後可[重新組稿](#重新組稿)。某市場 0 則時，09:00 不會自動發布該市場。                                                                                                                                                         |
| `2026-10-02 tw_equity 精選失敗 已改用粗分排序`             | `入選 N 則 星等留空`                                                                                                                                                         | 以 `newsroom_llm_calls` 中 `stage = 'editor'` 的錯誤碼判斷原因。草稿仍可用：可直接人工調整項目後核准，或修正後重新組稿取得星等。                                                                                                                                                                                             |
| `2026-10-02 us_equity 有 N 則新聞在 12:00 前未完成 已放棄` | 被放棄項目的事件標題                                                                                                                                                         | 查詢該版次項目的 `analysis_error_code`／`why_error_code`。該則已不對讀者顯示；若修正後仍要上架，見[恢復被放棄的項目](#恢復被放棄的項目)；整個版次都被放棄時改用[重開已發布的版次](#重開已發布的版次)。                                                                                                                       |
| `2026-10-25 版次沒有任何項目 暫停自動發布`                 | `市場 global, tw_equity, us_equity`、該日文章的初篩結果（例如 `收稿 512 篇 初篩失敗 498 篇 (triage_provider_http_402 498)`，或「收稿時段沒有任何文章」）、可能原因與處置提示 | 版次維持 `draft`，讀者端繼續顯示上一個版次。依初篩錯誤碼修正原因，照[供應商停擺後的復原順序](#供應商停擺後的復原順序)重新排入並[重新組稿](#重新組稿)；有項目後下一輪自動發布。確定當天沒有新聞時在後台核准空版次。同一版次只通知一次（`held_empty_at`），重開後重新計算。                                                    |
| `新聞來源「<名稱>」持續抓取失敗`                           | 來源代碼、連續失敗次數與最後錯誤碼、最後成功時間（台北時間，或「從未成功」）                                                                                                 | 開啟「新聞來源管理」確認來源。依[來源錯誤碼](#來源健康)修正網址、格式或連結規則，或暫時停用來源。同一次故障只通知一次，來源成功一次後重置。                                                                                                                                                                                  |
| `新聞管線 <stage> 階段發生無法重試的錯誤`                  | `錯誤代碼 <code>`，例如 `triage_provider_http_401`、`embed_provider_http_402`、`newsroom_llm_configuration_missing`                                                          | 401／403：金鑰錯誤或被撤銷；402：額度不足；400／404：model 名稱或 base URL 錯誤；`*_configuration_missing`：未設定金鑰。修正 Secret 並重新部署後，以[復原工具](#復原工具) `requeue --stage <stage> --error-code <code>` 重新排入該錯誤碼的 `failed` 資料列。同一 stage 與錯誤碼每小時最多通知一次（worker 重啟後重新計算）。 |

注意：

- `stage_fatal` 標題中的 `<stage>` 是佇列 stage（`embed`、`triage`、`analysis`、`why`、
  `translate`）；錯誤碼前綴則是 `newsroom_llm_calls.stage`（`embed`、`triage`、`analysis`、
  `why`、`translate`），可直接用來篩選稽核紀錄與要重新排入的資料列。復原工具的
  `--stage` 使用欄位前綴，`translate` 佇列為 `en`。
- `embed` 前的標題翻譯失敗（例如 `embed_provider_http_402`）不發 `stage_fatal`，文章也
  不會 `failed`：它以原文 embedding 後照常進入初篩。修正後以 `reembed-untranslated`
  重新 embedding 尚未初篩的文章。
- 08:00 組稿在 `orchestration-worker` 執行，它的金鑰錯誤不會發 `stage_fatal`，而是表現為
  「精選失敗 已改用粗分排序」。

## 來源健康

```sql
SELECT key, name, kind, enabled, poll_interval_minutes, last_polled_at, last_success_at,
       consecutive_failures, last_error_code, last_error_at, next_poll_at
FROM newsroom_sources
WHERE kind <> 'manual'
ORDER BY consecutive_failures DESC, last_success_at NULLS FIRST;
```

| `last_error_code`                                                                 | 意義與處置                                                                                                                                                         |
| --------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `timeout`、`unreachable`、`dns_failed`、`http_429`、`http_5xx`（例如 `http_503`） | 暫時性故障，下次輪詢自動再試；長時間持續才需處理                                                                                                                   |
| `http_401`、`http_403`、`http_404`、`http_410`                                    | 發布者封鎖或 feed 已移除；改用新的 feed 網址或停用                                                                                                                 |
| `robots_disallowed`、`robots_unavailable`                                         | robots.txt 禁止或無法取得（僅在 `DAILY_INSIGHTS_NEWS_IGNORE_ROBOTS=false` 時會出現）；預設已略過 robots，若仍看到此碼代表部署尚未帶上新設定，或改用其他 feed／停用 |
| `redirected`                                                                      | feed 會轉址；把網址改成轉址後的最終網址                                                                                                                            |
| `unsafe_destination`                                                              | 網址解析到非公開位址或不符合 HTTPS 規則；確認網址                                                                                                                  |
| `too_large`、`parse_error`                                                        | 回應過大或格式與 `kind` 不符；確認格式與網址                                                                                                                       |
| `guardian_api_key_missing`、`sec_contact_email_missing`                           | 需要憑證的來源未設定；設定 `DAILY_INSIGHTS_GUARDIAN_API_KEY` 或 `DAILY_INSIGHTS_SEC_CONTACT_EMAIL` 並重新部署，或停用來源。這類略過不計入連續失敗                  |
| `host_blocked`                                                                    | 網域列在 `DAILY_INSIGHTS_NEWS_BLOCKED_HOSTNAMES`；移除封鎖或停用來源                                                                                               |

- 停用來源在後台按「停用」即可，不需部署；已收到的文章不受影響。
- 緊急封鎖某網域（例如發布者要求停止抓取）使用 `DAILY_INSIGHTS_NEWS_BLOCKED_HOSTNAMES`：
  它同時停止輪詢該網域的來源、拒絕抓取該網域的全文，並拒絕管理員以該網域送出池外
  URL。需重新部署生效。
- 需要立即重新輪詢某來源時，可把 `next_poll_at` 設為 NULL；worker 一分鐘內會領取。

## 版次發布與晚到補上

- **09:00 前**：管理員在後台核准單一市場或全部草稿，立即發布（`published_by_user_id`
  記錄核准者）。核准在 API 內完成，不依賴 `newsroom-worker`；空的草稿也可以核准。
- **09:00 自動發布**：由 `newsroom-worker` 每分鐘檢查。worker 當下停止時，草稿維持
  `draft`，讀者端繼續顯示上一個已發布版次並標示日期；worker 恢復後會立即補發布。
- **空版次暫停自動發布**：到期的草稿沒有任何未移除、未隱藏、未放棄的項目時不自動
  發布，維持 `draft`，第一次暫停時記錄 `held_empty_at` 並發出
  [`版次沒有任何項目 暫停自動發布`](#slack-通知與處置)（同一版次只通知一次，同一日期的
  市場合併為一則）。修正原因並[重新組稿](#重新組稿)後，有項目的市場在下一輪自動發布；
  確定當天沒有新聞時可在後台直接核准空版次。暫停期間讀者端顯示「今日新聞準備中」與
  上一個有內容的版次。查詢被暫停的版次：

  ```sql
  SELECT edition_date, market_code, held_empty_at, assembled_at, auto_publish_at
  FROM newsroom_editions
  WHERE status = 'draft' AND held_empty_at IS NOT NULL
  ORDER BY edition_date, market_code;
  ```

- **延後自動發布**（例如需要更多審核時間），只對仍為草稿的版次有效：

  ```sql
  UPDATE newsroom_editions
  SET auto_publish_at = TIMESTAMPTZ '2026-10-02 09:30:00+08'
  WHERE edition_date = DATE '2026-10-02' AND market_code = 'tw_equity' AND status = 'draft';
  ```

- **晚到補上**：發布後，分析完成的項目即自動可見並排入英文翻譯。12:00
  （`late_fill_deadline`）仍未完成的項目被標記放棄並通知，每個版次只執行一次
  （`late_fill_closed_at`）。需要延長時，在 12:00 前調整：

  ```sql
  UPDATE newsroom_editions
  SET late_fill_deadline = TIMESTAMPTZ '2026-10-02 14:00:00+08'
  WHERE edition_date = DATE '2026-10-02' AND late_fill_closed_at IS NULL;
  ```

### 重開已發布的版次

組稿只重建 `draft`，已發布的版次要先以[復原工具](#復原工具)的 `reopen` 退回草稿：

```sh
# 先 dry run，確認每個市場的 action 都是 reopen
make newsroom-recover CMD=reopen ARGS='--edition-date 2026-10-25 --extend-late-fill 18:00'
make newsroom-recover CMD=reopen ARGS='--edition-date 2026-10-25 --extend-late-fill 18:00 --actor ops@example.com' APPLY=1
```

- **只重開沒有項目的版次**：版次仍有未移除、未隱藏、未放棄的項目時拒絕，需加
  `--force`。重開有項目的版次會讓它從讀者端消失，直到重新發布（讀者看到上一個版次）。
- **等待重新組稿**：重開會清除 `published_at`、`assembled_at`、`late_fill_closed_at` 與
  `held_empty_at`，被放棄的項目恢復。`assembled_at` 為 NULL 的草稿不會被自動發布，也
  不會被暫停通知，直到[重新組稿](#重新組稿)；組稿後下一輪 sweep 發布有項目的市場，仍
  沒有項目則暫停並通知。
- **晚到補上期限**：重開會重新開始晚到補上，重新發布後到期限仍未完成分析的項目會被
  放棄。原期限已過時必須以 `--extend-late-fill` 給新期限（`HH:MM` 為台北時間今天，或
  ISO 時間如 `2026-10-28T02:00+08:00`），否則拒絕。
- **全有或全無**：同一日期的任一市場被拒絕時，`--apply` 不會改動任何市場；可用
  `--market` 只處理其中一個。
- **稽核**：`newsroom_edit_log.user_id` 不可為 NULL，只有加 `--actor <管理員 email>` 時
  才寫入 `action = 'reopen'` 的紀錄（含重開前的狀態與項目數）；未加時保留工具輸出。

工具無法使用時的 SQL 備援（同一個交易內執行，先確認版次沒有項目）：

```sql
BEGIN;
UPDATE newsroom_editions
SET status = 'draft', published_at = NULL, published_by_user_id = NULL,
    assembled_at = NULL, late_fill_closed_at = NULL, held_empty_at = NULL,
    late_fill_deadline = TIMESTAMPTZ '2026-10-27 18:00:00+08'
WHERE edition_date = DATE '2026-10-25' AND market_code = 'global' AND status = 'published';
UPDATE newsroom_edition_items i
SET abandoned_at = NULL
FROM newsroom_editions e
WHERE e.id = i.edition_id AND e.edition_date = DATE '2026-10-25'
  AND e.market_code = 'global' AND i.abandoned_at IS NOT NULL;
COMMIT;
```

### 恢復被放棄的項目

被放棄的項目不會再自動上架。先修正原因，在後台對該事件「重新分析」（或以 SQL 重新
排入 `analysis`）；分析成功時會一併重寫該事件所有未移除項目（包含已放棄者）的
「為何重要」。確認 `analysis_status = 'ready'` 後清除放棄標記，該則即對讀者可見
（「為何重要」只在後台顯示，不必等它完成），英文翻譯由每分鐘的 stale sweep 自動排入
（限發布後 2 天內的版次）：

```sql
UPDATE newsroom_edition_items i
SET abandoned_at = NULL
FROM newsroom_events e
WHERE i.id = '<item uuid>' AND e.id = i.event_id AND e.analysis_status = 'ready';
```

整個版次的項目都被放棄（例如停擺期間的分析全部失敗）時，改用
[重開已發布的版次](#重開已發布的版次)並重新組稿。

### 發布後的修正

- 改字（標題、摘要、「為何重要」）立即生效，簡中同步更新，英文自動重新翻譯。改標題或
  摘要時，翻譯完成前英文頁暫不顯示該則；只改「為何重要」不影響讀者端。
- 不應再出現的項目使用「隱藏」；隱藏只影響該市場版。
- 相關標的只能保留站內有儀表板的標的，對不上的修改會被拒絕。

## 正文清除

`newsroom-worker` 每 10 分鐘檢查一次，在每天 03:00 之後清除 `first_seen_at` 早於 30 天
的文章正文（`body_status = 'purged'`）。確認清除進度：

```sql
SELECT body_status, count(*), min(first_seen_at), max(first_seen_at)
FROM newsroom_articles
WHERE first_seen_at < now() - interval '30 days'
GROUP BY 1;
```

正常情況下結果只有 `purged`。若持續看到 `ok`，確認 `newsroom-worker` 是否在執行，以及
log 中的 `newsroom.periodic_error`（`task = newsroom_purge_bodies`）。正文已清除的事件
無法再自動分析（會停在「缺全文」），需要時以「貼上全文」處理。

## 停用與重新啟用

- 停用：把開關設為 `false` 並重新部署（本機是 `apps/api/.env` 的
  `DAILY_INSIGHTS_NEWSROOM_ENABLED`；正式環境是 GitHub Variable
  `DAILY_INSIGHTS_DAILY_NEWS_ENABLED`，release 以 `DAILY_INSIGHTS_NEWSROOM_ENABLED` 傳入）。`newsroom-worker`
  轉為只維持心跳，08:00 組稿回傳 `no_change`，已發布內容照常提供，進行中的佇列資料列
  保留原狀。
- 重新啟用：設回 `true` 並確認 LLM 與 embedding 金鑰後重新部署。worker 會從資料列狀態
  繼續；租約已過期的 `pending` 資料列會自動被重新領取。停用期間沒有輪詢，該段時間發布的
  文章若已超過 24 小時不會補收。

## 切換 LLM 供應商

所有 LLM 階段（初篩、精選、分析、為何重要、翻譯，以及 embed 前的標題翻譯）共用
`DAILY_INSIGHTS_NEWSROOM_LLM_*` 這組 OpenAI 相容設定；embedding 走
`DAILY_INSIGHTS_NEWSROOM_EMBEDDING_*`，換 LLM 供應商時不需要改。目前使用過兩組：
DeepSeek 官方 API 與 OpenCode Go 訂閱（2026-10-27 起，DeepSeek 金鑰在 10-24 開始回
402 後停用）。兩者是同一個模型（DeepSeek-V4.1-Flash），差別在端點、識別 header 與
預設是否推理。

| 設定（`DAILY_INSIGHTS_` 之後）    | DeepSeek 官方                 | OpenCode Go                                  |
| --------------------------------- | ----------------------------- | -------------------------------------------- |
| `NEWSROOM_LLM_BASE_URL`           | `https://api.deepseek.com`    | `https://opencode.ai/inference/go/openai/v1` |
| `NEWSROOM_LLM_API_KEY`            | DeepSeek 平台金鑰             | OpenCode Go 訂閱金鑰                         |
| 四個 `NEWSROOM_*_MODEL`           | `deepseek-chat`               | `deepseek-v4.1-flash`                        |
| `NEWSROOM_LLM_USER_AGENT`         | `daily-insights-newsroom/1.0` | `daily-insights-newsroom/1.0`                |
| `NEWSROOM_LLM_SESSION_HEADER`     | 空白（不送）                  | `x-opencode-session`（必填）                 |
| `NEWSROOM_LLM_EXTRA_BODY`         | 空白                          | `{"thinking":{"type":"disabled"}}`           |
| `NEWSROOM_EDITOR_EXTRA_BODY`      | 空白                          | 空白（精選要推理時才設）                     |
| `NEWSROOM_EDITOR_TIMEOUT_SECONDS` | 空白                          | 空白（精選推理時建議 240–300）               |

DeepSeek 官方：

```sh
DAILY_INSIGHTS_NEWSROOM_LLM_BASE_URL=https://api.deepseek.com
DAILY_INSIGHTS_NEWSROOM_LLM_API_KEY=<DeepSeek 金鑰>
DAILY_INSIGHTS_NEWSROOM_LLM_USER_AGENT=daily-insights-newsroom/1.0
DAILY_INSIGHTS_NEWSROOM_LLM_SESSION_HEADER=
DAILY_INSIGHTS_NEWSROOM_LLM_EXTRA_BODY=
DAILY_INSIGHTS_NEWSROOM_EDITOR_EXTRA_BODY=
DAILY_INSIGHTS_NEWSROOM_EDITOR_TIMEOUT_SECONDS=
DAILY_INSIGHTS_NEWSROOM_TRIAGE_MODEL=deepseek-chat
DAILY_INSIGHTS_NEWSROOM_EDITOR_MODEL=deepseek-chat
DAILY_INSIGHTS_NEWSROOM_ANALYSIS_MODEL=deepseek-chat
DAILY_INSIGHTS_NEWSROOM_TRANSLATE_MODEL=deepseek-chat
```

OpenCode Go（精選也不推理；要讓精選推理時，另設
`DAILY_INSIGHTS_NEWSROOM_EDITOR_EXTRA_BODY='{"thinking":{"type":"enabled"},"reasoning_effort":"high"}'`
與 `DAILY_INSIGHTS_NEWSROOM_EDITOR_TIMEOUT_SECONDS=300`，並先以重播驗證）：

```sh
DAILY_INSIGHTS_NEWSROOM_LLM_BASE_URL=https://opencode.ai/inference/go/openai/v1
DAILY_INSIGHTS_NEWSROOM_LLM_API_KEY=<OpenCode Go 金鑰>
DAILY_INSIGHTS_NEWSROOM_LLM_USER_AGENT=daily-insights-newsroom/1.0
DAILY_INSIGHTS_NEWSROOM_LLM_SESSION_HEADER=x-opencode-session
DAILY_INSIGHTS_NEWSROOM_LLM_EXTRA_BODY='{"thinking":{"type":"disabled"}}'
DAILY_INSIGHTS_NEWSROOM_EDITOR_EXTRA_BODY=
DAILY_INSIGHTS_NEWSROOM_EDITOR_TIMEOUT_SECONDS=
DAILY_INSIGHTS_NEWSROOM_TRIAGE_MODEL=deepseek-v4.1-flash
DAILY_INSIGHTS_NEWSROOM_EDITOR_MODEL=deepseek-v4.1-flash
DAILY_INSIGHTS_NEWSROOM_ANALYSIS_MODEL=deepseek-v4.1-flash
DAILY_INSIGHTS_NEWSROOM_TRANSLATE_MODEL=deepseek-v4.1-flash
```

設定說明：

- **Session header**：Go 拒絕沒有 `x-opencode-session` 的呼叫（HTTP 400
  `MissingSessionID`），佇列會把它當成不可重試錯誤，整批資料列直接 `failed`。因此
  base URL 在 `opencode.ai` 而 session header 空白時，持有 LLM 金鑰的服務
  （`newsroom-worker`、`orchestration-worker`，本機另有 API）都拒絕啟動
  （`newsroom_llm_session_header is required for opencode.ai`）。
  session id 由 stage 與 prompt 版本產生，同一階段的呼叫共用一個，讓 Go 命中同一份
  prompt 快取。
- **關閉推理**：Go 的 `deepseek-v4.1-flash` 預設會推理，而 prompt 都是在不推理的
  DeepSeek 上校準的，所以 `LLM_EXTRA_BODY` 要關閉推理。`{"reasoning_effort":"none"}` 也
  有效；`"reasoning_effort":"minimal"`、`enable_thinking`、`chat_template_kwargs` 都關不掉。
- **額外欄位**：`LLM_EXTRA_BODY` 是單行 JSON 物件，合併進每一次 chat completion（含
  修復重試）；`EDITOR_EXTRA_BODY` 只用於精選、去重與重複新聞篩選，頂層 key 覆蓋
  `LLM_EXTRA_BODY` 的同名 key。兩者都不能設定 `model`、`messages`、`response_format`、
  `temperature`、`stream`，否則設定驗證失敗、服務不會啟動。`apps/api/.env` 中可用單引號
  包住或不加引號；GitHub Variable 直接貼 JSON，不加引號。
- **稽核**：Go 不回 `x-request-id`，`newsroom_llm_calls.request_id` 改記
  `x-opencode-log-id`；向 OpenCode 回報問題時提供這個值。
- **額度**：Go 的 `deepseek-v4.1-flash` 每月約 US$80 額度，任 5 小時內最多用掉 20%、
  每週 50%。台北時間週一至週五 09:00–12:00、14:00–18:00 是 Go 的尖峰，同樣的呼叫
  扣得比較多。可在 OpenCode console 開啟 Extra Usage，額度用完後改扣預付餘額。

### 切換步驟

1. 確認要執行的映像包含這組設定（`DAILY_INSIGHTS_NEWSROOM_LLM_SESSION_HEADER` 等）。
   舊映像會忽略不認得的設定，對 Go 送出沒有 session header 的請求，全部得到 400。
2. 本機：改 `apps/api/.env`，再重新建立會呼叫 LLM 的容器（`restart` 不會重讀
   `env_file`）：
   `docker compose up -d --no-build --force-recreate newsroom-worker orchestration-worker api`。
   正式環境：在 GitHub `production` environment 更新上表的 Variables；LLM 金鑰放在既有的
   Secret `DAILY_INSIGHTS_NEWS_MODEL_API_KEY`（workflow 以
   `DAILY_INSIGHTS_NEWSROOM_LLM_API_KEY` 傳給服務，沒有這個名稱的 Secret），換金鑰時更新
   它。再執行 release workflow 重新部署
   （[正式環境維運操作手冊](production.md#release-procedure)）；deploy 會重新建立
   `orchestration-worker` 與 `newsroom-worker`。
3. 確認 worker log 沒有 `fatal`，並查看切換後的呼叫：

   ```sql
   SELECT stage, model, error_code, count(*), max(request_id) AS sample_request_id
   FROM newsroom_llm_calls
   WHERE created_at > now() - interval '15 minutes'
   GROUP BY stage, model, error_code
   ORDER BY stage, error_code NULLS FIRST;
   ```

4. 切換前因供應商錯誤而 `failed` 的資料列不會自動恢復，依錯誤碼[重新排入](#重新排入)；
   已發布的空版次見[版次發布與晚到補上](#版次發布與晚到補上)。

### 供應商錯誤碼

| 錯誤碼                         | DeepSeek 官方              | OpenCode Go                                                                      | 處置                                                                                                                                                                                                                                    |
| ------------------------------ | -------------------------- | -------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `<stage>_provider_http_400`    | 請求格式或 model 錯誤      | 缺 session header（`MissingSessionID`）或 model 不存在（`Model is unavailable`） | 不可重試。修正設定、重新部署後重新排入                                                                                                                                                                                                  |
| `<stage>_provider_http_401`    | 金鑰錯誤或被撤銷           | 金鑰錯誤（`Invalid credential`）                                                 | 不可重試。換金鑰、重新部署後重新排入                                                                                                                                                                                                    |
| `<stage>_provider_http_402`    | 餘額不足（10-24 停擺原因） | 文件未列出                                                                       | 不可重試。儲值或切換供應商後重新排入                                                                                                                                                                                                    |
| `<stage>_provider_http_429`    | 限流                       | 限流                                                                             | 自動重試，依 `Retry-After` 退避                                                                                                                                                                                                         |
| `<stage>_provider_usage_limit` | —                          | 5 小時、每週或每月額度用完（HTTP 429 `GoUsageLimitError`）                       | 資料列留在 `pending` 等待，不用掉重試次數，也不會轉 `failed`：依 `retry-after` 再試（最多 6 小時；沒有時 60 分鐘）。每個 stage 每小時發一則「OpenCode Go 額度已用完」通知，附預計恢復時間。不需要重新排入；要提早恢復可開啟 Extra Usage |

## 舊管線的執行紀錄

- 舊新聞的 orchestration job／function runs（`news_*_refresh`、`news_publish`、
  `news_daily_update` 等已移除的 key）仍列在 orchestration 後台的歷史中，但無法執行或
  重跑；切換 migration 已取消當時仍為 pending 或 running 的紀錄。這些紀錄不需要也
  不應手動修改。
- `legacy_data_management_runs` 是唯讀封存（trigger 保護），保留 `news_all`、
  `news_market`、`news_publish` 等歷史紀錄，可由
  `GET /api/admin/orchestration/legacy-runs` 查閱，不提供任何恢復操作。
- 舊版次的內容已搬入 `newsroom_*`（`selection_mode = legacy`），需要隱藏時在審核頁
  處理，見[重點新聞架構](../architecture/daily-news.md#舊版次資料搬移)。

### 已套用舊版 import 的來源檢查

`20261029_0054` 會補建 `newsroom_events.legacy_sources`（若尚無此欄位），並從 import 當時建立或
連結、仍有關聯的歷史文章回填來源；`manual` 連結以讀者端原本顯示的 hostname 命名。
新部署的 `0034` 會在刪除舊表前保存每個事件的來源快照，包含跨日期重複 URL。
如 migration 記錄 `legacy newsroom events have no recoverable source link`，可先唯讀確認：

```sql
SELECT id, edition_date, working_title
FROM newsroom_events
WHERE created_by = 'legacy' AND legacy_sources = '[]'::jsonb
ORDER BY edition_date, id;
```

這些事件在舊 import 已失去文章關聯；目前資料無法可靠推回原始 URL。將切換前備份還原到
隔離資料庫，比對版次日期、市場與舊新聞三語內容，確認對應後才補入 `legacy_sources`
（`[{"name":"原來源名稱","url":"https://原始網址","published_at":null}]`；有發布時間則填 ISO 8601）。
不得在正式資料庫回跑歷史 import、重新指派共用文章的 event ID，或只依標題猜測網址。
降級 `0054` 保留來源快照，重新升級時不覆寫已有快照。
若部署仍在原始 `0053` schema，也可直接降級至 `20261001_0033`；修正版 `0034` 的降級容許快照欄位不存在。

事件合併會轉移來源快照，依 URL 去重並優先保留目標來源的名稱與發布時間，目標事件不必是歷史匯入事件。
拆分選取文章時，對應 URL 的快照隨文章移動；只有快照、沒有文章的來源留在原事件。
兩者的編輯紀錄 `after.moved_snapshot_urls` 列出搬移的快照 URL，可據此追查讀者看到的來源變化。
後台事件卡片會以「舊版快照」標示沒有文章的快照連結。
不要只用 SQL 搬移 `event_id`，以免略過來源快照與版次項目的同步處理。

## 相關文件

- [重點新聞架構](../architecture/daily-news.md)
- [統一 orchestration 操作手冊](unified-orchestration.md)
- [正式環境維運操作手冊](production.md)
