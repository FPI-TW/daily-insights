# 五市場晨報基準同步

狀態：歷史基線，已由
[`twelve-data-three-market-morning-report-plan.md`](twelve-data-three-market-morning-report-plan.md)
取代，不再是現行第一波 source policy。本文件保留當時的五市場、FinDB-only 與
54 張卡片調查證據，不代表目前 launch scope。

本基線 PR 確認後，才可開始 feature PR；feature PR 開始前還必須通過一次
production FinDB 唯讀 probe。沒有這兩項證據，不得以示意資料或代理來源提前上線。

## 1. 已接受的邊界與架構

### 架構決定

以下現行架構保留並視為本功能的部署基礎：

- FastAPI API、TanStack Start Web，以及 PostgreSQL/RDS 保存 immutable
  publication revision。
- GitHub Actions 驗證、建置並發布 immutable image；部署以 GHCR digest 鎖定
  image，執行於 EC2 containers。
- `/reference` 只作需求證據與 BE 編號來源，保持不變；本基線不修改任何
  `/reference` 檔案。

### 第一波市場與來源

第一波固定為下列五個 market order：

1. `global_macro_bonds`：宏觀／債券
2. `crypto`：加密貨幣
3. `us_equity`：美股
4. `tw_equity`：台股
5. `tw_index_derivatives`：台指期貨／選擇權

FX、港股、陸股不在第一波。既有八市場 catalog 仍是長期產品範圍，並不代表
這三個市場可在本次 launch manifest 或 runtime 自動加入。

本晨報只使用 FinDB。AI 摘要以及直接呼叫 Twelve Data、FinLab、Yahoo、FRED、
CMC、TWSE、TAIFEX、CME、BBG 等來源均不在第一波 contract；不得用代理來源、
快取、截圖或人工輸入補卡片。

下列舊模式不是本基線的資料或架構選項：

- `mr_*` legacy tables、durable raw snapshot/rows、`data/{market}/latest.json`；
- screenshots/OCR；
- 舊 analyst pipeline、AI summary。

### 前置條件與 probe gate

Production FinDB release 與新的 GitHub Environment key 都是必要前置條件。現有
key 已失效，且 public OpenAPI hash 已與 repository pin 漂移；因此必須完成唯讀
production probe，才能判斷任何卡片是否能進 manifest。

2026-08-20 的 pre-production observation 只作非接受證據，絕不稱為 production
probe：

- public OpenAPI version：`0.1.0`；
- observed SHA-256：
  `8af97480a020e2ab120107dd6d622e2fe2d8746f2b418e88682ea10dba49c9de`；
- repository pin：
  `a5f299b27a8533de5c5e13cfec748172ab531e1dc7d32c3a1391dac5b4ac76a1`；
- configured key 回應 HTTP `403`。

Probe 必須以新的 production Environment key、唯讀 request、實際 production
release/contract 完成，並保存不含 secret 的 endpoint、symbol、history、
freshness、unit、formula/window 對帳證據；不得保存 provider raw payload。

### 原子卡片規則

每一張卡片的每個 required field、row、series、history、unit 與 freshness 都
必須由 FinDB 保證。任何一項缺漏都排除整張卡片；不得 pruning、換代理來源、
縮短視窗、以 `0` 冒充 `null` 或只發布能算出的子列。

狀態定義如下：

- `候選`：所有 production probe requirements 已通過，且可納入 versioned
  launch manifest。
- `待 production probe`：FinDB 文件能力看似可行，但 production 的 exact
  code/symbol/history/freshness 尚未驗證。
- `排除`：與 scope、來源 contract 或架構禁令存在硬衝突。

目前 54 筆矩陣的總數固定為：`候選 0`、`待 production probe 23`、`排除 31`。
每個第一波 market 在 probe 後都必須至少有一張 `候選`；任一市場為零即
launch no-go，回到 scope/source discussion，不製造假資料。

## 2. 54 筆 block/card baseline matrix

