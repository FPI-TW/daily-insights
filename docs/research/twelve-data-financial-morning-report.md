# Twelve Data 金融晨報資料研究

研究日期：2026-08-31  
研究範圍：Twelve Data 官方網站、API 文件與官方 Support Center。方案、credits、
市場權限與授權可能變動，正式上線仍須以 production 帳戶 Dashboard 與書面合約為準。

## 結論摘要

Twelve Data 的市場價格、歷史序列、參考資料與 market movers 足以支援第一波
「宏觀／債券、加密貨幣、美股」晨報的基本內容。每日批次晨報應以 REST API 為主；
WebSocket 適合價格串流，但不提供 OHLC、技術指標或 bid/ask。

本專案晨報固定於 `08:00 Asia/Taipei` 執行。這個時間正好是 `00:00 UTC`，符合
crypto 日線的 UTC 日界；但 Twelve Data 說明 REST 完成 K 棒通常在 candle 結束後
約 0.3–2 分鐘才可用，因此 08:00 整點執行時仍須以實際回傳時間戳驗證最後一根日線
是否已完成，不得把 partial bar 當作完整日線。

美股方面，08:00 台北時間雖已在美股收盤後，但官方表示涵蓋 100% 市場成交量的完整
historical／EOD 資料，要到收盤後下一交易日的美東時間午夜才提供，約為台北時間
12:00–13:00。故 08:00 晨報只能採用較早可得的即時 quote／market movers feed，
不能宣稱為已完成官方對帳的 consolidated EOD。

公開資料最大的上線風險是授權而非 endpoint 能力。Individual 方案只允許個人、內部、
非商業用途；客戶端 display 至少應評估 Business Venture，而報告散布、下游提供資料、
美股 redistribution 或 white label 可能需要 Enterprise、Redistribution Rights Add-On
或另外書面協議。公開報告原則上必須依官方規則標示 Twelve Data attribution。

## 資料分類總表

| 分類             | 官方確認的資料能力                                                                            | 主要端點                                                                          | 晨報適用性                                     |
| ---------------- | --------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- | ---------------------------------------------- |
| 市場價格         | 股票、FX、crypto、ETF、基金、商品、債券與指數的即時／延遲／歷史 OHLC(V)                       | `/quote`、`/price`、`/time_series`、`/eod`                                        | 高；收盤摘要、區間漲跌與績效曲線               |
| 美股             | 全方案可用預設即時 feed；預設 feed 約代表全市場 5% 成交量；完整 EOD 涵蓋 100%                 | `/quote`、`/time_series`、`/eod`、`/market_movers/stocks`                         | 高，但 08:00 不是 confirmed EOD                |
| 加密貨幣         | 180+ 交易所、24/7 即時與歷史資料，可指定交易所或使用聚合資料                                  | `/cryptocurrencies`、`/quote`、`/time_series`、`/market_movers/crypto`            | 高；固定幣種價格、區間報酬、Base-100           |
| 外匯             | Forex API v2 涵蓋 1,500+ fiat pairs、60+ 來源，使用加權綜合中價                               | `/forex_pairs`、`/exchange_rate`、`/currency_conversion`、`/time_series`          | 中高；適合未來 FX 晨報，但不是 broker 可成交價 |
| 商品             | 貴金屬、工業金屬與能源等 30+ 商品；提供即時與歷史資料                                         | `/commodities`、`/quote`、`/time_series`                                          | 高；適合宏觀商品卡片                           |
| 債券／固定收益   | `/bonds` 提供每日更新的債券與殖利率商品目錄，例如 US Treasury Yield 2 Years                   | `/bonds`、`/quote`、`/time_series`                                                | 中高；須逐期限驗證 symbol、單位與歷史          |
| ETF／共同基金    | ETF 涵蓋 50+ 國家；共同基金超過 200,000 檔；另有 NAV、績效、風險、持倉與 ESG                  | `/etfs`、`/funds`、`/etfs/world/*`、`/mutual_funds/world/*`                       | 中；完整 metrics 多要求 Ultra／Enterprise      |
| 技術指標         | 100+ 指標，包含 SMA、EMA、MACD、RSI、Bollinger Bands、ATR、OBV 等                             | `/technical_indicators`、`/rsi`、`/macd` 等                                       | 高；固定晨報公式由應用端計算更容易重現         |
| 基本面與公司事件 | Profile、股利、拆股、earnings、IPO／財報日曆、statistics、三大財表與 press releases           | `/profile`、`/earnings_calendar`、`/statistics`、三大財表                         | 中高；適合事件日曆與公司焦點                   |
| 分析與監管       | 盈餘／營收預估、EPS revisions、recommendations、price target、analyst ratings、EDGAR、holders | `/earnings_estimate`、`/recommendations`、`/price_target` 等                      | 中；方案與 credit 成本較高                     |
| 參考與市場狀態   | 商品目錄、交易所、MIC、國家、交易時間、開休市狀態與最早歷史日期                               | `/stocks`、`/symbol_search`、`/exchanges`、`/market_state`、`/earliest_timestamp` | 高；用於 source gate、交易日與 freshness       |

