# 重點新聞 Newsroom 管線重構規格

> 給實作者（Claude Code 平行工作樹）的完整交辦文件。撰寫日期 2026-10-01。
> 本規格**取代** `docs/architecture/daily-news.md` 描述的現行管線，不沿用其設計。
> 所有決策已與產品負責人逐題確認，實作時**不得自行改變決策**；遇到規格未涵蓋或互相衝突之處，
> 先回報協調者（coordinator），不要自行發明產品行為。

## 0. 目標與非目標

這次重構要解決三個問題：

1. **複雜度與脆弱性**：現行多階段 LLM 管線（大型選題 prompt、視窗、reserve、checkpoint、
   dependency cooldown、failure classification）每次修補都牽一髮動全身。
2. **選題品質**：選題單位改為「事件」，用多家報導作為重要性訊號。
3. **編輯控制權**：來源可在後台管理；草稿可在上架前審核、改字、換稿。

產品形態不變：每天（含週末）、分 `global`／`tw_equity`／`us_equity` 三個市場版、精選數則。

非目標：盤中即時更新版面、多空／情緒判斷、讀者端篩選。

## 1. 總覽

```
newsroom_sources（DB 管理）
   │  news worker 依各來源頻率持續輪詢
   ▼
newsroom_articles ──全文抓取＋品質檢查──► body（Postgres，保留 30 天）
   │  embedding（OpenAI-compatible）
   │  逐篇初篩（DeepSeek）：市場粗分、主題、事件歸屬（kNN top-5 候選事件 + LLM 判斷）
   ▼
newsroom_events（單一版次窗內的事件）
   │  08:00 組稿：每市場取粗分前 30 事件 → 精選 LLM 打「事件×市場」星等 → 程式套配額
   ▼
newsroom_editions（draft）＋ newsroom_edition_items
   │  深度分析（每事件一次，產出共用事實 + 各市場「為何重要」）
   │  Slack 通知「草稿完成」；管理員審核、改字、換稿、核准
   ▼  09:00 未核准者自動發布；09:00 後完成的項目 12:00 前自動補上
newsroom_editions（published，可持續編修，編輯紀錄寫入 newsroom_edit_log）
   │  繁中 → 簡中（OpenCC tw2sp，同步）；繁中定稿 → 英文（LLM，非同步）
   ▼
讀者 API：最近一個有可見項目的已發布版次，非今日則明確標示日期
```

## 2. 已確認的產品決策（不可更動）

