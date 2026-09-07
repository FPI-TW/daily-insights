# 美股標的選擇修正規格

> 給實作者（Claude Code）的交辦文件。撰寫日期 2026-09-03。
> 本規格處理一個**資料選擇**問題，不是格式化問題。前端無法修正，
> 相關的呈現層工作見 `web-presentation.md`。

## 0. 問題

正式站 `/zh-hant/reports/us_equity` 的「漲跌焦點」實際顯示：

| 商品   | 價格   | 變動    |
| ------ | ------ | ------- |
| BURUD  | 1.4500 | +98.63% |
| DSX.WT | 1.0500 | +68.00% |
| EYES   | 1.4200 | -95.12% |
| ADBT   | 1.7200 | -53.39% |

四檔股價 1–2 美元的微型股。這是要賣給投資研究團隊與投顧的晨報，
唯一的美股區塊卻沒有任何一檔他們的客戶持有或關心的標的。

**這不是 bug，是端點的數學性質。** 依漲跌**幅**排序全美股票池，前幾名必然是低價股：
一檔 1.45 美元的股票漲 0.72 美元就是 +98.63%，而一檔 500 美元的權值股要漲 493 美元
才能上榜。所以這個表格結構上永遠不會出現 NVDA、AAPL、TSLA。

`EYES` 的 `-95.12%` 另外值得注意：單日跌 95% 通常不是正常交易，
而是反向分割、下市前異動、或供應商未還原除權息。直接印在付費晨報上，
客戶會第一時間抓到。

## 1. 資料路徑（已完整追查）

那個百分比**不是本專案計算的**，是供應商欄位原封不動傳遞：

```
Twelve Data GET /market_movers/stocks?direction=gainers|losers&outputsize=2&country=USA
    ↓ values[].percent_change
TwelveDataMover.percent_change: Decimal        data_sources/twelve_data/schemas.py:85（僅驗型別）
    ↓
Mover.percent_change                            twelve_data/adapter.py:210（直接指派）
    ↓
_quantize(item.percent_change, precision=4)     reports/morning_report.py（僅四捨五入）
    ↓
TableCell(value=...) 搭配 TableColumn(id="change", unit_code="percent")
```

中間沒有除法、沒有基準日選擇、沒有前收價比對。所以 `+98.63%` 的**定義**
完全取決於 Twelve Data 怎麼算，而供應商文件未載明 movers 端點的 `percent_change`
基準（前收 vs 開盤、是否還原除權息）。

manifest 對此其實很誠實：

```python
formula="top two gainers followed by top two losers as returned by provider"
```

`as returned by provider` —— 定義外包給了供應商。

## 2. 為什麼「加個價格門檻」不夠

直覺的修法是抓多一點再過濾掉低價股。這條路會撞到三道牆：

1. **端點不支援過濾參數**。`/market_movers/stocks` 只接受 `direction`、`outputsize`、
   `country`，沒有市值、價格或成交量條件。
2. **`outputsize` 上限 50**（`adapter.py` 的 `if not 1 <= outputsize <= 50`）。
   全美股票池的漲跌幅前 50 名幾乎全是低價股，過濾後很可能一檔都不剩。
3. **現有的嚴格檢查會直接爆掉**。`adapter.py:196` 是
   `if payload.status != "ok" or len(payload.values) != outputsize: raise DataSourceContractError`
   —— 過濾後數量不足時，整個 `us_equity` 市場會變成 `unavailable`，
   而不是優雅降級。

過濾治標，而且治不乾淨。真正的問題是**「漲跌幅前兩名」這個定義本身不符合產品需求**。

## 3. 建議方案：改為固定籃子 + `/quote`

投顧晨報的美股段落要回答的問題是「昨天大盤與主要標的怎麼走」，
不是「哪一檔雞蛋水餃股漲停」。所以把 dataset 從「供應商排序的 movers」
換成「我們自己定義的一籃子標的」。

### 固定籃子與指數資料（產品決策，可調整）