官方來源：

- [Twelve Data 介紹與資料涵蓋](https://support.twelvedata.com/en/articles/5609168-introduction-to-twelve-data)
- [API 文件](https://twelvedata.com/docs/advanced/api-usage)
- [商品資料](https://twelvedata.com/commodities)
- [加密貨幣資料](https://twelvedata.com/cryptocurrency)
- [ETF 與基金資料](https://twelvedata.com/etf)
- [基本面資料](https://twelvedata.com/fundamentals)
- [Forex API v2](https://support.twelvedata.com/en/articles/12520817-forex-api-v2)

## 時間週期、延遲與歷史深度

官方主要支援 `1min`、`5min`、`15min`、`30min`、`45min`、`1h`、`2h`、
`4h`、`8h`、`1day`、`1week`、`1month`。部分文件另列出 `5h`，應以各 endpoint
當下的參數定義與 credentialed probe 為準。

- 日、週、月資料：多數股票可追溯至第一個交易日，市場頁常標示最早約 1980 年。
- Intraday：通常只有數月到數年；官方通用說明稱 1 分鐘資料自 2020-02-10 起，
  但各市場可能更晚，例如美股市場頁標示自 2022-12-15 起。
- `/time_series` 單次最多 5,000 筆；較長歷史必須使用日期窗口分段取得。
- `/earliest_timestamp` 可查特定 symbol 與 interval 的實際起始時間。
- 基本面資料可追溯至 1980–90 年代；dividends、splits 通常有 10+ 年，earnings
  標示為完整公司歷史；三大財表的完整歷史可能要求 Ultra／Enterprise。
- REST 完成 K 棒通常在 candle 結束後約 0.3–2 分鐘可用。
- WebSocket tick 一般約 170ms，但依商品與 provider 狀況變動。
- 公司股利與 coupon 通常在官方揭露後 24 小時內更新；split、reverse split、
  merger 等較複雜事件可能需要 48 小時。

官方來源：

- [歷史價格指南](https://support.twelvedata.com/en/articles/5656039-how-to-get-historical-prices)
- [資料延遲](https://support.twelvedata.com/en/articles/5203307-data-delays)
- [品質標準與歷史限制](https://support.twelvedata.com/en/articles/5549842-twelve-data-quality-standards)
- [美股資料範圍與 EOD 時間](https://support.twelvedata.com/en/articles/9935903-us-equities-market-data)

## 重要端點與 credits

| 端點                                     | 用途                                 | 官方 credit／方案                 |
| ---------------------------------------- | ------------------------------------ | --------------------------------- |
| `/time_series`                           | OHLC(V) 歷史序列                     | 1／symbol                         |
| `/quote`                                 | 最新 OHLC、成交量、變動與 52 週區間  | 1／symbol                         |
| `/price`                                 | 最新價格                             | 1／symbol                         |
| `/eod`                                   | 最新 EOD 價格                        | 1／symbol                         |
| `/time_series/cross`                     | 跨資產／跨幣別歷史換算               | 5／symbol                         |
| `/market_movers/{market}`                | 股票、ETF、基金、FX、crypto 漲跌排行 | 100／request；Pro／Venture+       |
| `/exchange_rate`、`/currency_conversion` | 即時匯率與換算                       | 1／symbol                         |
| `/stocks`、`/etfs`、`/funds`、`/bonds`   | 商品 catalog                         | 多數為 1／request                 |
| `/symbol_search`                         | symbol／名稱／識別碼搜尋             | 1／request                        |
| `/technical_indicators`                  | 列出指標                             | 1／request                        |
| `/rsi`、`/macd` 等                       | 指標序列                             | 通常 1／symbol                    |
| `/profile`                               | 公司簡介                             | 10／symbol；Grow／Venture+        |
| `/dividends`、`/earnings`                | 股利或 earnings 歷史                 | 20／symbol；Grow／Venture+        |
| `/earnings_calendar`、`/ipo_calendar`    | 事件日曆                             | 40／request；Grow／Venture+       |
| `/statistics`                            | 估值與財務統計                       | 50／symbol；Pro／Venture+         |
| 三大財表                                 | 年／季財務報表                       | 100／symbol；Pro／Venture+        |
| `/key_executives`                        | 高階主管資料                         | 1,000／symbol；Ultra／Enterprise+ |
| `/last_change/{endpoint}`                | 查基本面／分析最後變更               | 1／request                        |
| `/market_state`                          | 即時開休市狀態                       | 1／request                        |
| `/exchange_schedule`                     | 指定日期交易時段                     | 100／request；Ultra／Enterprise+  |

Batch 可減少網路 round-trip，但不會減少 credits：三個 `/time_series` symbols 仍消耗
三個 credits。多端點 POST 的 credit 是所有子請求加總；超過 quota 時可能只回部分資料。

官方來源：

- [API credits](https://support.twelvedata.com/en/articles/5615854-credits)
- [Batch API requests](https://support.twelvedata.com/en/articles/5203360-batch-api-requests)
- [API endpoint 文件](https://twelvedata.com/docs/advanced/api-usage)

## 方案限制

公開頁面列的是各 tier 的起始額度；同一方案可選更高的 credit 組合。

| 方案                 | 起始 API／WS                     | 主要能力                                                 | 晨報判斷                             |
| -------------------- | -------------------------------- | -------------------------------------------------------- | ------------------------------------ |
| Individual Basic     | 8 API／分鐘、800／日、8 trial WS | 美股、FX、crypto、reference、技術指標                    | 測試用                               |
| Individual Grow      | 55+ API、8 trial WS、無每日上限  | 約 27 市場、全球 EOD、商品、有限基本面                   | 只適合內部或非商業用途               |
| Individual Pro       | 610+ API、500+ WS                | 約 75 市場、market movers、fixed income、盤前盤後        | 技術上足夠，但不能作客戶產品授權依據 |
| Individual Ultra     | 2,584+ API、2,500+ WS            | 全市場、analysis、ETF／基金 metrics、較深歷史            | 仍限個人／內部用途                   |
| Business Venture     | 610+ API、500+ WS                | 約 75 市場、external display、商品、基本面、fixed income | 客戶端 display 的最低候選            |
| Business Enterprise  | 10,000+ API／WS                  | 全市場、analysis、ETF metrics、external distribution     | 對外散布晨報的主要候選               |
| Business Enterprise+ | 客製                             | White label、交易所授權、客製 endpoint                   | 白牌或轉售需求                       |

官方來源：

- [Individual Pricing](https://twelvedata.com/pricing)
- [Business Pricing](https://twelvedata.com/pricing-business)
- [Trial 與方案差異](https://support.twelvedata.com/en/articles/5335783-trial)

## 授權與 attribution

- Individual 方案限個人、內部及非商業用途，不允許 commercial display 或
  redistribution。
- Business 方案可用於商業與客戶端情境，但非美國市場可能另需交易所核准。
- 任何資料 redistribution 都可能需要 Twelve Data 的額外書面協議。
- 美國股票對外提供給客戶需要 US Equities Redistribution Rights Add-On；OTC 亦需
  另外授權。
- 公開網站、app、dashboard、報告或出版品使用 Twelve Data 資料時，原則上必須在
  相關資料附近顯示 `Data provided by Twelve Data` 或 `Source: Twelve Data`，並使用
  指向 Twelve Data 主站的 dofollow link。
- 多個資料區塊可能需要在各相關區塊分別標示；只有合約明載的 white-label 情境可豁免。

官方來源：

- [商業及個人用途](https://support.twelvedata.com/en/articles/5332349-commercial-and-personal-usage)
- [Attribution 指引](https://support.twelvedata.com/en/articles/12647398-attribution-guidelines-for-using-twelve-data)
- [美股資料授權](https://support.twelvedata.com/en/articles/9935903-us-equities-market-data)

## 適用於第一波晨報的資料

### 優先採用

1. 商品快照：固定商品以 `/quote` 或短期 `/time_series` 取得。商品與 FX 是多來源
   聚合中價，不是交易所或 broker 的可成交報價；官方表示不同來源可能出現約 ±3%
   偏差，報告需附資料口徑 caveat。
2. 加密貨幣：固定 BTC、ETH、SOL、XRP、ADA 日線，由應用端計算 1D／7D／30D
   與 Base-100。08:00 必須檢查最新 bar 是否已結束及通過 REST 處理延遲。
3. 美股：主要指數 `/time_series`、`/quote` 與 `/market_movers/stocks`。08:00 版應明示
   為 post-close 即時 feed 摘要，而不是 confirmed consolidated EOD。
4. 交易日與 freshness：使用 `/market_state`、exchange metadata、provider timestamp
   與應用自己的時區規則。`/exchange_schedule` 權重高，適合定期快取而非每日全量呼叫。

### 未來擴充

- Earnings calendar、earnings surprise、股利與拆股。
- Press releases、公司 statistics、recommendations 與 price targets。
- ETF／基金 summary、composition、risk 與 performance。

官方來源：

- [商品與外匯價格差異](https://support.twelvedata.com/en/articles/11850499-understanding-price-deviations-in-commodities-and-forex-data)
- [市場資料與 reference 能力](https://twelvedata.com/market-data)

## 主要風險與缺口

1. **授權缺口**：Venture 的 external display 文案不能取代正式 redistribution 合約；
   必須取得本產品客戶端晨報、下載、API response 與衍生內容保存方式的書面確認。
2. **08:00 crypto 邊界**：執行時間與 UTC 日線收盤相同，REST 可能仍在處理最後一根
   candle；需以 timestamp 與完整性 gate 阻止 partial bar。
3. **08:00 美股最終性**：完整 EOD 尚未發布；quote 與 movers 的排名、volume 或價格
   可能與中午後可取得的 confirmed EOD 不同。
4. **Catalog 不等於可用權限**：`/stocks`、`/exchanges`、`/symbol_search` 可列出方案
   不含的市場；每個正式 block 仍須 credentialed probe。
5. **統計口徑不一致**：官方不同頁面使用近一百萬 instruments、100K+ symbols、
   84 markets、100+ exchanges 等不同口徑；production acceptance 只能依 exact catalog
   與實際 probe。
6. **修正契約不足**：EOD 有 preliminary 與 confirmed 階段，但公開文件未完整定義
   revision window、backfill SLA 或更正通知；應保存 source timestamp、fetch time、
   edition revision 與重新發布政策。
7. **高權重尖峰**：兩個 market movers 已消耗 200 credits；若同分鐘再取 statistics
   或財報，需有明確限流與分批策略。
8. **Null 與 volume 語義**：官方允許欄位為 `null`；商品、FX、crypto 或特定日線的
   volume 不一定存在或可互相比較，缺值不可當成零。

## 上線前後續調查

### P0：上線阻擋

1. 取得 licensing team 對 external display、redistribution、attribution、white label、
   衍生資料保存與三個正式市場的書面確認。
2. 保存 production Dashboard 的方案、API credits／分鐘、daily limit、market access
   與 add-ons 證據。
3. 明確定義 08:00 美股資料為 post-close feed；若產品要求 100% consolidated EOD，
   則應改至台北 12:00–13:00 後產生或另行補版。

### P1：Credentialed probe

- 驗證所有美債期限商品的 exact symbol、值是 price 或 yield、單位與歷史。
- 驗證 crypto 在 08:00 的最後一筆是完成 bar 或 partial bar。
- 驗證 `/market_movers/stocks` 在美股收盤後至午夜間是否修改排名與成交量。
- 記錄 commodity quote currency、聚合來源、market day boundary 與 missing fields。
- 驗證 `/earliest_timestamp`、batch partial failure、429／`Retry-After`、credit headers。
- 驗證 preliminary EOD 轉 confirmed 的時間及既有日期是否會被覆寫。

### P2：未來擴充

- 對 earnings、press releases、statistics、recommendations 做代表性股票 coverage probe。
- 驗證 ETF／基金 metrics 的更新日、持倉 `as_of`、歷史與授權。
- 擴張至台股、港股、中國股與 FX 前，逐交易所核對 EOD／real-time／delayed 狀態與
  add-on，不依賴全站行銷數字。

## 建議決策

Twelve Data 可繼續作為第一波三市場晨報的唯一 raw-data provider，但 production
go-live 應以以下條件全部通過為前提：

1. 外部展示與 redistribution 的書面授權證據完整。
2. 08:00 crypto bar 完整性 gate 通過。
3. 美股 08:00 feed 的非 consolidated EOD 語義已寫入產品文案與 freshness contract。
4. Exact endpoint、symbol、單位、歷史及分鐘 credit 預算均通過 production credential
   probe。
