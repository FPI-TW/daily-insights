# 儀表板設計交付驗收紀錄

驗收日期：2026-09-07。範圍為宏觀、債券與外匯頁及台股技術面兩份設計交付。

## 實作結果

- Step 1（功能）：宏觀頁分為今日重點、商品、利率、外匯；重點指標列、圖表期間控制、殖利率比較曲線、外匯雙欄聯動與折疊說明。
- Step 2（修正）：三語字型與 ISO 日期統一；英文使用 Latin 字型優先並修正彎引號顯示。
- Step 3（功能及測試）：三條乖離線、0–100 刻度讀數、12/18/24 個月視窗、縮放同步讀數，以及日 K、API 均線與成交量副圖。
- API 與契約未變更。20/60/120MA 直接使用既有 API 的 sma-close-v1 結果；前端只計算乖離。
- 沿資料來源、adapter、DTO 與資料儲存流程確認 volume 為 Yahoo Finance Volume 成交股數，圖上以億股顯示，不以成交金額換算。

## 驗證

| 檢查                   | 結果                                                                                 |
| ---------------------- | ------------------------------------------------------------------------------------ |
| pnpm format、pnpm lint | 通過                                                                                 |
| make check             | 通過，含 format:check、lint:check、型別、契約生成、測試、前後端 build 與部署設定檢查 |
| Web Vitest             | 157 項通過                                                                           |
| API client Vitest      | 19 項通過                                                                            |
| 一般 API pytest        | 412 項通過，89 項資料庫案例略過                                                      |
| make test-db           | 501 項 API 案例全部通過，包含資料庫 migration 升降版驗證                             |
| Playwright 完整測試    | 32 項通過                                                                            |

新增純函式測試涵蓋 MA 為 null／日期未對齊、不變區間回傳 50、窗長改變刻度、可見區間縮放及月底日期邊界。瀏覽器測試實際拖曳 slider，確認讀數改變且不新增指數資料請求。

三語兩頁各以 1440、1280、1024 px 截圖，共 18 張；檢查初次載入、圖表完成渲染、字型、英文彎引號字寬、紅綠 token、頁面與表格儲存格溢出及執行期錯誤。截圖使用可重現的 E2E 測試資料，並非即時市場數據。

截圖保存在本機 `output/playwright/dashboard-handoff/`。基本檔名為 `{locale}-{market}.png`，窄版為 `{locale}-{market}-1280.png` 與 `{locale}-{market}-1024.png`；locale 為 zh-hant、zh-hans、en，market 為 global_macro_bonds、tw_equity。測試也會重新產生 `apps/web/test-results/` 中的截圖。

## 規格差異與原因

1. 依確認過的詳細 README，殖利率曲線使用日／週／月／年比較控制；其餘三圖使用 30／90／365 天期間。曲線是期限截面，期間控制不能直接套用時間序列語意。
2. 表格、meta、tooltip 使用 YYYY-MM-DD；依詳細圖表規格，時間軸保留 MM-DD 或 YYYY-MM 縮寫，避免密集標籤重疊。句子內日期與月份分組標題保留語系格式。
3. 1024–1280 px 的窄面板調整表格欄寬與變動值字級（11px），bp 單位使用較小的 sans 字體，避免原比例導致數值溢出；寬面板保留較大的欄位配置。
4. 警示標籤沿用現有 token，未新增原型的褐色文字色碼。
5. 刻度讀數代表縮放後最後可見交易日，明確顯示日期；不沿用原型固定「今日」文字，避免瀏覽歷史視窗時誤導。
6. 原型中的成交金額文案改為成交量（億股），符合既有來源實際單位。均線不足與缺值保持空白，不補零、不重新計算 SMA。
7. IndexHistoryChart 為台股與美股共用元件；日 K 與均線圖同步使用共用實作，保留多指數市場選擇器與動態名稱。
8. 外匯資料若日期不一致，保留各自日期資訊，不虛構共同資料日期。

目前沒有必須由使用者裁決的未決取捨。

## 實際改動檔案

- `apps/web/e2e/dashboard-handoff.spec.ts`
- `apps/web/e2e/mock-api.mjs`
- `apps/web/e2e/podcast.spec.ts`
- `apps/web/e2e/reports.spec.ts`
- `apps/web/playwright.config.ts`
- `apps/web/src/components/AnalystViewpointManagementPage.tsx`
- `apps/web/src/components/AppShell.tsx`
- `apps/web/src/components/DailyNews.tsx`
- `apps/web/src/components/DashboardPrimitives.tsx`
- `apps/web/src/components/IndexDataManagementPage.test.tsx`
- `apps/web/src/components/IndexDataManagementPage.tsx`
- `apps/web/src/components/IndexHistoryChart.test.tsx`
- `apps/web/src/components/IndexHistoryChart.tsx`
- `apps/web/src/components/MacroDashboard.test.tsx`
- `apps/web/src/components/MacroDashboard.tsx`
- `apps/web/src/components/MemberManagementPage.tsx`
- `apps/web/src/components/PodcastPage.test.tsx`
- `apps/web/src/components/PodcastPage.tsx`
- `apps/web/src/components/Reports.test.tsx`
- `apps/web/src/components/Reports.tsx`
- `apps/web/src/lib/chart.ts`
- `apps/web/src/lib/format.test.ts`
- `apps/web/src/lib/format.ts`
- `apps/web/src/lib/i18n.ts`
- `apps/web/src/lib/indices.test.ts`
- `apps/web/src/lib/indices.ts`
- `apps/web/src/lib/macro-dashboard.test.ts`
- `apps/web/src/lib/macro-dashboard.ts`
- `apps/web/src/routes/$locale/_authenticated/_admin/admin/conversations/index.tsx`
- `apps/web/src/routes/__root.tsx`
- `apps/web/src/styles.css`
- `docs/reports/dashboard-handoff-verification.md`