| 群組     | 標的                                                            | 用途               |
| -------- | --------------------------------------------------------------- | ------------------ |
| 五大指數 | `^GSPC`、`^NDX`、`^DJI`、`^SOX`、`^RUT`                         | 日、月、年漲跌表現 |
| 權值股   | `AAPL`、`MSFT`、`NVDA`、`GOOGL`、`AMZN`、`META`、`AVGO`、`TSLA` | 客戶實際關心的標的 |

五大指數由獨立的指數日線 pipeline 寫入 `index_daily_bars`，不納入晨報 publication
的 Twelve Data quote snapshot。

### 顯示方式

美股頁面先呈現五大指數表現，再呈現晨報的 `us.mega_caps` table block；權值股欄位為
標的 / 價格 / 漲跌幅，**依漲跌幅排序**（籃子固定，排序不影響選樣，就沒有低價股問題）。

### 成本

權值股共 8 檔，符合每分鐘 8 credits 的限制。`get_quotes` 以逗號分隔批次查詢，
供應商仍按 symbol 計費。

## 4. manifest 的硬約束（動手前必讀）

`LaunchManifest.validate_references` 有六條規則會擋住不完整的改動：

| 規則                                             | 對本次改動的影響                           |
| ------------------------------------------------ | ------------------------------------------ |
| `markets` 順序必須完全等於 `LAUNCH_MARKET_ORDER` | 只改 `us_equity` 內容不受影響              |
| **每個 block 只能引用 1 個 dataset**             | `us.mega_caps` 只引用 `us.mega_cap_quotes` |
| **每個 dataset 只能被 1 個市場引用**             | 權值股 dataset 只掛 `us_equity`            |
| `symbol_units` 必須涵蓋每個 symbol               | `us.mega_cap_quotes` 要逐一列出 8 檔       |
| **每個 unit 必須是三碼大寫**（`^[A-Z]{3}$`）     | 全部填 `"USD"`                             |
| 每個 block 必須有三語 labels                     | 權值股 block 要有 zh-hant / zh-hans / en   |

另外 `DatasetManifest.endpoint` 是 `Literal["/quote", "/time_series", "/market_movers/stocks"]`。
新 dataset 用 `/quote`，已在允許值內。若確定完全不再使用 movers，
可考慮把該 literal 值移除，但那會連帶影響 `TWELVE_DATA_CONTRACT_HASH`
（`adapter.py:27` 的 `b"twelve-data:quote,time_series,market_movers/stocks,asset-type:2026-08-31.v3"`）
—— **建議這次先保留，減少 blast radius**。

## 5. 版本與 revision 的連鎖反應

這是動 manifest 一定會遇到的事，先講清楚：

- `ACTIVE_LAUNCH_MANIFEST.sha256` 是對整份 manifest 的 canonical JSON 取雜湊。
  改任何一個欄位都會改變它，而它會寫進 `ReportPublication.manifest_hash`。
- `morning_report.py` 的 `_next_revision` 會比對 `latest_manifest_hash`，
  manifest 變了就產生**新的 revision**。這是設計上的正確行為，不要繞過。
- 同時要更新兩個版本字串：
  - `LaunchManifest.version`（目前 `"three-market.v4"`）
  - `MORNING_REPORT_DERIVATION_VERSION`（目前 `"twelve-data.three-market.v4"`）

若這次同時要做市場擴充，**把 manifest 的改動合併成一次**，避免連續 bump 產生多個 revision。

## 6. 順帶解掉的授權問題

`/market_movers/stocks` 在 Twelve Data 免費層**不開放**，需要 Grow（US$79/月）以上。
`/quote` 免費層就有。

換成籃子 + `/quote` 之後，`us_equity` 對付費層的依賴就消失了。這不代表授權問題全解
（把資料顯示給付費訂閱者仍屬 external display，條款上要 Venture 層），
但至少把「為了一個沒有價值的表格而綁在 Grow 方案」這個不划算的相依拿掉了。

## 7. 順便修正的資料問題

改 manifest 時一併處理：

