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
與 `<stage>_error_code`：

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
4. SQL 重新排入與 `queue.enqueue` 相同：`status = 'pending'`、`attempts = 0`、
   `next_attempt_at = NULL`（設為 `now()` 亦可，兩者都會立即被領取）、`error_code = NULL`。
   worker 會在下一次輪詢（預設 2 秒）領取，不需要重啟。

| 狀況                                       | 優先做法                                                                                       |
| ------------------------------------------ | ---------------------------------------------------------------------------------------------- |
| 事件「分析失敗」或項目「『為何重要』失敗」 | 審核頁該則的「重新分析」：事件 `analysis` 與其所有未移除、未隱藏、未放棄項目的 `why` 重新排入  |
| 「缺全文」（`needs_body`）                 | 原文對照中對某篇文章「貼上全文」，事件自動重新排入分析                                         |
| 文章 `fetch_status = failed`               | 以 SQL 重新排入 `fetch`；全文取得後若事件仍是 `needs_body` 或要用新全文重寫，再按「重新分析」  |
| 文章 `embed_status = failed`               | 以 SQL 重新排入 `embed`；成功後自動排入初篩                                                    |
| 文章 `triage_status = failed`              | `embedding` 為 NULL 時重新排入 `embed`，否則重新排入 `triage`                                  |
| 事件 `en_status = failed`                  | 改任何繁中文字會自動重新排入；否則以 SQL 重新排入 `en`                                         |
| 事件分到錯的群                             | 後台「合併其他事件到此」或「拆分選取的文章」，受影響事件自動重新分析；不要用 SQL 改 `event_id` |

重新排入某版次所有 `failed` 的文章 stage（以 `embed` 為例，`fetch`、`triage` 只需替換
欄位前綴）：

```sql
UPDATE newsroom_articles
SET embed_status = 'pending', embed_attempts = 0,
    embed_next_attempt_at = NULL, embed_error_code = NULL
WHERE edition_date = DATE '2026-10-02' AND embed_status = 'failed';
```

修正金鑰後，重新排入同一錯誤碼造成的事件分析失敗：

```sql
UPDATE newsroom_events
SET analysis_status = 'pending', analysis_attempts = 0,
    analysis_next_attempt_at = NULL, analysis_error_code = NULL
WHERE analysis_status = 'failed'
  AND analysis_error_code = 'analysis_provider_http_402'
  AND edition_date >= DATE '2026-10-01';
```

以 SQL 重新排入 `analysis` 時，項目的「為何重要」會隨分析一起寫入，不需另外排入。
單獨重新排入項目 `why` 只適用於事件已是 `ready` 的情況（`why` stage 只領取已分析事件
的項目）。翻譯以事件為單位：

```sql
UPDATE newsroom_events
SET en_status = 'pending', en_attempts = 0, en_next_attempt_at = NULL, en_error_code = NULL
WHERE id = '<event uuid>' AND en_status = 'failed';
```

已經在佇列中的 provider 設定錯誤：只改 GitHub Secret 或 `apps/api/.env` 不會影響執行中
的容器；正式環境需重新部署（本機需重新建立容器）讓新值生效，之後再重新排入。

### 重新組稿

08:00 組稿失敗時，統一 orchestration 每 30 分鐘重試到 10:00；精選失敗則已自動退回
粗分（`fallback`）。需要在修正後重新組稿某日草稿時：

- 本機：`make assemble-newsroom EDITION_DATE=2026-10-02`（省略時為當日）。
- 正式環境：在已具備組稿設定的 `orchestration-worker` 容器內執行同一個 script：
  `docker exec daily-insights-orchestration-worker python -m daily_insights_api.scripts.run_newsroom_assemble`，
  日期參數與 `make assemble-newsroom` 傳入的相同。

組稿是冪等的：只重建仍為 `draft` 的市場，保留管理員加入與移除的項目；已發布的
市場不會被改動。重新組稿會再發一次「草稿完成」通知。

## Slack 通知與處置

通知由 `DAILY_INSIGHTS_NEWSROOM_SLACK_WEBHOOK_URL` 送出（未設定時只在 log 出現
`newsroom.notice`）。標題以粗體顯示，帶連結的訊息最後一行為「開啟後台」。