矩陣每個 row 只出現一次。BE ID 取自 `/reference/api`；HTML-only 項目沒有
backend BE task，明列「無」而不虛構 ID。`待 production probe` 不是已通過，
`排除` 也不是可用的降級卡片。

### 宏觀／債券（11）

| BE ID  | block/card | 狀態                | 原子理由                                                                                                 |
| ------ | ---------- | ------------------- | -------------------------------------------------------------------------------------------------------- |
| BE-101 | `tb-cmd`   | 待 production probe | FinDB exact five commodity symbols、日頻至少一年、銅 `cent/lb` 單位與 freshness 尚未在 production 對帳。 |
| BE-101 | `c-ratio`  | 待 production probe | 油金比／銅金比的同日五檔輸入、分母處理與完整 history 尚未由 production FinDB 保證。                      |
| BE-102 | `tb-yld`   | 待 production probe | `DGS3MO/DGS2/DGS5/DGS10/DGS30`、至少三年（10Y 至少十年）及百分點／bp 口徑尚未驗證。                      |
| BE-102 | `c-curve`  | 待 production probe | 殖利率曲線所有期限同一資料日、排序與完整歷史尚未通過 production probe。                                  |
| BE-103 | `c-move`   | 待 production probe | FinDB 是否提供 MOVE exact series、必要歷史與每日 freshness 尚未確認；不得以 VIX 代理。                   |
| BE-103 | `c-infl`   | 待 production probe | PCE 與 2Y breakeven 的雙序列、頻率、歷史及同日語義尚未由 production FinDB 保證。                         |
| BE-104 | `c-life`   | 待 production probe | `DGS10` 完整 EMA warm-up、序列歷史與日界線尚未 production 對帳。                                         |
| BE-105 | `c-gauge`  | 待 production probe | `T10Y2Y/T10Y3M` 至少 260 筆尚未驗證；HTML 另要求 SP500 correlation，BE-105 未完整規格化，不能默認接受。  |
| BE-106 | `c-credit` | 待 production probe | Aaa/Baa/HY/EM 所有 OAS/yield series、≥750 日所需歷史與 unit 尚未驗證。                                   |
| BE-106 | `tb-cn`    | 待 production probe | 三檔中資美元債 exact code、歷史、來源時間與完整 row 尚未由 FinDB production release 保證。               |
| BE-107 | `tb-fed`   | 排除                | FedWatch 是 CME／截圖型需求，與 FinDB-only 及禁止 screenshots/OCR 的 contract 衝突。                     |

### 加密貨幣（9）

| BE ID  | block/card      | 狀態                | 原子理由                                                                                         |
| ------ | --------------- | ------------------- | ------------------------------------------------------------------------------------------------ |
| BE-301 | `tb-coin`       | 待 production probe | BTC/ETH/XRP/SOL/ADA exact symbols、UTC 日界、價格欄與至少 485 日歷史尚未 production 對帳。       |
| BE-301 | `c-coins`       | 待 production probe | 五幣完整 Base-100 history、共同起點、視窗與 unit 尚未由 FinDB 保證。                             |
| BE-302 | `c-fg`          | 排除                | 需求依賴 CMC／alternative.me 與跨序列切源，非 FinDB-only；不得把不同指標當降級。                 |
| BE-303 | `kpi_dominance` | 排除                | 需求依賴 CMC global metrics；直接 provider 與 FinDB-only scope 衝突。                            |
| BE-303 | `kpi_mktcap`    | 排除                | 需求依賴 CMC global metrics；FinDB 未形成可接受的 exact source contract。                        |
| BE-303 | `kpi_vol24h`    | 排除                | 需求依賴 CMC global metrics；代理或部分欄位會違反原子卡片規則。                                  |
| BE-304 | `c-flow`        | 排除                | 需求依賴 CMC perpetual OI endpoint，且其 provider-specific 欄位不能由 FinDB-only contract 保證。 |
| BE-305 | `c-alt`         | 排除                | 需求依賴 CMC 官方 Altcoin Season Index／排除清單；不可自算或改用代理。                           |
| BE-305 | `tb-gain`       | 排除                | 需求依賴 CMC listings／網站榜單對帳，與 FinDB-only 及禁止直接來源衝突。                          |