| #   | 決策                                                                                                                                                           |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| D1  | 審核模型：AI 產草稿 → 管理員可在 09:00 前改字／換稿／排序／核准；09:00 未核准自動發布；發布後仍可編修或隱藏。                                                  |
| D2  | 時程（Asia/Taipei）：08:00 組稿，09:00 自動發布，12:00 停止自動補上。                                                                                          |
| D3  | 來源存資料庫並在後台管理（新增、停用、信任等級、權重、輪詢頻率、健康狀態）。                                                                                   |
| D4  | 全天持續收稿；全文存 Postgres，30 天後清除正文只留 metadata。全文永不提供給讀者。                                                                              |
| D5  | 逐篇初篩（收稿時）＋ 08:00 精選（每市場一次 LLM 打最終星等）；精選失敗退回粗分排序並標示 `fallback`。                                                          |
| D6  | 上架內容：標題、事實摘要（2–3 句，綜合多來源、只寫事實）、各市場「為何重要」（1–2 句）、相關標的（連到站內儀表板）、來源列表。**不做多空／情緒判斷。**         |
| D7  | 多語系：繁中由 LLM 產出且是唯一可編輯的語言；簡中由 OpenCC `tw2sp` 同步轉換；英文於發布時（及發布後繁中被修改時）由 LLM 非同步翻譯，未完成時英文頁不顯示該則。 |
| D8  | 配額（每市場，由**程式**執行）：5 星全收；4 星最多 5 則；5＋4 星總數不足 5 則時才用 1–3 星補位，最多補 5 則。                                                  |
| D9  | 同一事件可出現在多個市場版：標題、事實摘要、來源、相關標的共用；「為何重要」依市場各一段。                                                                     |
| D10 | 事件分群：embedding 找本版次窗內最相近的 5 個事件 → 放入初篩呼叫由 LLM 判斷歸屬或開新事件。後台可合併／拆分事件。                                              |
| D11 | embedding 使用 OpenAI-compatible API（預設 `text-embedding-3-small`，1536 維），向量存 `pgvector`。                                                            |
| D12 | 四個 LLM 階段（初篩、精選、深度分析、英文翻譯）全部使用 DeepSeek，但每階段 model 可獨立設定。                                                                  |
| D13 | 新聞管線由獨立常駐的 news worker 以「資料列即佇列」方式運作（`next_attempt_at` + `FOR UPDATE SKIP LOCKED`），不使用 checkpoint 表。                            |
| D14 | 跨日事件一律視為新事件：事件只存在於單一版次窗內。隱藏只對當版有效。                                                                                           |
| D15 | 09:00 只發布已完成項目；未完成者完成後自動補上，12:00 仍未完成則放棄並通知。                                                                                   |
| D16 | 當天沒有可用版次時，讀者端顯示最近一版並**明確標示日期**。                                                                                                     |
| D17 | 通知：Slack incoming webhook，經 `Notifier` 介面。                                                                                                             |
| D18 | 抓不到全文的文章仍可參與初篩與分群；事件中至少一篇全文才可自動分析上架，否則後台標示「缺全文」，可手動貼上內文。                                               |
| D19 | 第一版審核頁功能：改字、移除／加入項目、排序、合併／拆分事件、貼上全文、單一事件重新分析、手動輸入池外 URL。（附指示重跑、手動星等延後。）                     |
| D20 | 上線：本機驗證後一次切換；舊版次歷史資料搬移到新表；舊管線與舊表在切換階段移除。                                                                               |

## 3. 時間與版次窗

- 時區一律 `Asia/Taipei`。
- 版次日期 `D` 的收稿窗：`[D-1 08:00, D 08:00)`，以文章的 `first_seen_at` 判定，寫入 `newsroom_articles.edition_date`。
  08:00 之後才看到的文章屬於 `D+1`。
- 08:00 組稿前，最多等待 10 分鐘讓窗內 `triage_status = pending` 的文章完成初篩；逾時未完成者忽略並記錄數量。
- `newsroom_editions.auto_publish_at = D 09:00`、`late_fill_deadline = D 12:00`（存欄位，便於測試與本機調整）。

## 4. 資料模型（模組 `daily_insights_api.modules.newsroom`）

所有新表以 `newsroom_` 為前綴，與舊表並存直到切換階段。
完整欄位以 `apps/api/src/daily_insights_api/modules/newsroom/models.py` 為準，此處列語意。

### 4.1 `newsroom_sources`

來源設定，初始資料由舊 `feeds.py::FEED_SOURCES` 匯入。

- `key`（唯一 slug）、`name`、`kind`（`rss`／`rdf`／`atom`／`rss_full`／`news_sitemap`／`json_list`／`guardian_api`／`manual`）、
  `url`、`hostname`、`link_pattern`、`markets`（`text[]`，此來源預期涵蓋的市場）、`language_filter`、`full_text_in_feed`。
- `trust_tier`（1–3，3 最高）、`weight`（0.5–2.0，影響事件粗分）、`poll_interval_minutes`（預設 30）、`enabled`。
- 健康狀態：`last_polled_at`、`last_success_at`、`consecutive_failures`、`last_error_code`、`last_error_at`、`next_poll_at`。
- 一個固定的 `manual` 來源，承接管理員輸入的池外 URL。

### 4.2 `newsroom_articles`

- `source_id`、`url`、`url_hash`（SHA-256，唯一）、`title`、`feed_summary`、`published_at`（可為 null）、
  `first_seen_at`、`edition_date`、`language`。
- 全文：`body`（`text`，上限 40,000 字元）、`body_status`（`pending`／`ok`／`unavailable`／`rejected`／`purged`）、
  `body_quality_reason`、`body_fetched_at`、`body_source`（`feed`／`fetch`／`manual`）。