**銅的計價幣別**。`macro.commodity_quotes` 的 `symbol_units` 是
`{"XBR/USD": "USD", "XAU/USD": "USD", "HG1": "EUR"}` —— 銅是歐元計價，
與並排的另外兩個不同幣別。後端已正確傳遞（`MetricItem.unit_code = item.currency.lower()`），
前端會依 `web-presentation.md` 補上標示。**但更好的做法是換成美元計價的銅商品代碼**，
讓宏觀快照三個數字同幣別。請確認 Twelve Data 是否有美元計價的銅，若有就換掉。

**銅的漲跌幅是 `0.0000`**。實際畫面上銅的 change 是零。可能是供應商該商品的
`percent_change` 未更新，也可能是 `HG1` 這個代碼本身資料品質不佳。
換代碼時一併驗證。

## 8. 實作步驟

1. 確認 `/quote` 是否支援批次 symbol
2. 確認美元計價的銅代碼是否存在
3. `launch_manifest.py`：保留 `us.mega_cap_quotes` DatasetManifest 與 `us.mega_caps`
   BlockManifest，移除 `us.market_movers` 的 block 與 dataset
4. `launch_manifest.py`：`version` bump
5. `morning_report.py`：建立權值股 dataset 的 build 分支，移除 `us.market_movers` 分支；
   `MORNING_REPORT_DERIVATION_VERSION` bump；顯示精度 `precision` 由 4 改為 **2**
   （價格與漲跌幅都不需要四位小數）
6. `adapter.py`：`get_stock_movers`、`Mover`、`MoversResult`、`TwelveDataMovers`
   在確認無其他呼叫者後移除（**先 grep**）；若決定保留備用則不動
7. 測試

### `formula` 欄位要寫實話

新 block 的 `formula` 不要再寫 `as returned by provider`。要寫明籃子與基準，例如：

```
formula="fixed basket of four index-proxy ETFs and VIX; latest provider quote close and percent_change"
```

若後續決定自行計算漲跌幅（見下），也要在這裡反映。

## 9. 待決策

**漲跌幅要沿用供應商的 `percent_change`，還是自己算？**

沿用的問題是定義不透明（基準為何、是否還原除權息，供應商文件未載明），
對要賣給金融機構的產品是個弱點 —— 客戶問「你的漲跌幅怎麼算的」時答不出來。

若 `/quote` 的回應包含 `previous_close`（**本規格未查證，請確認**），
可以自行計算 `(close - previous_close) / previous_close`，讓定義變成我們自己的、
可稽核的，並寫進 `formula`。代價是可能與其他終端顯示的數字有微小差異。

建議：**先確認 `/quote` 有沒有 `previous_close`**，有的話自行計算並在 `formula`
中寫明公式；沒有的話沿用供應商欄位，但在 `formula` 中註明「定義由供應商決定」。

## 10. 驗收

- `/zh-hant/reports/us_equity` 顯示大盤代理與權值股，不再出現股價低於 5 美元的標的
- manifest 通過 `validate_references`，`sha256` 與 `version` 已更新
- 產生新 revision 而非覆蓋既有 edition
- 價格與漲跌幅顯示兩位小數
- 免費層額度下能完整跑完一次 edition 生成（或已確認需要的方案層級）
- 全專案 grep `market_movers` 無殘留（若選擇移除）
- `make check` 通過

依 `AGENTS.md`：分支 `<type>/<summary-kebab-case>`、commit `<type>: <summary>`、
不使用 `--no-verify`、不放寬既有的契約檢查（那些嚴格檢查是刻意的，
不要為了讓籃子跑得過而放寬 `DataSourceContractError` 的條件）。

## 11. 本規格未查證的項目

- Twelve Data `/quote` 是否支援逗號分隔的批次 symbol 查詢
- `/quote` 回應是否包含 `previous_close`
- Twelve Data 是否提供美元計價的銅商品代碼
- 上述建議籃子中各 symbol 在 Twelve Data 的實際可用性與 `expected_asset_types` 值
  （ETF 與 Common Stock 的 type 字串需實測後填入 manifest）
