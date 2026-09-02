# Daily Insights

Daily Insights 是重新啟動的多語系市場報告產品 monorepo。本專案整合既有報告前端的概念，並以模組化單體架構重新建置 API。先前的 `report-*` 服務僅作為需求與設計參考，其資料及執行期契約不會遷移至此工作區。

## 工作區結構

```text
apps/
  web/              TanStack Start 客戶端與內部管理後台
  api/              模組化單體 API
packages/
  api-client/       產生或共用的 API 契約
docs/
  architecture/     架構決策、邊界與開發路線圖
  runbooks/         維運操作手冊
infra/
  nginx/            開發環境入口基礎設定
```

新增領域或服務前，請先閱讀[系統架構](docs/architecture/system-architecture.md)與[實作路線圖](docs/architecture/roadmap.md)。

## 初始化與開發

TanStack Start 應用程式位於 `apps/web`，模組化 FastAPI 基礎位於 `apps/api`。首次取得專案後，在根目錄執行：

```bash
make init
```

此指令會建立三份互不共用的未追蹤設定檔、安裝 pnpm 與 uv 依賴，並啟用
版本控制內的 Git hooks：

- 根目錄 `.env`：僅供 Compose 與 PostgreSQL 基礎設施使用。
- `apps/web/.env`：僅供 TanStack Start Web 服務使用。
- `apps/api/.env`：僅供 FastAPI 服務使用。

請先替換各檔案中的 `CHANGE_ME` 預留值，再啟動完整開發環境：

```bash
make dev
```

常用指令：

```bash
make help          # 顯示全部指令
make dev-web       # 僅啟動 TanStack Start，後端位址由 API_INTERNAL_URL 指定
make dev-api       # 僅啟動 FastAPI
make migrate       # 升級 API 資料庫 schema
make generate-morning-reports # 本地以 Twelve Data 單次產生三市場晨報
make generate-daily-news      # 本地以 DeepSeek 單次產生本日重大新聞
make test-db       # 以隔離 PostgreSQL 執行完整測試
make check         # 執行格式、lint、型別、測試與建置
make stop          # 停止 Compose 開發環境
```

單獨啟動 Web 前，需先確保 `apps/web/.env` 的 `API_INTERNAL_URL` 指向可連線
的 API 或 nginx origin；Vite 開發伺服器會將瀏覽器的 `/api` 請求代理至該
位址。預設範例會連至 `http://localhost:8080`。單獨啟動 API 時則只會載入
`apps/api/.env`，不會讀取 Web 或根目錄的應用程式設定。

首次建立內部管理員時，先完成 migration，再執行：

```bash
make bootstrap-admin EMAIL=admin@example.com NAME="Admin"
```

本地需要驗證正式晨報資料時，可執行 `make generate-morning-reports`；也可傳入
`EDITION_DATE=YYYY-MM-DD` 指定台北報告日期。此指令只允許 development／test
環境的一次性執行，會使用 Twelve Data 正式 credential 並消耗 API credits；不會
啟用背景排程。正式環境啟用晨報時只驗證功能開關、provider URL 與 API key。

指令只會顯示一次隨機臨時密碼；管理員登入後必須立即更改。

本地需要驗證每日重大新聞時，可執行 `make generate-daily-news`；它同樣只允許
development／test 環境的一次性執行，使用 `apps/api/.env` 中的 DeepSeek credential，
並只會產生台北時間當日的版本。正式環境以 `DAILY_INSIGHTS_DAILY_NEWS_ENABLED`
旗標啟用，詳見[每日重大新聞架構與部署](docs/architecture/daily-news.md)。

目前已完成身份／租戶、結構化報告基礎與 Podcast 先行版的本機實作。正式 R2
音檔與 browser E2E 驗收狀態請以各階段文件為準；領域出現在文件中不代表已經
完成上線驗收。

## Git hooks

專案使用版本控制內的原生 Git hooks，設定位於 `.githooks/`。執行 `make init` 或 `pnpm install` 時，會自動將此儲存庫的 `core.hooksPath` 設為 `.githooks`。

- `pre-commit`：檢查格式、lint 與型別。
- `pre-push`：執行 pre-commit 的全部檢查，並額外執行測試。

`pre-push` 會透過 Docker 建立一次性的 PostgreSQL，確保 Phase 1
與 Phase 2 整合測試不會因本機未設定測試資料庫而被略過；測試結束後容器會
自動移除。

若未透過 `pnpm install` 初始化環境，可手動啟用：

```bash
git config core.hooksPath .githooks
```

## 開發環境基礎設施

`make init` 會分別由三份 `.env.example` 建立根目錄基礎設施、Web 與 API
設定，且不會覆寫既有檔案。替換所有預留值後，可透過 Make 指令管理完整服務：

```bash
docker compose config
make dev
```

開發環境入口預設為 `http://localhost:8080`。只有 nginx 對外開放，PostgreSQL 保留在 Compose 內部網路。

Compose 設定僅用於開發環境，不代表正式環境拓撲。正式部署方式請參考[新加坡部署操作手冊](docs/runbooks/production.md)。

## 架構文件

- [已確認決策與待確認事項](docs/architecture/product-decisions.md)
- [系統與領域邊界](docs/architecture/system-architecture.md)
- [分階段實作與驗收路線圖](docs/architecture/roadmap.md)
- [Phase 2 資料來源與結構化報告](docs/architecture/phase-2-data-reports.md)
- [Phase 2B 完整應用架構與驗收](docs/architecture/phase-2b-application-architecture.md)
- [Podcast 先行版範圍與決策清單](docs/architecture/podcast-pilot.md)
- [每日重大新聞架構與部署](docs/architecture/daily-news.md)
- [正式環境維運操作手冊](docs/runbooks/production.md)
- [專案審查基準（2026-09-02）](docs/reviews/2026-09-02-project-review.md)