- 佇列欄位（每個階段一組）：`<stage>_status`、`<stage>_attempts`、`<stage>_next_attempt_at`、`<stage>_error_code`，
  stage ∈ `fetch`（全文抓取）、`embed`、`triage`。
- `embedding`：`vector(1536)`。
- 初篩結果：`relevant`（bool）、`topic`、`market_scores`（JSONB：`{"global": 0-100, "tw_equity": 0-100, "us_equity": 0-100}`）、
  `event_id`。

### 4.3 `newsroom_events`

- `edition_date`、`working_title`（初篩產生，僅後台使用）、`status`（`open`／`merged`）、`merged_into_id`、
  `created_by`（`triage`／`split`／`manual`）。
- 事件的報導數、來源數、是否有全文由 articles 彙總（可用 view 或查詢時計算，不冗餘存放）。
- 深度分析結果（事件層級、跨市場共用）：
  - `headline_zh_hant`、`summary_zh_hant`、`headline_zh_hans`、`summary_zh_hans`、`headline_en`、`summary_en`。
  - `related_symbols`（JSONB 陣列，每筆 `{symbol, kind, label}`，必須能對應站內儀表板，對應不到者丟棄）。
  - `analysis_status`（`idle`／`pending`／`ready`／`failed`／`needs_body`）＋ 佇列欄位、`analysis_model`、`analyzed_at`。
  - 英文翻譯：`en_status`（`idle`／`pending`／`ready`／`failed`）＋ 佇列欄位、`en_source_digest`（翻譯當時繁中內容的 digest，用來判斷是否過期）。
  - `edited_at`、`edited_by_user_id`。

### 4.4 `newsroom_editions`

- `edition_date`、`market_code`（`global`／`tw_equity`／`us_equity`），兩者唯一。
- `status`（`draft`／`published`）、`selection_mode`（`pending`／`editor`／`fallback`）、
  `auto_publish_at`、`late_fill_deadline`、`published_at`、`published_by_user_id`（null = 自動發布）、
  `assembled_at`、`ignored_pending_triage`（組稿時忽略的未初篩文章數）。

### 4.5 `newsroom_edition_items`

- `edition_id`、`event_id`（兩者唯一）、`rank`、`stars`（1–5，fallback 時可為 null）、`editor_score`（組稿時的事件粗分快照）、
  `origin`（`model`／`manual`）。
- 各市場「為何重要」：`why_zh_hant`、`why_zh_hans`、`why_en`、`why_status`（`pending`／`ready`／`failed`）＋ 佇列欄位、`why_en_status`。
- `removed_at`（草稿階段移除）、`hidden_at`／`hidden_by_user_id`（發布後隱藏）、`abandoned_at`（12:00 仍未完成）。
- 讀者可見條件：edition `published` ∧ 未 removed ∧ 未 hidden ∧ 未 abandoned ∧ 事件 `analysis_status = ready` ∧ `why_status = ready`；
  英文另需事件 `en_status = ready` 且 `en_source_digest` 為最新 ∧ `why_en` 已備妥。

### 4.6 `newsroom_edit_log`

所有管理員動作（改字、移除、加入、排序、合併、拆分、隱藏、核准、貼全文、重新分析、手動 URL）：
`entity_type`、`entity_id`、`action`、`before`（JSONB）、`after`（JSONB）、`user_id`、`created_at`。

### 4.7 `newsroom_llm_calls`

每次 LLM／embedding 呼叫的稽核：`stage`（`embed`／`triage`／`editor`／`analysis`／`why`／`translate`）、`subject_id`、`model`、
`prompt_version`、`input_tokens`、`output_tokens`、`latency_ms`、`error_code`、`created_at`。不存 prompt 與正文。

## 5. 佇列與重試（D13）