### 美股（9）

| BE ID      | block/card                | 狀態                | 原子理由                                                                                                           |
| ---------- | ------------------------- | ------------------- | ------------------------------------------------------------------------------------------------------------------ |
| BE-407     | `c-sector`                | 待 production probe | 11 檔 sector exact code（含 `^GSPE`）及完整 history/freshness 尚未由 production FinDB 驗證；不得默認 HTML symbol。 |
| BE-401/408 | `tb-idx`（含 Forward PE） | 排除                | 五大指數與 Forward PE 原要求直接依賴 Yahoo/外部成分資料；四檔 Forward PE（BE-401/408）無完整來源，原子卡片不能拆。 |
| BE-402     | `c-vixfg`                 | 排除                | 需求依賴 FRED VIX 與 CNN F&G 的雙來源／累積快照，非 FinDB-only 且含禁止 durable raw snapshot 依賴。                |
| BE-402     | `sent`                    | 排除                | 情緒三卡依賴 CNN／外部情緒序列，FinDB 無已接受的完整同日 contract。                                                |
| BE-403     | `tb-m7`                   | 排除                | M7 quote/statistics 直接依賴 Twelve Data，且不在 FinDB-only source scope。                                         |
| BE-404     | `c-stock`                 | 排除                | 個股 ticker 動態取自 analyst 觀點並依賴 Twelve Data OHLC，違反固定 manifest 與 FinDB-only。                        |
| BE-405     | `tb-fund`                 | 排除                | profile/earnings/recommendations 等多個 Twelve Data endpoint 的完整涵蓋率未有 FinDB contract。                     |
| BE-406     | `c-spx`                   | 排除                | DataHub 成分快照與逐檔 Twelve Data 歷史、PIT raw snapshot 依賴均與禁止 legacy/durable raw snapshot 衝突。          |
| BE-408     | `snews`                   | 排除                | 個股新聞直接依賴 Twelve Data press releases，且動態個股母體未能由 FinDB 固定保證。                                 |

### 台股（13）

| BE ID          | block/card       | 狀態                | 原子理由                                                                                        |
| -------------- | ---------------- | ------------------- | ----------------------------------------------------------------------------------------------- |
| BE-701         | `tb-idx`         | 待 production probe | 台股指數 exact series、個股 rows、資料日與 freshness 尚未在 production FinDB 對帳。             |
| BE-701         | `c-k`            | 待 production probe | 加權指數 OHLCV、活棒排除、日界與完整 K 線 history 尚未由 FinDB 保證。                           |
| BE-704         | `c-bias`         | 待 production probe | 20/60/120/240 日窗口、圖／欄位公式與完整歷史尚未通過 production probe。                         |
| BE-705         | `c-dist`         | 待 production probe | 上市／上櫃母體、全市場 rows、五桶端點與同日 freshness 尚未由 FinDB 保證。                       |
| BE-706         | `tb-sec`         | 待 production probe | 上市 33／上櫃 22 類股 series、分類 mapping、強弱公式與完整歷史尚未驗證。                        |
| BE-707         | `c-life`         | 待 production probe | 台股生命線所需 OHLC、EMA warm-up、短／長期週期與 unit 尚未 production 對帳。                    |
| BE-708         | `tb-tech`        | 待 production probe | RSI/MACD/KD/MA 全部輸入、Wilder 口徑、公式版本與 required history 尚未驗證。                    |
| BE-702         | `c-chips`        | 排除                | 需求直接依賴 TWSE BFI82U/T86；FinDB-only 無完整三大法人 series，不能只留可算欄位。              |
| BE-702         | `tb-instk`       | 排除                | 需求直接依賴 TWSE T86 個股 rows；非 FinDB-only，且完整欄位無 accepted replacement。             |
| BE-703         | `c-margin`       | 排除                | 需求直接依賴 TWSE MI_MARGN/TWT93U；個股維持率缺成數表，原子卡片不能以大盤部分代替。             |
| BE-703         | `tb-margin`      | 排除                | 四檔門檻家數依賴個股維持率與 TWSE rows；缺任一 required input 即整卡排除。                      |
| BE-709         | 四面向 composite | 排除                | 基本面、籌碼、預期等 required inputs 含非 FinDB／未定義來源；合成分不得跨排除卡片或以代理補齊。 |
| 無（web-only） | HTML-only `heat` | 排除                | 只有 HTML 示意卡、沒有 backend BE/FinDB contract；web-only heat 不得默認成產品資料。            |

