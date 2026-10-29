# 2026-10-05 貴金屬與外匯圖表資料調查

調查時間：2026-10-05 台北時間約 12:04–12:15。以正式環境唯讀資料庫查詢、
已部署程式、Git 歷史及 Twelve Data 官方 API GET 核對；未重新發布或修改資料。

## 已確認狀態

| 資料            | 資料庫最新日期        | 09:29:49 台北時間發布的 dashboard 快照 |
| --------------- | --------------------- | -------------------------------------- |
| 黃金 XAU/USD    | 2026-10-03            | unavailable，0 點                      |
| 白銀 XAG/USD    | 2026-10-03            | unavailable，0 點                      |
| WTI/USD         | 2026-10-02            | ok，579 點                             |
| XBR/USD         | 2026-10-02            | ok，575 點                             |
| 銅 HG1          | 2026-10-04            | ok，721 點                             |
| 14 組外匯貨幣對 | 2026-10-03，各 429 筆 | 全部 unavailable，0 點                 |
| 美元指數 DXY    | 2026-10-02            | ok，509 點                             |

10/05 商品 attempt 在台北時間 12:04 為 partial，黃金與白銀失敗；WTI、
XBR、HG1 成功。14 組外匯在 09:29 與 12:03 的 attempts 皆失敗。共同錯誤為
`DataSourceContractError: Twelve Data time series must be strictly ascending`。

## 上游回傳同日重複日線，更新被拒收

正式容器的 `/time_series` 回傳黃金與白銀各兩筆 2026-10-04，close 不同：

| 商品    | 第一筆 close | 第二筆 close | `/eod` 日期與 close    |
| ------- | ------------ | ------------ | ---------------------- |
| XAU/USD | 4137.63144   | 4137.51110   | 2026-10-04，4137.51110 |
| XAG/USD | 60.36886     | 60.36723     | 2026-10-04，60.36723   |

上游有新報價，並非完全沒有價格。adapter 在日期嚴格遞增檢查時因相同日期拒絕
整段序列，尚未走到與 EOD 的一致性檢查。指定 UTC 或 America/New_York
亦重現商品重複日期，不能只靠 timezone 參數解決。

直接抽查 EUR/USD 回應 HTTP 200、`status: ok`，400 筆日線中有兩筆
2026-10-04；其餘日期遞增。其他 13 組貨幣對只有已持久化的相同驗證錯誤證據，
未逐一查詢原始回應。EUR/USD 的 EOD 日期未另行核對。

相關程式：[Twelve Data adapter](../../apps/api/src/daily_insights_api/modules/data_sources/twelve_data/adapter.py)。

## 舊發布流程排除失敗來源的可用歷史

09:29:49 發布的快照沒有外匯、黃金、白銀歷史，但其凍結輸入仍有 5,824 筆外匯
資料（每組 416 筆），以及黃金 685 筆、白銀 673 筆，最新日期皆為 10/03。

舊版本 `66897f5` 的 `_rows_for_current_outcomes` 排除 unavailable dataset，
也排除 partial dataset 中更新失敗的 symbol。因此上游失敗後，已儲存的歷史沒有
進入輸出快照，並非資料庫歷史遭刪除。

目前版本 `ca36ec4` 已移除此排除邏輯，保留可用歷史；部署時間約為台北 11:59，
晚於上述空快照。以目前 dataset／symbol mapping 唯讀比對該凍結輸入，外匯與
黃金、白銀均可形成 `ok` 歷史；這項檢查未執行完整發布函式，亦未寫入或重新發布。
API 仍直接提供已儲存的 09:29 快照。

相關程式：[projection](../../apps/api/src/daily_insights_api/modules/orchestration/projections.py)、
[dashboard endpoint](../../apps/api/src/daily_insights_api/modules/reports/router.py)。

## 圖表影響與後續處理

前端油金比需要 WTI 與黃金的同日資料；黃金序列為空時，`ratioPoints` 回傳空陣列，
銅金比同樣受影響。外匯圖表則因快照為 0 點顯示 unavailable。前端日期篩選以
最新已有觀察日為基準，單純資料較舊不會導致這些圖表變空。DXY 資料正常。

10/05 早報 revision 2 在 09:29:48 發布，`macro.commodities`、`macro.rates_fx`、
`macro.commodity_ratios` 皆為 error，沒有 ratio series。

建議分開處理：透過已授權的重新產生／發布流程，讓目前保留歷史的程式產生新快照；
另為上游重複日期與衝突數值建立可驗證的處理規則。不能盲目取最後一筆、放寬驗證，
或把尚未完成的日線當成已收盤資料。單純重試無法修正仍持續回傳的重複日線。

相關程式：[ratioPoints](../../apps/web/src/lib/macro-dashboard.ts)、
[MacroDashboard](../../apps/web/src/components/MacroDashboard.tsx)。

以上為調查時點的狀態；後續排程、供應商修正或新快照發布可能改變結果。本次沒有
enqueue、backfill、重新發布、部署、commit 或 push。

## 同日恢復結果

使用者隨後授權恢復歷史圖表。台北時間 12:21:19，已部署的 worker 完成一次
`macro_dashboard_publish`，任務 ID 為 `f46c63dc-f36b-48cc-b42d-38dd13b60766`。
透過既有 job 建立流程排入獨立 projection，沿用原始 terminal 來源任務的 dependency，
保留來源失敗紀錄與資料 provenance；沒有新增供應商擷取或修改既有重試任務。

任務結果為 `action: published`、`missing_histories: []`、`missing_datasets: []`。
狀態仍為 partial，反映上游更新失敗；歷史序列已全部恢復為 ok。
黃金 685 點、白銀 673 點、14 組外匯各 416 點，最新日期皆為 10/03。

以正式快照執行前端 `macroDashboardSchema`、`ratioPoints` 與 `recentPoints`
驗證通過：油金比 558 點，最新 10/02；銅金比 676 點，最新 10/03；全部 14 組
外匯有可呈現的一年內資料。未以登入中的正式瀏覽器檢查圖表像素。
本次恢復未修改產品程式、原始行情或已發布早報；上游重複日線問題仍待處理。

## 上游重複日線的程式修正

本地修正將 Twelve Data 契約更新為 `2026-10-05.v8`。已完成價格路徑先以
完整 schema／metadata 與原始非倒序日期驗證回應，再排除 EOD 之後的日線；
同日完全相同的 OHLC／nullable volume 可合併。歷史衝突拒收，EOD 當日衝突
僅接受恰有一種完整日線的 exact Decimal close 符合官方 EOD，保留完整 row，
不混用欄位。所有已完成候選包含未選取者均檢查 OHLC 範圍；未來 schema 異常
仍拒收，通用日線路徑仍禁止重複日期。至少請求 4 筆原始資料，仍須有兩個不同
已完成日期。原始 digest、實際 query fingerprint 與接受日線的 count／as-of
各自保留。完整操作規則見[統一排程 runbook](../runbooks/unified-orchestration.md)。

此段記錄本地程式與回歸測試的契約，未部署、重新擷取、enqueue 或修改正式資料。
後續驗證與正式環境處理應另記錄時間與結果，避免與前述恢復作業混淆。