- 每個可失敗的工作以資料列欄位表示：`<stage>_status = pending` 且 `<stage>_next_attempt_at <= now()` 即可被領取。
- 領取：`SELECT … FOR UPDATE SKIP LOCKED LIMIT n`，在同一交易內把 `next_attempt_at` 推遲一個租約時間（預設 10 分鐘），
  完成後寫回結果並清除。worker 崩潰時租約到期自然重試，不需額外的 checkpoint。
- 失敗分兩類，**不再有細緻的 failure classification**：
  - **可重試**（網路、逾時、429、5xx、JSON／schema 不合法）：`attempts += 1`，指數退避（1、2、4、8… 分鐘，上限 60 分鐘），
    遵守 `Retry-After`；超過該 stage 的 `max_attempts` 則轉 `failed`。
  - **不可重試**（401／402／403、設定缺失）：直接 `failed`，並觸發 Slack 通知（同一錯誤碼每小時最多通知一次）。
- 共用實作在 `newsroom/queue.py`，各 stage 不得自行發明重試邏輯。

## 6. 各階段規格

### 6.1 收稿（工作樹 ①）

- worker 每分鐘找出 `enabled ∧ next_poll_at <= now()` 的來源輪詢；每來源獨立失敗，不互相影響。
- 解析沿用舊 `feeds.py` 的格式支援與 SSRF-safe client（`extraction.py`），可搬移重構到 `newsroom/` 下，不得放寬 SSRF、robots.txt、大小上限。
- 去重：`url_hash` 唯一；同窗內標題完全相同者略過。不再做標題字詞重疊去重（交給事件分群）。
- 全文：`full_text_in_feed` 來源直接取 feed 內容；其他抓取原文。品質檢查（長度下限、導覽字詞比例、與標題相關性）不合格 → `rejected`。
- 健康：連續失敗累計；某來源連續失敗且距離 `last_success_at` 超過 6 小時 → Slack 通知一次（恢復後重置）。
- 每日 03:00 清除 30 天前文章的 `body`（`body_status = purged`）。
- 後台來源管理 API 的 service 層（新增、修改、停用、健康查詢）。
- 手動 URL：建立 `manual` 來源的文章，`edition_date` 由管理員指定（預設為正在審核的版次日期），走完整抓取 → embedding → 初篩。
- 手動貼全文：寫入 `body`、`body_source = manual`、`body_status = ok`，並把所屬事件若為 `needs_body` 改回可分析。

### 6.2 初篩與分群（工作樹 ②）

- embedding：標題 + feed 摘要（+ 正文前 1,500 字元，若有）。
- 初篩呼叫（DeepSeek，JSON mode，temperature 0）輸入：文章標題、來源、摘要／正文前段、**同 `edition_date` 窗內** kNN 最相近的
  5 個事件（以文章 embedding 對窗內已歸屬事件的文章做 cosine 搜尋，取不重複事件，附事件 `working_title` 與 2 則代表標題）。
- 輸出（`newsroom/contracts.py::TriageResult`）：`relevant`、`topic`、`market_scores`、
  `event`：`{"match": "<event_id>"}` 或 `{"new": "<working_title>"}`。不相關文章不歸屬事件。
- 併發：同一 `edition_date` 的「事件歸屬寫入」以 advisory lock 序列化，避免兩篇同事件文章同時開新事件；
  LLM 呼叫本身可並行，寫入時若選擇「新事件」須在鎖內再做一次 kNN（相似度 ≥ 門檻則改歸屬既有事件）。
- 事件服務：`merge_events(target, sources)`、`split_event(event, article_ids)`（被拆出的文章形成新事件，`created_by = split`），
  合併／拆分後相關事件的 `analysis_status` 重設為 `pending`（若已在版次中）。

### 6.3 組稿、分析、翻譯、發布（工作樹 ③）

**組稿（08:00，由統一編排 daily routine 的 `newsroom_assemble` function 觸發，冪等）**

1. 等待窗內初篩（§3）。
2. 每市場計算事件粗分：`max(article.market_scores[m] × source.weight)` + 多來源加分（每多一個不同來源 +5，最多 +20）。
3. 只有 `relevant` 且含至少一篇 `body_status = ok` 文章的事件可入選；取粗分前 30 名送精選。
4. 精選呼叫（每市場一次）：輸入事件 id、working title、最多 5 則報導標題與來源、報導家數；輸出每個事件的 `stars`（1–5）。
   評分準則沿用並改寫 `news/prompts/selection_criteria.txt` 的精神（市場相關性、可信度、重要性）。
