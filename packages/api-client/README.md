# API Client 契約

此套件是 FastAPI OpenAPI 與 Web 應用之間唯一的 TypeScript 契約邊界。

執行下列指令會以本機 FastAPI application 產生排序穩定的 `openapi.json`，再由
`openapi-typescript` 產生 `src/generated.ts`：

```bash
pnpm generate:api-client
```

CI 與完整檢查會執行：

```bash
pnpm api-client:check
```

此指令在暫存目錄重新產生兩份檔案並逐位元比較；API schema 改變但未更新 client
時會失敗。

產生的 TypeScript 型別不會在執行期驗證資料，因此公開 client 另以 Zod 驗證
所有實際使用的 response trust boundary。不得在此套件複製 FinDB、資料庫或其他
內部 persistence 型別。

瀏覽器 transport 固定使用同源 `/api` 路徑及 `credentials: "same-origin"`，
不接收內部 API URL。Server transport 的 base URL 只能由 TanStack Start
server-only handler 提供，且只轉送已驗證的 session Cookie、request ID，以及
必要的 `Accept`、`Content-Type`、`X-CSRF-Token`；呼叫端無法覆寫 Cookie 或夾帶
Authorization、Host、`X-Forwarded-*` header。
