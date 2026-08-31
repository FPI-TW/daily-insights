# Phase 2 資料來源與結構化報告

狀態：基礎能力已實作；現行三市場晨報的 source gate 與遷移後 publication 行為見
[`twelve-data-three-market-morning-report-plan.md`](twelve-data-three-market-morning-report-plan.md)。
本文件區分現行 foundation 與尚未實作的 feature target，不把 target 宣稱為現況。

## 本階段交付邊界

- 以 application-owned contract 隔離 Twelve Data 與 FinDB，不讓 provider schema 進入
  reports 模組。
- 原始 provider response 只存在記憶體；PostgreSQL 僅保存同步執行狀態、
  provenance、checksum、來源時間及應用程式加工後的 publication。
- 每日 pipeline 使用穩定 idempotency key、資料庫 claim、lease 及
  attempt fencing token，讓重試、lease 接手與程序重啟不會讓舊 worker
  重複或覆蓋發布。
- 現行 foundation 在任一必要輸入缺漏、契約錯誤或逾時時不建立新 publication；
  既有最後成功 publication 仍可讀取並標示 stale。這是目前 fail-closed/LKG
  實作，不是三市場晨報遷移後的接受規則。
- 三市場晨報 feature target 改由現行計劃定義：每一 edition 都有 publication，
  可為 `complete`、`partial` 或 `unavailable`，並保留 failed source-run evidence；
  included blocks 不因當日失敗而被移除，缺值以 nullable cells/points 表達。
- Publication 是 immutable revision，包含 locale-neutral values/charts 與
  `zh-hant`、`zh-hans`、`en` 三語 presentation。
- 客戶報告 API 從已登入 membership 取得 organization，並在伺服器端套用
  市場可見政策。隱藏市場不會出現在列表，直接存取亦回傳不存在。

本階段不包含：

- 自行發明或逆向推導指標、公式、分數及投資建議。
- 未確認的 editorial approval、機器翻譯或人工審稿流程。
- 把 Twelve Data 或 FinDB raw row、response body 或完整 payload 保存至本服務。
- 宣稱 Twelve Data 已完整支援三市場或任一 block；exact production capability、
  授權、history、credits 與 freshness 必須先通過現行計劃的唯讀 probe。
- production scheduler、CloudWatch alarm 或正式環境 secret 配置。

## FinDB 目前契約

2026-07-24 取得的公開 OpenAPI：

- 文件：<https://findb.tingfong.com/docs>
- OpenAPI：<https://findb.tingfong.com/openapi.json>
- API version：`0.1.0`
- 調查時 OpenAPI SHA-256：
  `a5f299b27a8533de5c5e13cfec748172ab531e1dc7d32c3a1391dac5b4ac76a1`
- Serve API 使用 `X-API-Key`。
- 已文件化 instruments、EOD、macro、bonds、futures、corporate actions、
  calendar 與 lookup 等唯讀端點。
- 金額、價格、殖利率與觀測值以 decimal string 表示；adapter 必須使用
  `Decimal`，不可轉成 binary float。
- Provider response 可增加欄位；必要欄位移除、型別改變或不合法 decimal
  必須視為 contract failure。

目前只有文件範例能確認 `US` 與 `CRYPTO`。其他市場代碼、時區／日界線、
修訂與歷史回補語義、rate limit、穩定排序及 dataset-level `as_of` 都尚未
確認。OpenAPI 目前也沒有 options endpoint，因此台指選擇權仍是明確缺口。

2026-08-20 的 pre-production observation 不是 production probe，也不是接受
證據：public OpenAPI version 為 `0.1.0`，觀察到的 SHA-256 是
`8af97480a020e2ab120107dd6d622e2fe2d8746f2b418e88682ea10dba49c9de`，而 repository
pin 為 `a5f299b27a8533de5c5e13cfec748172ab531e1dc7d32c3a1391dac5b4ac76a1`；
configured key 回傳 `403`。正式 release、新 Environment key 及 read-only
production probe 仍是 go/no-go 前置條件。

## 資料與 publication invariants

Source provenance 至少保存：

- provider 與 application dataset key；
- provider contract version/hash；
- 不含 secret 的 endpoint/query fingerprint；
- fetch time、日級 source `as_of`、record count、normalized checksum；
- provider request ID（若有）；
- sanitized error code/detail。

禁止 source metadata table 出現 `raw_payload`、`response_body`、`rows` 等
可用來持久複製 provider raw dataset 的欄位。

Publication 至少保存：

- stable report key、market、edition date 及 revision；
- derivation version 與 content schema version；
- input digest、日級 source `as_of`、published time；
- locale-neutral derived content；
- 完整三語 presentation；
- 產生此 publication 的所有成功 source run 關聯。

目前 publication schema 要求 publication-level `source_as_of` 非 null，且
現行 payload 已有 metrics/charts；這是 foundation 現況。三市場 feature PR
必須依現行計劃遷移為 block/publication `source_as_of` 可為 null，並加入
block/report status 與 caveat，不能把現行 non-null/LKG contract 當成新晨報規則。

相同 idempotency key 的重試必須回到同一 logical pipeline run。相同 report、
market、edition、revision 只能產生一個 publication。已發布內容及其
source-run 關聯由 PostgreSQL trigger 禁止 `UPDATE` 與 `DELETE`；修正必須新增
revision。

`source_as_of` 使用 `date`，不自行替來源補上未確認的時區或日界線；
`fetched_at`、`published_at` 與 pipeline 執行時間則使用帶時區的 timestamp。

## 待產品與資料團隊確認

以下項目阻擋三市場晨報 feature/deploy acceptance，但不阻擋既有 adapter 與
pipeline foundation：

1. Twelve Data 合約的外部展示、attribution、涵蓋市場與 endpoint 使用權。
2. 三市場 manifest 的 exact endpoint、symbol、required fields、history、credits、
   time zone、日界與 freshness；文件或 catalog 能力不能替代 credentialed probe。
3. 每個指標與圖表的公式、lookback、缺值與修訂規則、derivation version。
4. 其他報告產品的分類及發布頻率是否沿用 daily/weekly/research/AI news；三市場
   晨報已依 accepted baseline 固定每日 `08:00 Asia/Taipei`，不在此待確認項目。
5. 三語 labels/presentation 的完整 manifest；AI summary 不在第一波 contract。
6. 各市場交易日曆、時區、daily cutoff 與 freshness SLO。
7. Twelve Data rate limit、429/Retry-After、資料修正及歷史回補契約。

在上述決策與 production probe 完成前，generic contract 可用合成／sanitized
fixture 驗證，但不得視為正式客戶報告內容或 block 已通過 source gate。Production
scheduler、API/Web UI 遷移、deploy restart 與三市場 terminal-state check
屬後續 feature/deploy PR；本文件 PR 未執行。
