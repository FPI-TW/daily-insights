# Twelve Data 三市場晨報計劃

狀態：第一波核心功能已完成本機實作與 credentialed provider probe；manifest
核准、授權證據、資料庫整合測試與 production deployment 尚未完成。本文件取代
[`five-market-morning-report-baseline.md`](five-market-morning-report-baseline.md)
作為現行第一波晨報的市場、來源與驗收政策；舊文件只保留為歷史決策紀錄。

## 1. 決策摘要

第一波正式晨報固定為下列三個市場，順序不得由 runtime 自動改變：

1. `global_macro_bonds`：宏觀／債券
2. `crypto`：加密貨幣
3. `us_equity`：美股

Twelve Data 是這三個市場暫時唯一的 raw-data provider。Runtime 不得在請求失敗、
資料過期或缺欄位時自動改用 FinDB、Yahoo、FRED、CMC 或其他來源，也不得把不同
provider 的歷史序列接在一起。

`tw_equity` 與 `tw_index_derivatives` 不在第一波資料與 publication 範圍。現有
元件、三語翻譯、mock fixture 與 route code 保留，但不出現在晨報 tab 或報告列表；
登入使用者直接輸入既有 URL 時仍可預覽，頁面必須清楚標示「尚未上線／示意資料」，
不得呼叫或冒充正式 publication API。

`forex`、`hk_equity`、`cn_equity` 仍屬八市場 catalog 與 organization market
policy，但不納入本次 launch manifest。第一版沿用目前簡版 UI；完整 `/reference`
卡片矩陣、AI 摘要及未被簡版 UI 消費的資料不在本次範圍。

## 2. Source gate 與 launch manifest

### Twelve Data 唯讀 probe

Feature implementation 前，以 production 將使用的授權與 credential 執行唯讀
probe。Probe 只保存不含 secret 的對帳證據，不保存完整 response 或 raw rows。
每個候選 block 必須確認：

- 合約允許本產品的客戶端外部展示、必要 attribution 及涵蓋市場；
- exact endpoint、symbol、asset type 與必要欄位；
- API credit weight、分鐘額度、batch 行為及 daily limit；
- 可取得的歷史深度、排序、pagination 與資料修正語義；
- 時區、日界、交易日、資料延遲、dataset-level `as_of` 與 freshness；
- 價格、殖利率、比率、成交量等欄位的單位與 decimal 口徑。

Metadata catalog 出現某個 symbol 不代表其 price endpoint、歷史、方案權限或展示授權
已通過。每個正式市場至少要有一個完整通過 probe 的 block；任一市場為零即為
launch no-go，不得用 mock、ETF proxy、近似指數或不同語意序列補足。

### Versioned launch manifest

Probe 通過後建立 immutable、versioned manifest。每個 included block 固定記錄：

- market 與 block order；
- application dataset key、Twelve Data endpoint 與 exact symbols；
- required fields/rows/series、history、freshness 與 atomicity；
- formula version、lookback、display/calculation basis、unit 與 precision；
- `zh-hant`、`zh-hans`、`en` labels、units、caveat 與 attribution。

Twelve Data 無法完整供應的 block 不進 manifest，也不在正式 UI 保留空版位。
Manifest 一旦發布，runtime 不得因每日資料情況增加或刪除 block；已納入 block 的每日
失敗必須保留固定版位並標記 `missing` 或 `error`。這與「未納入第一版」是不同狀態。

首輪 probe 依現有簡版 UI 評估下列候選能力：

- 宏觀：商品快照與商品日線標準化表現；美債期限與殖利率曲線只有在 exact bond
  symbols 與全部必要期限通過時才可整張納入。
- 加密貨幣：BTC、ETH、SOL、XRP、ADA 報價、期間變動與 Base-100 表現。
- 美股：exact 主要指數；US market movers 可定義為固定數量的當日漲幅與跌幅排行。