5. 套 D8 配額（程式）。精選失敗（重試耗盡）→ `selection_mode = fallback`：依粗分取前 5 名、`stars = null`，Slack 通知。
6. 建立 `draft` 版次與項目，事件 `analysis_status = pending`、項目 `why_status = pending`。
7. 全部市場組稿完成後 Slack 通知「草稿完成」（各市場則數、5 星事件標題、後台連結）。

**深度分析（worker）**

- 事件層級一次呼叫：輸入事件內最多 5 篇有全文的文章（依 trust_tier、weight 排序，每篇截 8,000 字元）、事件所在的市場清單。
  輸出 `headline`、`summary`、`related_symbols`、以及每個市場的 `why`。成功後同時寫入事件與各項目的 `why_zh_hant`。
- 事件後來被加入新的市場版時，只對該項目做 `why` 單獨呼叫。
- 繁中寫入時同步以 OpenCC `tw2sp` 產生簡中。
- related_symbols 必須對照站內既有的標的清單（`markets` 模組），對不上的丟棄。

**發布**

- 管理員核准單一市場或全部 → 立即 `published`。
- worker 每分鐘檢查 `draft ∧ now() >= auto_publish_at` → 自動發布（`published_by_user_id = null`）。
- 發布後，項目完成即可見（D15）；`now() >= late_fill_deadline` 時仍未 ready 的項目設 `abandoned_at` 並 Slack 通知。
- 發布時把可見項目的事件 `en_status` 設為 `pending`；之後繁中被修改（digest 改變）則再設為 `pending`。英文翻譯包含事件標題、摘要與各項目的 `why`。

### 6.4 後台（工作樹 ④）

- 管理 API（`/api/admin/newsroom/...`）與頁面，涵蓋 D19 全部功能＋來源管理＋健康狀態。
- 審核頁依市場呈現項目（星等、標題、摘要、為何重要、相關標的、來源列表、原文對照），以及「候選事件」清單（粗分排序，可加入）。
- 事件與文章的狀態（缺全文、分析失敗、未初篩）要清楚標示；每個動作寫 `newsroom_edit_log`。
- 「重新分析」＝把事件 `analysis_status` 設回 `pending`（及其項目 `why_status`）。
- 遵守 `AGENTS.md` 的 Dashboard UI 規則（Tailwind、skeleton loading、i18n）。

### 6.5 讀者端（工作樹 ⑤）

- 公開 API：依市場與語系回傳最近一個 `published` 且有可見項目的版次（`edition_date <= 今天`），回應含 `edition_date` 與 `is_today`。
- 前端：每則顯示標題、事實摘要、「為何重要」、相關標的（連到站內儀表板）、來源列表；非今日版次顯示明確的日期提示。
- 沿用首頁 `Promise.allSettled` 的容錯；遵守 loading／skeleton 規則與 i18n。

## 7. 設定（環境變數，前綴 `DAILY_INSIGHTS_`）

| 變數                                                                                                    | 用途                                         |
| ------------------------------------------------------------------------------------------------------- | -------------------------------------------- |
| `NEWSROOM_ENABLED`                                                                                      | 啟用 news worker 與 08:00 組稿（預設 false） |
| `NEWSROOM_LLM_BASE_URL`、`NEWSROOM_LLM_API_KEY`                                                         | DeepSeek（OpenAI-compatible）                |
| `NEWSROOM_TRIAGE_MODEL`、`NEWSROOM_EDITOR_MODEL`、`NEWSROOM_ANALYSIS_MODEL`、`NEWSROOM_TRANSLATE_MODEL` | 各階段 model（預設 `deepseek-chat`）         |
| `NEWSROOM_EMBEDDING_BASE_URL`、`NEWSROOM_EMBEDDING_API_KEY`、`NEWSROOM_EMBEDDING_MODEL`                 | embedding（預設 `text-embedding-3-small`）   |
| `NEWSROOM_SLACK_WEBHOOK_URL`                                                                            | 通知；未設定時只寫 log                       |
| `NEWSROOM_ADMIN_BASE_URL`                                                                               | Slack 訊息中的後台連結                       |