| 通知標題（範例）                                           | 內容                                                                                                                | 處置                                                                                                                                                                                                                                                                |
| ---------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `2026-10-02 重點新聞草稿完成`                              | 各市場則數（精選失敗者附「精選失敗 改用粗分排序」）、5 星事件標題、「組稿時仍有 N 篇文章未完成初篩 已忽略」         | 09:00 前開啟後台審核、改字、核准。有大量未初篩文章時檢查 `embed`／`triage` 佇列與 LLM 錯誤；修正後可[重新組稿](#重新組稿)。                                                                                                                                         |
| `2026-10-02 tw_equity 精選失敗 已改用粗分排序`             | `入選 N 則 星等留空`                                                                                                | 以 `newsroom_llm_calls` 中 `stage = 'editor'` 的錯誤碼判斷原因。草稿仍可用：可直接人工調整項目後核准，或修正後重新組稿取得星等。                                                                                                                                    |
| `2026-10-02 us_equity 有 N 則新聞在 12:00 前未完成 已放棄` | 被放棄項目的事件標題                                                                                                | 查詢該版次項目的 `analysis_error_code`／`why_error_code`。該則已不對讀者顯示；若修正後仍要上架，見[恢復被放棄的項目](#恢復被放棄的項目)。                                                                                                                           |
| `新聞來源「<名稱>」持續抓取失敗`                           | 來源代碼、連續失敗次數與最後錯誤碼、最後成功時間（台北時間，或「從未成功」）                                        | 開啟「新聞來源管理」確認來源。依[來源錯誤碼](#來源健康)修正網址、格式或連結規則，或暫時停用來源。同一次故障只通知一次，來源成功一次後重置。                                                                                                                         |
| `新聞管線 <stage> 階段發生無法重試的錯誤`                  | `錯誤代碼 <code>`，例如 `triage_provider_http_401`、`embed_provider_http_402`、`newsroom_llm_configuration_missing` | 401／403：金鑰錯誤或被撤銷；402：額度不足；400／404：model 名稱或 base URL 錯誤；`*_configuration_missing`：未設定金鑰。修正 Secret 並重新部署後，[重新排入](#重新排入)該錯誤碼的 `failed` 資料列。同一 stage 與錯誤碼每小時最多通知一次（worker 重啟後重新計算）。 |

注意：

- `stage_fatal` 標題中的 `<stage>` 是佇列 stage（`embed`、`triage`、`analysis`、`why`、
  `translate`）；錯誤碼前綴則是 `newsroom_llm_calls.stage`（`embed`、`triage`、`analysis`、
  `why`、`translate`），可直接用來篩選稽核紀錄與要重新排入的資料列。
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

| `last_error_code`                                                                 | 意義與處置                                                                                                                                        |
| --------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `timeout`、`unreachable`、`dns_failed`、`http_429`、`http_5xx`（例如 `http_503`） | 暫時性故障，下次輪詢自動再試；長時間持續才需處理                                                                                                  |
| `http_401`、`http_403`、`http_404`、`http_410`                                    | 發布者封鎖或 feed 已移除；改用新的 feed 網址或停用                                                                                                |
| `robots_disallowed`、`robots_unavailable`                                         | robots.txt 禁止或無法取得；不得繞過，改用其他 feed 或停用                                                                                         |
| `redirected`                                                                      | feed 會轉址；把網址改成轉址後的最終網址                                                                                                           |
| `unsafe_destination`                                                              | 網址解析到非公開位址或不符合 HTTPS 規則；確認網址                                                                                                 |
| `too_large`、`parse_error`                                                        | 回應過大或格式與 `kind` 不符；確認格式與網址                                                                                                      |
| `guardian_api_key_missing`、`sec_contact_email_missing`                           | 需要憑證的來源未設定；設定 `DAILY_INSIGHTS_GUARDIAN_API_KEY` 或 `DAILY_INSIGHTS_SEC_CONTACT_EMAIL` 並重新部署，或停用來源。這類略過不計入連續失敗 |
| `host_blocked`                                                                    | 網域列在 `DAILY_INSIGHTS_NEWS_BLOCKED_HOSTNAMES`；移除封鎖或停用來源                                                                              |

- 停用來源在後台按「停用」即可，不需部署；已收到的文章不受影響。
- 緊急封鎖某網域（例如發布者要求停止抓取）使用 `DAILY_INSIGHTS_NEWS_BLOCKED_HOSTNAMES`：
  它同時停止輪詢該網域的來源、拒絕抓取該網域的全文，並拒絕管理員以該網域送出池外
  URL。需重新部署生效。
- 需要立即重新輪詢某來源時，可把 `next_poll_at` 設為 NULL；worker 一分鐘內會領取。

## 版次發布與晚到補上

- **09:00 前**：管理員在後台核准單一市場或全部草稿，立即發布（`published_by_user_id`
  記錄核准者）。核准在 API 內完成，不依賴 `newsroom-worker`。
- **09:00 自動發布**：由 `newsroom-worker` 每分鐘檢查。worker 當下停止時，草稿維持
  `draft`，讀者端繼續顯示上一個已發布版次並標示日期；worker 恢復後會立即補發布。
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

### 恢復被放棄的項目

被放棄的項目不會再自動上架。先修正原因，在後台對該事件「重新分析」（或以 SQL 重新
排入 `analysis`）；分析成功時會一併重寫該事件所有未移除項目（包含已放棄者）的
「為何重要」。確認 `analysis_status = 'ready'` 且項目 `why_status = 'ready'` 後清除放棄
標記，該則即對讀者可見，英文翻譯由每分鐘的 stale sweep 自動排入（限發布後 2 天內的
版次）：

```sql
UPDATE newsroom_edition_items
SET abandoned_at = NULL
WHERE id = '<item uuid>' AND why_status = 'ready';
```

### 發布後的修正

- 改字（標題、摘要、「為何重要」）立即生效，簡中同步更新，英文自動重新翻譯；翻譯完成前
  英文頁暫不顯示該則。
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

- 停用：把 `DAILY_INSIGHTS_NEWSROOM_ENABLED` 設為 `false` 並重新部署。`newsroom-worker`
  轉為只維持心跳，08:00 組稿回傳 `no_change`，已發布內容照常提供，進行中的佇列資料列
  保留原狀。
- 重新啟用：設回 `true` 並確認 LLM 與 embedding 金鑰後重新部署。worker 會從資料列狀態
  繼續；租約已過期的 `pending` 資料列會自動被重新領取。停用期間沒有輪詢，該段時間發布的
  文章若已超過 24 小時不會補收。

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

## 相關文件

- [重點新聞架構](../architecture/daily-news.md)
- [統一 orchestration 操作手冊](unified-orchestration.md)
- [正式環境維運操作手冊](production.md)