### 台指期貨／選擇權（12）

| BE ID          | block/card              | 狀態                | 原子理由                                                                                               |
| -------------- | ----------------------- | ------------------- | ------------------------------------------------------------------------------------------------------ |
| BE-802         | `c-k`                   | 待 production probe | TX/MTX/TMF OHLCV、成交量、OI、短均與 exact history/freshness 尚未由 production FinDB 保證。            |
| BE-804         | `c-bias`                | 待 production probe | 5/20/60/120 日與一年／全歷史位階的完整 series 尚未 production 對帳。                                   |
| BE-808         | `P92`                   | 待 production probe | 五項技術指標、補值後 `[0,100]` 值域、位階端點與 required history 尚未驗證。                            |
| BE-801         | raw snapshot dependency | 排除                | 依賴每日 durable raw snapshot／`mr_raw_snapshot`，直接違反禁止 legacy `mr_*` 與 durable raw rows。     |
| BE-803         | `c-vix`                 | 排除                | 需求依賴 TAIFEX 靜態月檔與下載解析，不是 FinDB-only，且缺檔／redirect 行為無 accepted FinDB contract。 |
| BE-805         | `c-oi`                  | 排除                | 需求依賴 TAIFEX 選擇權法人 OI；當日點加 null 歷史仍不足以滿足原子完整 series。                         |
| BE-806         | `c-top`                 | 排除                | 前十大交易人資料在既有調查中無可保證的完整來源；不能只保留全市場 OI 線。                               |
| BE-807         | `c-mtx`                 | 排除                | TMF 散戶多空與 10MA 依賴特定 TAIFEX／非 FinDB contract；不能以近似商品替代。                           |
| BE-807         | `c-pc`                  | 排除                | P/C ratio 歷史與 10MA 依賴 TAIFEX endpoint；FinDB exact history/freshness 未成立。                     |
| BE-809         | `P104`                  | 排除                | 12 項籌碼輸入中多項為期交所專有統計，另有 60 日 rank；缺任一項即整張 composite 排除。                  |
| 無（web-only） | HTML-only `tb-inst`     | 排除                | 只有 HTML 示意表、沒有 backend BE/FinDB contract；不得把示意列當 production data。                     |
| 無（web-only） | HTML-only `sr`          | 排除                | 選擇權支撐／壓力 HTML 卡沒有 FinDB contract，且 required strike OI 來源衝突。                          |

以上合計：宏觀／債券 `10 待 probe + 1 排除`、加密 `2 + 7`、美股 `1 + 8`、
台股 `7 + 6`、台指期權 `3 + 9`，即 `0 候選 / 23 待 production probe / 31 排除`。
`c-gauge` 的 HTML SP500 correlation、HTML-only `heat`／`tb-inst`／`sr`，以及
`P91`、`P93`–`P103` 均保留為 unresolved requirement conflicts；它們沒有被
默默視為已接受能力。

## 3. Manifest 與未來 publication contract

### Versioned launch manifest

Production launch 前建立 immutable、versioned manifest。manifest 必須凍結：

- 上述五市場的 block order（不得依每日資料動態重排）；
- 每個 block 的 exact FinDB source dataset、code/symbol、required fields/rows/
  series/history 與 freshness；
