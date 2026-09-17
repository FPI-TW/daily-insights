# 全球宏觀、債券與外匯

報告導覽將「宏觀」與「外匯」合併於 `/zh-hant/reports/global_macro_bonds`（其他語系同樣適用）。當使用者可查看宏觀市場時，舊的 `/reports/forex` 入口會轉址至整合頁；只開放外匯的組織仍保留原入口。API 在讀取共用快取前檢查宏觀市場權限，沒有擴大組織可讀取的市場範圍。

## 畫面與資料

- 能源／貴金屬：布蘭特、WTI、黃金、白銀、銅的 Twelve Data 現貨日收盤與日／週／月／年變動。歷史先以 `/eod` 取得已結算日期，再把日 K 截到該日並核對收盤，盤中未結算的當日 bar 不顯示。原油為 USD/桶、金銀為 USD/盎司、銅為 USD/磅。
- 油金比、銅金比：各自一張圖，相同日期的現貨收盤相除；缺日期不跨日配對，不補值，兩張圖的日期軸互不影響。
- 殖利率變動表：3M、2Y、5Y、10Y、30Y 及 SOFR；殖利率以百分比顯示、變動以 bp 顯示。
- 殖利率曲線：五個公債天期最後共同日期的觀察值，SOFR 不加入期限曲線。
- 經濟日曆目前未在前台顯示；Nasdaq 抓取已移除，僅保留停用的空 calendar payload 以維持 API 相容。
- 美元指數：DXY（DX-Y.NYB）最近 90 個日曆日的已完成收盤。
- 外匯：EUR/USD、GBP/USD、AUD/USD、NZD/USD、USD/JPY、USD/CHF、USD/CAD、USD/TWD、USD/KRW、USD/HKD、USD/CNH、USD/SGD、EUR/JPY、AUD/JPY，可選擇 30／90／365 日與貨幣對，切換不重送請求。
- 亞洲貨幣相對走勢：USD/TWD、USD/JPY、USD/KRW、USD/SGD、USD/CNH 各自以顯示期間內第一筆有效值設為 Base 100；每條線的基期日期由 API 的 `base_dates` 回傳並標示於圖例，期間改變時五條線各自重算。

原有不可變晨間報告保留在可展開區塊，其中的 Twelve Data 現貨與 ETF 代理資料，不與新儀表板的期貨、實際殖利率或 DXY 混用。

## 來源設定

`GET /api/reports/global_macro_bonds/dashboard` 回傳獨立的市場補充資料，不建立或變更報告 publication。

- `DAILY_INSIGHTS_TWELVE_DATA_API_KEY`：讀取上述外匯貨幣對的 Twelve Data 日線；明確指定 `Australia/Sydney` 時區並使用同一時區的 provider 當日作為 exclusive `end_date`，排除尚未完成的當日 bar。每條外匯 history 另回傳 30／90／365 日視窗的 `base_dates`。
- `DAILY_INSIGHTS_YFINANCE_ENABLED=true`：僅讀取 DXY。來源為延遲日資料，排除尚未完成的當日交易。DXY 的週末隔夜列沒有正式日收盤，排除該列並保留工作日收盤；工作日缺收盤仍視為資料錯誤。
- 美國財政部：[Daily Treasury XML](https://home.treasury.gov/treasury-daily-interest-rate-xml-feed)，不需金鑰。讀取當年及前兩年，單一年度失敗不丟棄其他成功年度。
- 紐約聯準銀行：[SOFR](https://www.newyorkfed.org/markets/reference-rates/sofr)，不需金鑰。

HTTP 從資料庫讀取最後保存的 dashboard snapshot，不直接呼叫來源。每日
RoutineRun 或後台 `global_macro_refresh` 建立 durable JobRuns；
`orchestration-worker` 執行各 Provider 的 functions，同 Provider 依序、不同 Provider
可平行，且部分失敗只重試缺失 scopes。`macro_dashboard_publish` 只讀取已保存的 typed
facts 與 provenance，不再次呼叫外部來源。Twelve Data 與 Yahoo 請求最多四個並行，
各商品失敗獨立處理。

每筆宏觀作業的 `result.sources` 記錄四個來源的 `code`、`name`、`status`、`fetched_at`、`affected_items` 與 `failures`。每個 failure 保存固定 endpoint 名称、項目、分類和可取得的 HTTP 狀態碼；不保存原始例外、金鑰或回應內容。來源狀態為 `ok/degraded/unavailable/disabled`。同一來源可以包含多種失敗；adapter 沒有提供可辨識原因時顯示「原因未能確認」。診斷只供後台，不加入前台 dashboard API。

市場來源失敗使用 `macro_sources_unavailable`。歷史作業沒有來源明細時顯示「未記錄來源明細」，不回寫歷史。

日變動比較前一筆有效交易日；週／月／年以日曆期間回推，尋找該日期當日或之前最多七日的觀察值。月末與閏年採該月最後一天。超過七日的比較缺口不計算報酬，資料日期由各商品自行標示。

## 驗證

前端測試涵蓋日曆期間、閏年、bp、跨日期比率、防止以缺值作為零、載入狀態與貨幣對／期間切換。後端測試涵蓋來源驗證、SOFR、快取合併及權限先於快取讀取。

`apps/web/e2e/mock-api.mjs` 的確定性價格只供瀏覽器測試使用；正式 API 不匯入測試資料。