信用債、FedWatch、CMC/CNN 情緒與 global metrics、永續合約 OI、SPX point-in-time
breadth，以及無 exact Twelve Data 對應的 sector index 預設不進第一版 manifest。

### 2026-08-30 credentialed probe 證據

本次以 `apps/api/.env` 的正式 credential 經 server-side transport 執行唯讀 probe；
API key 未放入 URL，未輸出或保存 raw payload、raw rows 或行情數值。只保留 endpoint、
query fingerprint、response digest、fetch/source date、record count、credit header 與
request ID 是否存在等 sanitized metadata。Twelve Data 本次未回傳 request ID。

- `/quote`：`XBR/USD`、`XAU/USD`、`HG1` 各 1 credit 且 adapter 全數通過。
  `XBR/USD` 與 `XAU/USD` 不回傳 `currency`，由請求 symbol 明示的 quote currency
  `USD` 決定單位；`HG1` 回傳 `EUR`。`source_as_of` 由 provider Unix `timestamp`
  轉為 UTC calendar date。另以 `/commodities` catalog 確認 `HG1` 是
  `Copper Spot`，未使用 ETF 或近似序列。
- `/time_series`：`BTC/USD`、`ETH/USD`、`SOL/USD`、`XRP/USD`、`ADA/USD` 以
  `interval=1day`、`order=ASC`、`outputsize=485` 各 1 credit，全數回傳 485 筆並通過
  adapter。日線具有 `datetime/open/high/low/close`，不供 `volume`；crypto 日線日期
  依 provider time-series contract 採 UTC。五個 symbol 的 `currency_quote` 均為
  provider label `US Dollar`，adapter 會對照 manifest 的 `USD` unit 並拒絕缺值或漂移。
- `/time_series`：`XBR/USD` 與 `XAU/USD` 以 `interval=1day`、`order=ASC`、
  `outputsize=500` 各自通過歷史探測；provider metadata asset type 分別為
  `Energy Resource` 與 `Precious Metal`，quote currency 均為 `US Dollar`。兩個
  date-only 日線以 provider calendar date 對齊，可取得至少 30 個共同完成日期，故
  納入獨立的 Brent／Gold Base-100 series dataset。此 date-only 合約沒有證明 UTC 或
  exchange timezone，runtime 不作此類聲稱。
- Treasury 候選 `US3M`、`US5Y`、`US10Y`、`US30Y` 不通過 exact history probe；只有
  `US2Y` 可用，不能形成完整曲線，因此不納入 Treasury／bond curve。`HG1` 的
  `/time_series` metadata 識別為 FSX 的 EUR Common Stock，不符合商品歷史合約，故不
  納入歷史 series；既有 `/quote` 商品快照 contract 不變。
- `/market_movers/stocks`：`country=USA`、`outputsize=2` 的 `gainers` 與 `losers`
  各 100 credits，兩方向均回傳 2 筆並通過 adapter。`datetime` 是無 offset 的美股
  market-local datetime，本版只取其 market-local calendar date 作 `source_as_of`。
- Probe 已證明三個 endpoint 的 credential 權限、實際欄位、上述 credit weight 與本版
  所需歷史深度。帳戶分鐘額度、daily limit、外部展示授權及 attribution 仍須由正式
  dashboard／合約仍需另行留存證據；runtime 不以 manifest 核准狀態或外部 hash 作為
  啟動條件。

