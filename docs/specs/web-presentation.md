# 前端呈現層修正規格

> 給實作者（Claude Code）的交辦文件。撰寫日期 2026-09-03。
> 本規格基於 2026-09-03 對 `https://daily-insights.geailab.com` 正式站的實際操作，
> 所有引用的畫面數值都是當下看到的真實值。

## 0. 這份規格要解決什麼

產品的資料管線是對的，但**呈現層讓正確的資料看起來不專業**。對一個要賣給投顧與
金融機構的產品，這比功能缺漏更傷 —— 客戶第一眼看到的是數字怎麼排版，不是背後有幾個資料源。

實際看到的畫面（`/zh-hant/reports/global_macro_bonds`）：

```
宏觀快照
布蘭特原油          黃金              銅
94.3679            4437.4020        24.4000
0.0397             1.1312           0.0000
```

三個問題同時出現在這六個數字裡：四位小數且無千分位；第二行是漲跌幅卻沒有 `%`
也沒有正負號與顏色；**而且銅是歐元計價、另外兩個是美元，畫面完全沒有標示**。

**關鍵發現：這些資料後端都已經送過來了，是前端沒有使用。**

- `MetricItem.unit_code` 由 `morning_report.py` 以 `item.currency.lower()` 填入，
  所以銅實際上帶著 `"eur"`、布蘭特與黃金帶著 `"usd"`。
- `TableColumn.unit_code` 在表格區塊中已明確標為 `"usd"` 與 `"percent"`。
- `ReportSummaryResponse.stale` 與 `stale_reason` 已由 `reports/router.py` 計算並回傳。
- `NewsItem.numeric_facts` 與 `source_hostname` 已存在資料庫。

因此第一層的工作**不需要任何後端改動**，只是把已經在 payload 裡的欄位接起來。

## 1. 不要做的事

- 不要為了顯示而在前端做四捨五入以外的數值運算。契約刻意用 Decimal 字串傳遞
  （`contracts.py` 的註解說明了原因：避免 locale 格式化與二進位浮點誤差），
  前端只做**顯示層格式化**，不要 parseFloat 之後再算。
- 不要動 `apps/api`。第二層需要的後端改動另列，但那是獨立 PR。
- 不要新增 UI 函式庫。依 `AGENTS.md` 的 Dashboard UI 規則，樣式優先用 Tailwind utilities，
  重複樣式抽成共用元件或 shadcn variant，不要用常數保存 class name 字串。
- 不要移除既有的三語切換、`/[locale]/reports/[market]` 路由結構或「報告問答」入口。

---

# 第一層 — 純前端，不需後端改動

## 2.1 數值格式化層（核心，其他項目都依賴它）

建立一個共用的格式化模組，所有數值一律經過它，**不得在元件內直接印 Decimal 字串**。

### 顯示精度

目前的四位小數來自 manifest 的 `precision=4`，那是**計算精度不是顯示精度**。
顯示精度改由 `unit_code` 決定：

| unit_code                    | 顯示規則                                               | 範例（現況 → 應為）                                                    |
| ---------------------------- | ------------------------------------------------------ | ---------------------------------------------------------------------- |
| `usd` / `eur` / 其他三碼幣別 | 千分位 + 2 位小數；絕對值 < 10 時 4 位小數             | `4437.4020` → `4,437.40`<br>`94.3679` → `94.37`<br>`0.2046` → `0.2046` |
| `percent`                    | 2 位小數 + `%`                                         | `98.6301` → `98.63%`                                                   |
| `index`                      | 千分位 + 2 位小數                                      | `107.3444` → `107.34`                                                  |
| `usd_percent`                | 表格層級的複合單位，實際精度看 `TableColumn.unit_code` | —                                                                      |
| 未知                         | 千分位 + 2 位小數，並在 dev 環境 console.warn          | —                                                                      |

小額資產的例外規則很重要：ADA 現價 `0.2046`，硬套 2 位小數會變成 `0.20`，
把有意義的位數砍掉。所以規則是「絕對值 < 10 用 4 位」。

### 千分位與字型

- 一律使用 `Intl.NumberFormat`，locale 取自路由的 `[locale]` 區段
  （`zh-hant` → `zh-Hant-TW`，`zh-hans` → `zh-Hans-CN`，`en` → `en-US`）。
