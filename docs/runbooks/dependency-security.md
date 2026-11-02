# 依賴安全性維護

## 2026-11-02 修復

- [GHSA-mrhx-c7xc-m3w5](https://github.com/advisories/GHSA-mrhx-c7xc-m3w5)：
  Zod `<4.6.6` 有記憶體耗盡風險。Web 與 API client 的直接依賴下限提高至
  `^4.6.7`，鎖定檔統一使用 `4.6.7`。
- [GHSA-g3hv-75wg-m249](https://github.com/advisories/GHSA-g3hv-75wg-m249)：
  minimatch `<11.0.1` 的 glob 預處理可能耗盡資源。針對 ESLint、
  `@eslint/config-array` 與 Redocly 的依賴加入限定範圍的 override，鎖定至
  `11.0.3`。

`@redocly/openapi-core@1.34.21` 原本呼叫 minimatch 的 CommonJS 函式匯出，
新版改用具名匯出。因此 `patches/@redocly__openapi-core@1.34.21.patch`
同步調整執行用 JavaScript 與 TypeScript 原始碼的 import。
Web Docker 建置在安裝依賴前會複製 `patches/`，確保 frozen lockfile 安裝可套用補丁。
`packages/api-client/tests/redocly-compatibility.test.mjs` 驗證遠端 schema 的
header pattern 會正確匹配或排除，避免只測試本地 OpenAPI 產生而漏掉此路徑。

後續升級 Redocly 時，須確認上游已支援修補版 minimatch，再移除對應 override
與 patch；若上游仍使用舊介面，應重新檢查並更新 patch，保留相容性測試。

## 驗證方式

```bash
pnpm install --frozen-lockfile
pnpm audit
pnpm format:check
pnpm lint:check
pnpm type:check
pnpm api-client:check
make test-db
pnpm build
```

`pnpm audit` 涵蓋正式與開發用 npm 依賴，不代表已完成 Python 依賴或應用程式碼的
安全性稽核。Dependabot 告警會在修復合併至預設分支並重新分析後更新。