- `formula_version`、lookback window、calculation/display basis、unit/precision；
- `zh-hant`、`zh-hans`、`en` 的 labels、units、caveat wording 與 policy key。

Runtime 不得依每日資料 add/remove blocks。manifest 內的 included block 每日都
保留版位與 contract；資料失敗只能寫入 null cells/points、狀態與 caveat，不能
刪掉 block 或縮小 required rows。

### Feature PR 的目標行為（目前尚未實作）

feature PR 將把 report payload 遷移為 metric/table/series 的 discriminated
blocks，並提供：

- `BlockStatus = ok | missing | error`；
- `ReportStatus = complete | partial | unavailable`；
- nullable block-level 與 publication-level `source_as_of`，以及 caveat；
- 任一每日來源失敗時，included blocks 保留但 cells/points 可為 `null`；
- 每一 edition 都出版，包括 `unavailable`，並保留 failed source-run evidence；
- immutable revisions、相同 input 與 failure state 的 no-op；資料或 failure state
  改變時新增同日下一 revision，不覆寫舊 publication；
- tenant market policy 與三語 labels/presentation；AI summary 不在本 contract；
- UI 固定 block order、initial loading、null、error、empty 與 chart gaps。

FinDB adapter 只擴充 launch manifest 實際用到的 EOD、macro、bonds、futures、
calendar 與 freshness endpoints；邊界仍使用 `Decimal`、strict schema、明確的
pagination 上限與不含 secret 的 sanitized provenance，且不持久保存 provider
raw rows。共用 calculation layer 只實作 probe 通過並寫入 manifest 的公式：

- 顯示欄按日曆日，計算欄按交易日；
- 週末或休市的顯示值可沿用前收，但必須標示 stale；
- 計算不得補造不存在的交易日；
- 缺值維持 `null`，不得轉成 `0`；
- 樣本不足、除零與不完整 warm-up 必須產生明確 missing/error，而非猜值。

API 與 Web 的 feature target 固定如下：

- `GET /api/reports` 只回輕量報告摘要；
- `GET /api/reports/{market_code}/latest` 回完整 fixed blocks；
- 兩個 endpoint 都沿用 organization market policy 與 `zh-hant`、`zh-hans`、
  `en` presentation；隱藏市場直接存取回 `404`；
- Web 在既有登入、語系與 AppShell 內新增列表與市場詳情 route，第一次請求顯示
  可存取的 loading skeleton，並具有 error、partial、unavailable banner；
- 畫面顯示 edition date 與各 block source date；null 顯示 `—`／「資料暫缺」，
  series 的 null point 形成斷點，不得連成 `0`。

目前 code 與上述目標不可混稱：現行實作是 fail-closed/LKG，具 metrics/charts，
且 publication `source_as_of` 為 non-null；它在缺輸入時不建立新 publication。
這是現況證據，不是晨報遷移後的接受規則。feature PR 必須把它遷移到上述
`complete/partial/unavailable` 行為，不能把現行 LKG/no-incomplete-publication
句子當成新 contract。

### Scheduler 與部署目標（後續 PR）

- scheduler 使用同一 API image，作為獨立 Compose service；每日
  `07:00 Asia/Taipei` 執行，具 claim/idempotency、manual rerun 與 tmpfs
  heartbeat。
- edition date 是 scheduler 執行當下的台北日期；週末與休市日仍執行並出版。
  有前收者依 manifest 標 stale，真正缺值者出版 `null`。
- 部署目標維持 GitHub Actions → GHCR digest → EC2 containers → RDS。後續
  feature/deploy PR 必須在 migration 前停止 scheduler，只執行一個 migration
  job，再啟動 API/Web/scheduler 並驗證五市場 terminal state。
- 後續 feature/deploy work 停止發布 mutable `latest` image tag，只使用 digest。
- 有時限的 launch exception 保留既有 SSH 與 production GitHub Environment
  secrets；OIDC、SSM、Secrets Manager 是獨立的 platform follow-up，不在本基線
  偷換部署授權或範圍。