- 數字欄位加 `tabular-nums`（Tailwind `tabular-nums`），讓表格中的位數對齊。
  目前 `77590.3600` 與 `2398.7800` 在同一欄但沒有對齊。

## 2.2 漲跌語意

目前「變動」欄顯示 `98.6301` / `-95.1184` / `0.0000`，缺三樣東西。

### 三個規則

1. **正號要顯示**。`+0.32%` 與 `0.32%` 在金融介面裡是不同的訊息密度。
   用 `Intl.NumberFormat` 的 `signDisplay: "exceptZero"`。
2. **百分比要有 `%`**。`MetricBlock` 的 `change` 一律是漲跌**百分比**
   （`morning_report.py` 填的是 provider 的 `percent_change`），與 `value` 的
   `unit_code` 無關 —— 這點要寫進格式化模組的註解，否則很容易被誤解成同單位。
   表格則看 `TableColumn.unit_code === "percent"`。
3. **零值要有語意**。銅目前顯示 `0.0000`。零值顯示為 `—` 或 `持平`，並用中性色，
   不要跟真實的 0.00% 混淆；若 `change` 為 `null` 則顯示 `—`。

### 顏色慣例（依 locale 切換）

目前只有負數是綠色、正數沒有顏色。要成對處理：

| locale                | 上漲 | 下跌 |
| --------------------- | ---- | ---- |
| `zh-hant` / `zh-hans` | 紅   | 綠   |
| `en`                  | 綠   | 紅   |

用 design token 定義 `--color-up` / `--color-down`，在 locale 層切換，
**不要在元件內寫死色碼**（`AGENTS.md` 明文禁止在元件內複製 light/dark 色碼）。
顏色不得是唯一的訊息載體 —— 正負號本身已經滿足這點，但仍要確認色盲對比。

## 2.3 單位與幣別標示

**這是目前最可能造成實質誤解的一項。**

宏觀快照三個數字並排，銅是歐元、其餘是美元，畫面沒有任何標示。讀者會合理地
假設三個都是美元。

- `MetricItem.unit_code` 已帶 `"usd"` / `"eur"`，直接渲染成幣別標記。
  建議放在數值後方或標籤旁的小字，例如 `4,437.40 USD` 或 `黃金 (USD)`。
- 同一個 metric block 內出現**不同幣別**時，必須逐項標示，不能只標一次。
- 表格的 `TableColumn.unit_code` 同樣要渲染到欄位標頭：
  「價格 (USD)」、「變動 (%)」。
- 圖表左下角目前顯示英文 `單位：index`。`index` 是 unit_code 不是給人看的文字，
  應經過 unit_code → 顯示標籤的對照表翻譯（繁中「指數」、簡中「指数」、英文「Index」）。

> `PresentationContract.LocalizedElementText` 有 `unit_label` 欄位，
> 但 `morning_report.py` 從未填入（只填 `title`）。第一層**不要依賴它**，
> 前端自行維護 unit_code → 標籤的對照表。是否改由後端填見 §3.3。

## 2.4 狀態、新鮮度與空狀態

### 已經在 API 裡但沒渲染的

`ReportSummaryResponse` 有 `stale: bool` 與 `stale_reason: string | null`，
`router.py` 已依 `result.freshness.status` 計算。前端目前完全沒顯示。

對金融客戶而言，「這筆是 T-1 收盤」與「這筆是即時」的差別是信任問題。
建議在報告標題列顯示資料截止時間（`source_as_of`），`stale === true` 時
加上明顯但不驚嚇的標記與 `stale_reason` 說明。

### 狀態徽章要能解釋自己

目前新聞區塊右上角有「部分可用」「暫不可用」徽章，但沒有說明為什麼。
`PublicationContent.status` 與每個 block 的 `status`（`ok` / `missing` / `error`）
加上 `caveat`（上限 2000 字）已經在 payload 裡。

- `status: "partial"` → 徽章可 hover / 點擊展開，說明哪些區塊缺漏
- block `status: "missing"` → 該區塊顯示「今日未取得」而非空白或 0
- block `status: "error"` → 顯示錯誤狀態並帶 `caveat`
- `caveat` 不為 null 時一律要有出口讓使用者看到，目前完全沒渲染