## 8. 工作樹分工與檔案所有權

為避免衝突，每個工作樹只修改自己擁有的檔案；需要動到他人檔案或 schema 時，先回報協調者。

| 工作樹         | 擁有的檔案（`modules/newsroom/` 下，除非另註）                                                                                                                                                               |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 地基（協調者） | `models.py`、`contracts.py`、`queue.py`、`providers.py`、`notifier.py`、`clock.py`、`editlog.py`、`worker.py`（框架）、migration `20261001_0031`、`compose.yaml`、`core/config.py`、`web/app.py`（路由掛載） |
| ① 收稿         | `ingestion/`（`register()`：輪詢、解析、全文、品質檢查、清除）、`sources_service.py`、來源 seed migration（唯一允許新增的 migration，`down_revision = "20261001_0031"`）                                     |
| ② 初篩與分群   | `triage.py`（`register()`）、`clustering.py`、`events_service.py`                                                                                                                                            |
| ③ 組稿與分析   | `assembly.py`、`analysis.py`、`translation.py`、`publishing.py`（各自的 `register()`）、`prompts/`、`orchestration/registry.py` 與 `functions.py` 中 newsroom 相關的接線                                     |
| ④ 後台         | `admin_api.py`（`/api/admin/newsroom`）、`apps/web` 後台頁面與元件、後台 i18n                                                                                                                                |
| ⑤ 讀者端       | `public_api.py`（`/api/newsroom`）、`apps/web` 讀者端元件、讀者端 i18n                                                                                                                                       |

- 地基已在各擁有者的檔案放好 stub：`register(runtime) -> Registration` 回傳空的註冊、跨工作樹呼叫的函式以 `NotImplementedError`
  佔位（`sources_service.py`、`events_service.py`、`publishing.py`、`assembly.py`）。擁有者**保留簽名**填入實作；
  確實需要改簽名時先回報協調者，因為④會依這些簽名開發。
- `worker.py::collect_registrations` 會匯入每個擁有者的 `register`；各工作樹不修改 `worker.py`。
- 後台只透過上述函式與資料列狀態變更（`queue.enqueue`）操作，不直接呼叫 LLM。管理員動作一律以 `editlog.record_edit` 記錄。
- 需要新增 Python 依賴或修改 schema 時，先回報協調者，避免 `pyproject.toml`、`uv.lock` 與 migration 鏈衝突（①的 seed migration 除外）。
- `packages/api-client` 的產生檔由④⑤各自重新產生；合併衝突由協調者以重新產生解決。
- i18n 都在 `apps/web/src/lib/i18n.ts`：④的 key 以 `newsroomAdmin` 為前綴、⑤以 `newsroom` 為前綴（不得與 `newsroomAdmin` 混用），分段落新增以降低衝突。
- 測試資料庫需要 pgvector：`scripts/test-with-postgres.sh` 已改用 `pgvector/pgvector:pg17`。
- 所有分支以 `feat/news-pipeline-v2` 為 base，PR 一律開 draft 並指向該分支。

## 9. 切換階段（協調者，最後執行）

- 舊 `news_editions`／`news_items`／`news_presentations` 的歷史資料搬到 `newsroom_*`（以 `legacy` 標記的事件與項目，三語內容直接帶入）。
- `compose.production.yaml` 新增 `newsroom-worker`（地基只加入本機 `compose.yaml` 的 `newsroom` profile，避免生產部署缺少 secrets）。
- 移除舊管線：`modules/news` 的產生與復原程式、`orchestration/news_functions.py`、舊資料表、復原面板、舊後台頁、`run_daily_news_scheduler.py`。
- 文件：以本規格為基礎改寫 `docs/architecture/daily-news.md`、更新 runbooks。
