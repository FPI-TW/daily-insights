# Daily Insights API

本目錄為 FastAPI 模組化單體服務，負責身份、租戶、市場政策、對話、
全域模型設定與共用 R2 檔案中繼資料。

## 系統邊界

- 本服務擁有應用程式身份、組織、授權事實、報告與對話中繼資料、
  加工結果及檔案中繼資料。
- 原始市場資料由外部服務提供。來源 adapter 必須轉換其契約，本服務不會
  持久保存來源的 raw row。
- R2 保存檔案內容，PostgreSQL 保存物件中繼資料及稽核事實。
- 模型選擇為全系統設定，組織與使用者不能自行覆寫。
- 組織沒有市場政策資料時代表預設可見；存在的資料列是經稽核的合約限制。

本專案視為重新啟動，不遷移舊資料庫或物件。

## 本機指令

在 monorepo 根目錄執行：

```bash
make init
make dev
make test-db
make check
```

若只在本目錄開發 API，可使用：

```bash
uv sync
uv run ruff format .
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run alembic upgrade head
uv run uvicorn daily_insights_api.main:app --reload
```

完整 API 測試需要 PostgreSQL，建議由根目錄執行 `make test-db`，避免略過
整合測試。

`DAILY_INSIGHTS_DATABASE_URL` 只會在 `development` 或 `test` 使用本機
預設值；staging 與 production 必須明確提供。

健康檢查：

- `GET /api/health/live`：程序存活狀態。
- `GET /api/health/ready`：包含資料庫連線的 readiness；不可用時回傳 `503`。

容器內部另有不帶 `/api` 前綴的等價端點，且不會出現在 OpenAPI。

## Phase 1：身份與租戶

Phase 1 包含：

- 以 secure、HTTP-only cookie 傳送的不透明 session，資料庫只保存雜湊。
- 所有已登入 mutation 都需要每個 session 專屬的 CSRF token。
- scrypt 密碼雜湊與獨立部署的 pepper。
- 首次登入強制改密碼及 session rotation。
- 由 PostgreSQL 共用的登入限流及 session 撤銷。
- `admin`、`asset_manager`、`org_member` RBAC。
- 組織、內部人員、客戶成員、合約席位與市場政策管理 API。
- 身份事件與高權限變更的 append-only 稽核事件。

初版沒有公開註冊、忘記密碼或郵件流程。Admin MFA、密碼復原及
新加坡區域 Amazon SES 已列為後續工作。

在 monorepo 根目錄執行 migration 並建立第一位 admin：

```bash
make migrate
make bootstrap-admin EMAIL=admin@example.com NAME="Admin"
```

Bootstrap 與帳號建立回應只會顯示一次隨機臨時密碼，資料庫僅保存其雜湊。
帳號完成首次密碼變更前不能使用一般產品 API。

身份端點：

- `POST /api/auth/login`
- `GET /api/auth/me`
- `POST /api/auth/change-password`
- `POST /api/auth/logout`

Admin 端點位於 `/api/admin`。客戶可見市場位於 `GET /api/markets`；
組織身份一律來自有效 membership，忽略客戶端提供的租戶 header。

不連接資料庫、只產生 migration SQL：

```bash
uv run alembic upgrade head --sql
```