欄位與時區語義以 Twelve Data 官方
[API documentation](https://twelvedata.com/docs/advanced) 與
[symbol reference guidance](https://support.twelvedata.com/en/articles/5620513-how-to-find-all-available-symbols-at-twelve-data)
交叉確認。

## 3. 實作順序

### A. Provider 與設定

1. 新增 Twelve Data transport 與 adapter，以 application-owned DTO 隔離 upstream
   schema，所有價格、金額、殖利率與比率使用 `Decimal`。
2. 實作有限 timeout、bounded concurrency、429 `Retry-After`、有限重試、credit
   header 監控及 sanitized error mapping；晨報使用 REST batch，不引入 WebSocket。
3. API key 只存在 server runtime secret。Query fingerprint、log、error、database
   及 browser bundle 都不得包含 credential。
4. Production startup 依 active launch manifest 驗證 Twelve Data 設定；FinDB adapter
   保留，但 FinDB key 不再是本波三市場的啟動條件。

### B. Pipeline、publication 與 API

1. 沿用現有 database claim、lease、attempt fencing、idempotency 與 immutable
   revision foundation。
2. Publication contract 遷移為固定 blocks，支援
   `BlockStatus = ok | missing | error`、
   `ReportStatus = complete | partial | unavailable`，block 與 publication 的
   `source_as_of` 均可為 `null`。
3. 每個 `Asia/Taipei` edition date 都出版，包括全來源失敗的 `unavailable`。
   相同 input 與 failure state 重跑為 no-op；內容或失敗狀態改變時新增 revision。
4. `GET /api/reports` 只回三市場輕量摘要；
   `GET /api/reports/{market_code}/latest` 回完整固定 blocks、status、source date、
   caveat、revision 與指定 locale presentation。
5. API 繼續套用 organization market policy；八市場 catalog 與 admin policy 不因
   launch manifest 縮減而刪除。

### C. Web

1. 將現行市場集合拆成正式 `launchMarketCodes` 與只供直接 URL 預覽的
   `previewMarketCodes`，避免移除台灣 mock code 或放寬 route typing。
2. Tab 與報告列表只使用 `launchMarketCodes`；三語「五市場晨報」改為「三市場晨報」。
3. 三個正式市場 loader 改讀 API，不再顯示硬編 mock 數值。
4. 台股與台指期直接 URL 使用既有 mock fixture，顯示醒目的未上線／示意警告，且不
   呼叫正式 publication API。
5. 保留 initial skeleton、`role="status"`、`aria-live`、error/retry、partial、
   unavailable、null `—` 與 chart gap 行為。

### D. Deployment 與未來 FinDB cutover

1. Scheduler 每日依既有 `07:00 Asia/Taipei` 目標執行，支援 manual rerun、
   heartbeat 與 terminal-state check。
2. Production preflight 驗證 Twelve Data credential、manifest version、provider
   readiness 與三市場終態；provider 暫時失敗不應使 API readiness 整體失敗。
3. 監控 API credits、429、provider latency/error rate、source freshness、pipeline
   terminal state 與 publication 缺漏。
4. FinDB 就緒後先以新 manifest version 進行 bounded dual-run 與對帳，再明確
   cutover；不得在 runtime 自動 fallback 或把兩個 provider 的歷史接成一條序列。

## 4. 驗收項目

### 資料來源

- [x] 已使用正式 Twelve Data credential 完成唯讀 probe，且未保存 raw payload 或
      secret。
- [ ] 已確認合約允許客戶端外部展示、必要 attribution 與使用市場範圍。
- [ ] 已確認每個 endpoint 的可用權限、credit weight、分鐘額度與歷史深度。
- [x] 每個納入 block 都有 exact endpoint、symbol、欄位、單位、時區、日界與
      freshness 證據。
- [x] 每個正式市場至少有一個完整通過 probe 的 block，否則該市場為 launch no-go。
- [x] 不存在 Yahoo、FRED、CMC、FinDB 或其他資料源的執行期 fallback。
- [x] 不以 ETF、近似指數或不同語意序列替代缺失資料。

### Launch manifest

- [x] Manifest 僅包含 `global_macro_bonds`、`crypto`、`us_equity`。
- [x] 每個 block 的順序、required fields、歷史窗、公式、單位、精度與三語標籤均已
      凍結並版本化。
- [x] Twelve Data 無法完整供應的 block 未出現在 manifest 或正式 UI。
- [x] Manifest 變更會產生新版本，不會在 runtime 動態增加或刪除 block。
- [x] 已明確區分「未納入功能」與「已納入但當日來源失敗」。

### Provider 與安全

- [x] Twelve Data adapter 使用 application-owned DTO，reports 模組不依賴 provider
      schema。
- [x] 金額、價格、殖利率與比率使用 `Decimal`，不轉為 binary float。
- [x] 401/403、429、5xx、timeout、非法欄位與不合法數值均有明確錯誤分類。
- [x] 429 遵守 `Retry-After`，重試次數、timeout 與併發數均有上限。
- [x] API key 只存在 server runtime secret，不出現在 URL log、錯誤、資料庫或前端
      bundle。
- [x] Provenance 僅保存 provider/application dataset key、contract version、不含
      secret 的 endpoint/query fingerprint、fetch time、source date、record count、
      digest、request ID 與 sanitized error。
- [x] PostgreSQL 不保存 Twelve Data raw response 或 raw rows。

### Publication 與 API

- [x] 支援 `BlockStatus = ok | missing | error`。
- [x] 支援 `ReportStatus = complete | partial | unavailable`。
- [x] Block 與 publication 的 `source_as_of` 可為 `null`。
- [ ] 每個台北日期都建立 edition，包括來源完全失敗的 `unavailable` edition。
- [ ] 相同輸入與失敗狀態重跑為 no-op；內容或失敗狀態改變時新增 immutable revision。
- [x] `GET /api/reports` 只回三市場輕量摘要。
- [x] `GET /api/reports/{market_code}/latest` 回完整固定 blocks、status、source date、
      caveat 與 revision。
- [x] API 繼續套用 organization market policy；隱藏的正式市場直接存取回 404。
- [x] 八市場 catalog 與 admin market policy 不因第一波縮減而刪除。

### Web UI

- [x] 晨報 tab 只顯示宏觀／債券、加密貨幣、美股。
- [x] 晨報列表只顯示三個正式市場。
- [x] 「五市場晨報」已在三語文案中改為「三市場晨報」。
- [x] 台股與台指期元件、翻譯、mock fixture 與 route code 保留。
- [x] 台股與台指期直接 URL 仍可開啟，且清楚標示「尚未上線／示意資料」。
- [x] 台灣預覽頁不呼叫正式 publication API，也不顯示成正式報告。
- [x] 三個正式市場不再顯示硬編 mock 數值。
- [x] 初次載入具有 skeleton、`role="status"` 與 `aria-live`。
- [x] `partial`、`unavailable`、`error`、`null` 與 chart gap 均有明確且三語一致的
      呈現。

### 測試與上線

- [x] Provider contract tests 覆蓋正常回應、缺欄位、非法 decimal、認證失敗、
      限流、timeout 與重試。
- [x] Manifest tests 驗證三市場、固定 block order、原子欄位規則與排除 block。
- [ ] Pipeline tests 覆蓋 status、nullable source date、no-op、revision 與來源失敗。
- [x] Web tests 驗證三個 tab、台灣 tab 隱藏、台灣 URL 預覽及示意警告。
- [ ] E2E 覆蓋登入、三市場列表／詳情、租戶 market policy 與 unavailable edition。
- [x] 已執行 API tests、Vitest、Playwright、`type:check`、`lint`、`lint:check`、
      `format`、`format:check`。
- [x] Production preflight 驗證 Twelve Data 設定，不再將 FinDB key 視為三市場啟動
      必要條件。
- [ ] 上線後監控 API credit 餘額、429、provider latency、來源 freshness、每日
      pipeline terminal state 與 publication 缺漏。

## 5. 假設與排除範圍

- Twelve Data 授權已涵蓋本產品的客戶端資料展示；正式部署前仍須以合約與
  credentialed probe 留存證據。若合約不涵蓋外部展示，launch no-go，不以技術實作
  規避授權限制。
- 第一版不包含完整 `/reference` 卡片、AI 摘要、外匯、港股、陸股或台灣市場資料
  接線。
- `/reference` 只作需求與歷史調查證據，本計劃不修改其中檔案。
- 本文件定義 target state；未勾選的驗收項目不得被描述成已完成能力。
