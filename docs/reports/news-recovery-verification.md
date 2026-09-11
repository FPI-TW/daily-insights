# 新聞恢復機制驗收紀錄

驗收日期：2026-09-11（Asia/Taipei）。作業工作樹為 PR #76 延續分支
`feat/continue-pr-76`。操作方式見 [新聞失敗與恢復操作](../runbooks/news-recovery.md)。

## 程式與契約檢查

- `pnpm format`、`pnpm check` 通過；涵蓋 format:check、零警告 lint、TypeScript、
  Python mypy、OpenAPI／產生 client 一致性、正式 web／API 建置及 client secret scan。
- 後端：696 項測試通過，使用隔離 PostgreSQL 與模擬來源／模型，不使用真實 DeepSeek。
- 前端：223 項測試通過；API client：27 項測試通過。
- Chromium E2E：40 項通過。新增三語管理頁驗證：503 後依 2、5 秒重試、初次 skeleton、
  成功進度及供應商暫停、CSRF 恢復 POST、失敗請求編號；控制時鐘前進 31 秒，POST
  仍僅送出一次。既有報表、音檔與登入邊界亦納入回歸。
- 新 migration `20260911_0025` 在隔離資料庫升級、schema check、空恢復資料狀態的
  降版再升級通過。已有恢復狀態時拒絕破壞性降版，不清除正式新聞／稽核歷史。

後端案例包含失敗分類、退避／Retry-After、共用 gate 與 probe、修正次數不重置、
內容／政策失效、語系進度重用、租約及取消、正常少量或零則、來源全面故障、排程補建、
中午／跨日邊界、人工上架去重／配額及隱藏新聞不復活。

## 本機代理與更新

- 本機與 production 固定 Nginx 映像一致；兩種設定均在隔離 Docker 網路替換 API／web
  並佔住舊 IP，驗證不 reload 可恢復，且錯誤 readiness 不會被當成健康。
- Nginx configuration／production deployment contract、媒體大小與路由限制回歸通過。
- 依序更新 API（先 migration）、worker、新聞排程器、web 與 Nginx；均為 healthy。
  PostgreSQL volume 未重建，沒有執行新聞重抓或付費模型恢復。
- 真實入口 `http://localhost:8080/zh-hant/login` 為 200；未登入
  `/api/admin/news/recovery` 為 401；公開存取 `/nginx-health/api` 為 404。
- Nginx 容器內的 API readiness 與 web 代理 health 均成功；資料庫版本為
  `20260911_0025`。

更新後以正式新聞查詢邏輯唯讀檢查本機資料，繁中／簡中／英文結果一致：

| 市場 | 讀取版本                      | 可見新聞 |
| ---- | ----------------------------- | -------- |
| 全球 | 2026-09-11，revision 1        | 5 則     |
| 台股 | 2026-09-11，revision 1        | 5 則     |
| 美股 | 回退至 2026-09-10，revision 4 | 10 則    |

讀者端不新增日期、新鮮度、更新延遲或技術錯誤提示；上述日期僅為本驗收紀錄。

## 驗收限制與人工確認

內嵌瀏覽器工具因工作區 URI 初始化錯誤，無法存取使用者現有登入頁面。隔離瀏覽器可運作，
但沒有使用者的登入 session；未擷取 cookie、建立正式測試帳號或改動認證。三語登入後 UI
由模擬 API 的 E2E 驗證，本機 API 與資料由上述入口及唯讀查詢驗證，兩者不等同於已驗證
使用者現有的登入 session。

仍請在已登入的本機頁面重新整理新聞管理頁，確認各市場進度、來源面板及作業歷史，
再至台股、美股檢查新聞與循環分頁。單純驗收不需要按「重新抓取」或「恢復」。

## 同次驗證發現的既有回歸

另外獨立修正台股英文公司名稱在窄版表格溢出，以及過時的 dashboard E2E：現行圖表數量、
標題、18 個月預設值、已移除的說明／日期 footer。保留真實滑桿拖曳、畫布改變及不重抓
資料的驗證，不跳過測試或放寬型別／lint 約束。