**這一項會真的用到。** 上游資料源實測會缺日（TWSE 的 T86 在 2026-09-02 就查無資料），
屆時前端必須能誠實表達「這塊今天沒有」，而不是顯示 0 或空白。

### 空狀態

台股頁目前是「報告尚未推出 — 此市場的正式晨間報告仍在準備中，目前先提供每日市場新聞。」
文案本身沒問題，保留。但要確認：

- 首次載入必須有 skeleton（`AGENTS.md` 明文要求，且需 `role="status"` / `aria-live`）
- 第一次請求完成前**不得**顯示錯誤占位或「無資料」
- 後續重新整理保留既有資料並顯示局部 pending，避免閃爍

## 2.5 缺陷修正

| 缺陷                           | 觀察到的現象                                                                                                                       | 位置                                           |
| ------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------- |
| **重複的 tab bar**             | 台股頁同時渲染兩層市場切換列：頁面層一組（全部市場／宏觀分析／美股／台股），卡片內又一組。其他市場頁只有一層                       | `/zh-hant/reports/tw_equity`                   |
| **加密市場是孤兒頁**           | `/zh-hant/reports/crypto` 有完整資料（加密資產概況表 + 標準化表現圖），但導覽列只有四個 tab，沒有加密。使用者永遠走不到這頁        | 市場導覽 `ref` 為「市場分類導覽」的 navigation |
| **unit_code 未翻譯**           | 圖表左下「單位：index」                                                                                                            | 所有 series block                              |
| **Podcast 卡片資訊重複且單薄** | 標題是「Podcast \| 2026-09-03」，日期在 eyebrow 與副標各出現一次，共三次。沒有長度、沒有摘要                                       | `/zh-hant/podcasts`                            |
| **新聞卡片缺時間**             | 全球版第一則（AP）顯示「時間未提供」。`source_published_at` 為 null 時應顯示相對描述或隱藏該行，不要顯示「未提供」這種內部狀態措辭 | 新聞區塊                                       |

加密 tab 補上時，導覽的市場清單應該由 API 回傳的市場列表驅動，
而不是前端硬編碼四個 —— 否則第二階段擴充到 8 大市場時會再犯一次同樣的錯。

---

# 第二層 — 需要後端配合

## 3.1 兩個免費的欄位（只改 schema，不用 migration）

`NewsItem` 資料表已經儲存這兩個欄位，但 `NewsItemResponse` 沒有暴露：

| 欄位              | 資料表型別           | 前端用途                                                                                                                 |
| ----------------- | -------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `numeric_facts`   | `JSONB`，`list[str]` | 晨報最有價值的內容 —— 「跌 3.2%」「升息 1 碼」這類數字重點。可在新聞卡片中做成 chip 列，讓讀者不必讀完摘要就抓到量化訊息 |
| `source_hostname` | `String(255)`        | 來源分組、favicon、以及「同一事件多家報導」的聚合                                                                        |

改動範圍：`modules/news/schemas.py` 的 `NewsItemResponse` 加兩個欄位，
以及 router 中組裝回應的位置。**不需要 migration，不需要重新生成 edition。**
這是本規格投報比最高的一項。

## 3.2 兩個需要 migration 的欄位

`SelectedCandidate`（`contracts.py`）在選稿階段就產出了 `market` 與 `event_key`，
但 `NewsItem` 沒有儲存這兩者，所以前端拿不到：

- **`market`**（`global` / `us` / `asia` / `china` / `taiwan` / `europe` / `commodities` / `crypto`）
  —— 缺了它，前端無法在同一份 edition 內做市場分組，也無法做「這則新聞屬於哪個市場」的標記。
- **`event_key`** —— 缺了它無法做「同一事件跨日追蹤」，那是晨報的差異化功能之一
  （「這是昨天那則關稅新聞的後續」）。

需要 Alembic migration 新增兩個欄位、修改 `service.py` 的持久化邏輯，
並在 `NewsItemResponse` 暴露。`event_key` 已有格式約束（`^[a-z0-9][a-z0-9_-]{2,79}$`），
`market` 建議加 CHECK constraint 與 `SelectedCandidate` 的 Literal 對齊。

⚠️ 這兩個欄位只對**新產生**的 edition 有值，既有資料不會回填。前端要能處理 null。

## 3.3 `unit_label` 的歸屬決策（需要拍板）