## 4. Production go/no-go

下列條件全部成立才可進入 launch manifest 與 feature/deploy acceptance：

1. FinDB production release 已部署，新的 production Environment key 可用，
   public OpenAPI/version/hash 與該 release 對帳完成；舊 key 的 `403` 不得當作
   通過。
2. 以唯讀 probe 逐一驗證每個待 probe block 的 exact code/symbol、required
   fields/rows/series/history、日期／時區、freshness、unit 與 formula inputs。
3. 每個第一波 market 至少一個 block 變成 `候選`；否則 no-go，必須重新討論
   scope/source，不得以代理或假資料湊數。
4. 只有已凍結的五市場 manifest 可產生 runtime blocks；被排除的 card 不得以
   partial/pruning 方式回來。
5. source-run、checksum、failure evidence 與 publication revision 可由 RDS
   immutable contract 追溯，但不持久化 FinDB raw row/response。
6. manual scheduler smoke、API/UI 驗收與五市場終態均通過；daily job deadline
   alarm 已送達值班管道，previous digest rollback 已實際確認可用。
7. feature PR、deploy PR 與 production smoke evidence 均標示本基準版本；本
   docs PR 不得宣稱上述 probe 或部署已執行。

## 5. 後續 feature/deploy PR 的測試矩陣

下表是後續 PR 的 required checks；本 docs PR 不執行其中的 feature/deploy
commands 或 production probe。

| 層級             | 必須驗證                                                                                                                             | 後續 PR 的 required command/check                                                  |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------- |
| FinDB contract   | OpenAPI/fixture 相容、market/ticker mapping、歷史深度、空回應、401/403、429/5xx、schema drift、freshness、Decimal 與 pagination 上限 | production read-only probe evidence、provider contract tests                       |
| Formula/manifest | 日曆／交易日、休市、樣本不足、除零、Decimal 精度、null/0、formula version/window/basis/unit、三語 label 與固定 order                 | API unit tests、固定樣本、manifest/schema validation                               |
| Atomic card      | 任一 required input 缺漏即整卡 `missing/error`；不得產生部分 rows 或把 `null` 變 `0`                                                 | API integration tests、missing-history/freshness fixtures                          |
| Publication      | 完整／部分／全空、failed source run、同日 revision、immutable trigger、tenant policy、三語完整性、same-input no-op                   | 完整 PostgreSQL migration/integration tests、re-run/idempotency tests              |
| API/policy       | list summary 與 latest full blocks、market policy、三語 presentation、固定 order                                                     | API contract/client tests、authorization tests                                     |
| Web              | 初次 loading、null/error/empty、chart gap、不可動態增刪 blocks、三語 labels                                                          | Web unit/component tests、Playwright acceptance                                    |
| Scheduler        | `07:00 Asia/Taipei`、claim/idempotency、manual rerun、tmpfs heartbeat、terminal states                                               | scheduler integration/clock tests、Compose service checks                          |
| Deployment       | GHCR digest only、API/Web/scheduler restart、RDS connectivity、health/readiness、五市場 terminal-state check、rollback evidence      | `docker compose config`、production contract/preflight/smoke checks、deployment CI |

後續 PR 必須依適用範圍執行 `format:check`、`lint:check`、`type:check`、API client
deterministic check、完整 PostgreSQL integration、Web/API client tests、E2E、
production build 與 Compose contract；production probe 必須是獨立的唯讀
go/no-go evidence，不能以本地 fixture 或本次 docs PR 取代。

## 6. 文件取代關係

本文件曾是五市場晨報第一波的 accepted baseline；2026-08-30 起由
[`twelve-data-three-market-morning-report-plan.md`](twelve-data-three-market-morning-report-plan.md)
取代。其 FinDB probe 結果、矩陣與排除理由仍可作歷史證據，但新的第一波市場、
provider、UI 與驗收規則一律以新文件為準。
