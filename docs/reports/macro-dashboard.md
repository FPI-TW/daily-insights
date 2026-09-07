# 全球宏觀、債券與外匯

報告導覽將「宏觀」與「外匯」合併於 `/zh-hant/reports/global_macro_bonds`（其他語系同樣適用）。當使用者可查看宏觀市場時，舊的 `/reports/forex` 入口會轉址至整合頁；只開放外匯的組織仍保留原入口。API 在讀取共用快取前檢查宏觀市場權限，沒有擴大組織可讀取的市場範圍。

## 畫面與資料

- 能源／貴金屬：布蘭特、WTI、黃金、白銀、銅的 Twelve Data 現貨日收盤與日／週／月／年變動。歷史先以 `/eod` 取得已結算日期，再把日 K 截到該日並核對收盤，盤中未結算的當日 bar 不顯示。原油為 USD/桶、金銀為 USD/盎司、銅為 USD/磅。
- 油金比／銅金比：相同日期的期貨收盤相除，以左右雙軸呈現；缺日期不跨日配對，不補值。
- 殖利率變動表：3M、2Y、5Y、10Y、30Y 及 SOFR；殖利率以百分比顯示、變動以 bp 顯示。
- 殖利率曲線：五個公債天期最後共同日期的觀察值，SOFR 不加入期限曲線。
- 經濟日曆：台北當日的 Nasdaq 公開經濟數據與來源涵蓋的央行事件；以來源提供的 GMT 時間轉換並篩選日期。未到事件時間不顯示來源提前提供的實際值。實際值 0 仍顯示為 0。
- 美元指數：DXY（DX-Y.NYB）最近 90 個日曆日的已完成收盤。
- 外匯：EUR/USD、GBP/USD、AUD/USD、NZD/USD、USD/JPY、USD/CHF、USD/CAD、USD/TWD，可選擇 30／90／365 日與貨幣對，切換不重送請求。

原有不可變晨間報告保留在可展開區塊，其中的 Twelve Data 現貨與 ETF 代理資料，不與新儀表板的期貨、實際殖利率或 DXY 混用。

## 來源設定

`GET /api/reports/global_macro_bonds/dashboard` 回傳獨立的市場補充資料，不建立或變更報告 publication。

- `DAILY_INSIGHTS_YFINANCE_ENABLED=true`：沿用既有 Yahoo Finance 開關與 adapter，讀取期貨、DXY、外匯歷史。來源為延遲日資料，排除尚未完成的當日交易。DXY 的週末隔夜列沒有正式日收盤，排除該列並保留工作日收盤；工作日缺收盤仍視為資料錯誤。
- 美國財政部：[Daily Treasury XML](https://home.treasury.gov/treasury-daily-interest-rate-xml-feed)，不需金鑰。讀取當年及前兩年，單一年度失敗不丟棄其他成功年度。
- 紐約聯準銀行：[SOFR](https://www.newyorkfed.org/markets/reference-rates/sofr)，不需金鑰。
- Nasdaq：[Economic Calendar](https://www.nasdaq.com/market-activity/economic-calendar)，不需金鑰。API 讀取台北今日與前一日的來源資料，再以 GMT 時間轉換後保留台北當日事件；來源失敗時顯示不可用，不將它當成「當日沒有事件」。Nasdaq 未提供事件重要性時，介面不顯示空白的重要性欄位。

伺服器按程序共用五分鐘快取，並合併同時進入的請求；台北跨日後日曆快取失效。Yahoo 請求最多四個並行，各商品失敗獨立處理。初次請求顯示 skeleton，缺資料顯示 `—` 或不可用訊息。來源失敗不以示範資料補上。

日變動比較前一筆有效交易日；週／月／年以日曆期間回推，尋找該日期當日或之前最多七日的觀察值。月末與閏年採該月最後一天。超過七日的比較缺口不計算報酬，資料日期由各商品自行標示。

## 驗證

前端測試涵蓋日曆期間、閏年、bp、跨日期比率、防止以缺值作為零、載入狀態與貨幣對／期間切換。後端測試涵蓋來源驗證、台北日期篩選、未公布實際值、SOFR、快取合併及權限先於快取讀取。

`apps/web/e2e/mock-api.mjs` 的確定性價格只供瀏覽器測試使用；正式 API 不匯入測試資料。