`PresentationContract.LocalizedElementText.unit_label` 欄位存在於契約中，
但 `morning_report.py` 建構 `LocalizedElementText` 時只填 `title`，從未填 `unit_label`。

兩條路，選一條：

- **A（建議，成本低）**：前端維護 unit_code → 三語標籤的對照表。
  目前 unit_code 的值域很小（`usd`、`eur`、`percent`、`index`、`usd_percent`、
  `provider_quote_currency`），前端 map 就夠，而且改動不需要重新生成 edition。
- **B**：後端在 manifest 中為每個 block 補 `unit_labels`，由 `morning_report.py` 填入
  `LocalizedElementText.unit_label`。好處是三語文案集中在後端與 manifest 一起版本控管，
  壞處是要動 manifest 與 `MORNING_REPORT_DERIVATION_VERSION`（會產生新 revision）。

**第一層先走 A。** 若之後市場數擴充到 8 個、unit_code 值域變大，再評估切換到 B。

---

# 4. 驗收

## 第一層

- 全站不存在直接輸出 Decimal 字串的位置；所有數值經過格式化模組
- 宏觀快照的三個數字顯示千分位、正確小數位、以及各自的幣別（銅必須看得出是 EUR）
- 所有漲跌值有正負號、`%`、依 locale 的紅綠慣例；零值顯示為中性的 `—` 或「持平」
- 圖表單位文字經過翻譯，三語各自正確
- 台股頁只有一層 tab bar
- 導覽列包含加密市場，且市場清單由 API 驅動而非硬編碼
- `stale`、`source_as_of`、`caveat`、block `status` 皆有對應的視覺呈現
- 首次載入有 skeleton 且帶 `role="status"` / `aria-live`
- 三語切換後，數字格式、顏色慣例、單位標籤全部跟著切換

## 第二層

- 新聞卡片顯示 `numeric_facts` chip
- 新聞可依 `market` 分組（migration 後）
- 既有無 `market` 的舊資料不會讓畫面壞掉

## 通用

`make check` 通過（format、lint、type、test、build）。依 `AGENTS.md`：
分支 `<type>/<summary-kebab-case>`、commit `<type>: <summary>`、不使用 `--no-verify`、
不放寬 TypeScript 或 lint 約束。

# 5. 附錄：2026-09-03 實測的路由與內容

| 路由                                   | 內容                                                                                          |
| -------------------------------------- | --------------------------------------------------------------------------------------------- |
| `/[locale]/login`                      | 三語切換已在登入卡片右上                                                                      |
| `/[locale]/reports`                    | 分析師觀點（全球宏觀／美國股市／台灣股市 三張敘述卡）+ 本日重大新聞（3 則，狀態「部分可用」） |
| `/[locale]/reports/global_macro_bonds` | 宏觀快照（布蘭特／黃金／銅）+ 布蘭特原油與黃金標準化表現（含 tooltip 與 brush）               |
| `/[locale]/reports/us_equity`          | 漲跌焦點表格（4 檔）+ 美股重點新聞（空，狀態「暫不可用」）                                    |
| `/[locale]/reports/tw_equity`          | 「報告尚未推出」空狀態 + 台股重點新聞（3 則，來源鉅亨）                                       |
| `/[locale]/reports/crypto`             | 加密資產概況（5 檔）+ 標準化表現 —— **導覽列沒有入口**                                        |
| `/[locale]/podcasts`                   | 每日 Podcast 清單，卡片有「開始收聽」按鈕                                                     |
| `/[locale]/account`                    | 未檢視                                                                                        |

導覽：晨間報告 / Podcast / 帳戶 / 設定（按鈕），另有浮動的「報告問答」按鈕。

## 不在本規格範圍、但需要另行處理的

`/[locale]/reports/us_equity` 的「漲跌焦點」列出 `BURUD`（1.4500，+98.63%）、
`DSX.WT`、`EYES`（-95.12%）、`ADBT` —— 全是股價 1–2 美元的微型股。

這是 Twelve Data `market_movers` 端點的本質：依漲跌**幅**排序的 top movers
永遠是雞蛋水餃股。對投顧晨報沒有參考價值，**但這是資料選擇問題，前端無法修正**。
需要在 manifest 層改為指數成分股或市值前段標的的變動。此項屬於後端 manifest 擴充範圍。
