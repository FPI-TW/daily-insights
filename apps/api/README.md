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

本專案視為重新啟動，不遷移舊資料庫。Podcast 核准沿用的既有 R2 物件是
例外：受控工具會將它們複製到後端產生的新路徑，逐檔核對 size、MIME type
與 SHA-256，整批 reconciliation 完成後才切換資料庫 mapping。工具不會刪除
來源物件；舊路徑只會在人工確認後由內部人員手動清理。

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

API 僅讀取 `apps/api/.env` 與程序環境中的 `DAILY_INSIGHTS_*` 變數，不讀取
根目錄或 Web 的設定檔。Compose 會以內部 PostgreSQL hostname 覆寫
`DAILY_INSIGHTS_DATABASE_URL`；直接執行 API 時，該 URL 必須指向本機可連線
的 PostgreSQL。

本機測試 Podcast 直傳時，使用獨立的開發 R2 bucket 和僅限該 bucket 的物件
讀寫金鑰。在 `apps/api/.env` 設定 `DAILY_INSIGHTS_R2_ENDPOINT_URL`、
`DAILY_INSIGHTS_R2_BUCKET_NAME`、`DAILY_INSIGHTS_R2_ACCESS_KEY_ID` 與
`DAILY_INSIGHTS_R2_SECRET_ACCESS_KEY`；本地 Compose 的 API 與
`podcast-media-worker` 會讀取同一份設定。開發 bucket 的 CORS 須允許
`http://localhost:${APP_PORT}`（`APP_PORT` 預設為 `8080`），並依
[正式環境 runbook 的直傳 CORS 範例](../../docs/runbooks/production.md#r2-browser-upload-cors)
允許直傳方法與標頭。切勿將金鑰提交到 Git，或用正式 bucket 進行本地上傳測試。
直傳完成後，音檔需由 `podcast-media-worker` 驗證並寫入資料庫，才會出現在
後台清單；新節目完成處理後會發布，並出現在前台 `/podcasts`。

`DAILY_INSIGHTS_DATABASE_URL` 只會在 `development` 或 `test` 使用本機
預設值；staging 與 production 必須明確提供資料庫、獨立 session/password
secret、FinDB API key，以及完整 R2 endpoint、bucket 與 access credentials。
缺漏、placeholder、非 HTTPS 外部端點或不合理的 signed URL TTL 都會在程序
啟動前 fail closed。所有 credential 都使用 secret 型別，禁止出現在
OpenAPI、回應或結構化 log。

健康檢查：

- `GET /api/health/live`：程序存活狀態。
- `GET /api/health/ready`：回傳由 operations 模組擁有的 component readiness；
  目前包含資料庫連線，不可用時回傳 `503`。FinDB/R2 的短暫故障不會讓已發布
  內容失去服務能力，會由各自的模組事件與告警呈現。

容器內部另有不帶 `/api` 前綴的等價端點，且不會出現在 OpenAPI。

## Phase 3：Podcast 先行版

客戶 Podcast API：

- `GET /api/podcasts?locale=zh-hant`
- `GET /api/podcasts/{episode_id}?locale=zh-hant`
- `POST /api/podcasts/{episode_id}/audio-url?locale=zh-hant`

內部 Podcast API 位於 `/api/admin/podcasts`。`admin` 可發布與下架；
`admin` 與 `asset_manager` 可透過 multipart endpoint 一次上傳 1–3 個語系
音檔。後端統一寫入
`podcasts/{trading-date}/audio/{locale}/podcast.{ext}`，其中 `{ext}` 僅支援
`mp3` 與 `mp4`。同交易日同語系確認後
直接覆寫該 object，並同步更新 size、MIME type、SHA-256 與邏輯版本。

客戶只能取得已發布內容與短效、object-scoped R2 URL。requested locale 沒有
對應音檔時依 `zh-hant` → `zh-hans` → `en` 回退，回應會同時標明 requested
與 resolved locale。

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

### 登入資源限制

登入沿用每組 IP/email 的五分鐘 5 次限制，另以資料庫原子計數限制每來源 IP 五分鐘 30 次、全站五分鐘 300 次。成功登入只清除 IP/email 計數，不重置共用額度。對應設定為 `DAILY_INSIGHTS_LOGIN_IP_RATE_LIMIT_ATTEMPTS`、`DAILY_INSIGHTS_LOGIN_GLOBAL_RATE_LIMIT_ATTEMPTS`，時間窗口沿用 `DAILY_INSIGHTS_LOGIN_RATE_LIMIT_WINDOW_SECONDS`。每個 API worker 最多同時執行 2 個登入密碼工作，沒有等待佇列；取消 HTTP 請求後，名額仍保留至實際密碼運算結束。工作數由 `DAILY_INSIGHTS_LOGIN_PASSWORD_WORKERS` 控制，超額回覆 429。

### 聊天用量限制

聊天在資料庫交易內跨 worker 檢查使用者與組織額度，預設每位使用者最多 2 個進行中回覆、每組織 8 個；滾動 24 小時最多分別 100／1000 次生成。失敗與取消仍計入每日額度，完成請求的冪等重播不重複計費或扣額度。設定分別為 `DAILY_INSIGHTS_CHAT_USER_MAX_PENDING`、`DAILY_INSIGHTS_CHAT_ORG_MAX_PENDING`、`DAILY_INSIGHTS_CHAT_USER_DAILY_TURNS`、`DAILY_INSIGHTS_CHAT_ORG_DAILY_TURNS`；超額回覆 429。每次模型輸出預設最多 4096 tokens，可用 `DAILY_INSIGHTS_CHAT_MAX_OUTPUT_TOKENS` 調整。這些是請求與輸出用量上限，並非依供應商價格計算的金額預算。

回覆的總生命週期（包含歷史讀取、模型連線與串流）受聊天 timeout 限制。worker 意外終止所留下的 pending 紀錄，會在下一次額度檢查時回收；回收門檻為允許的最大 timeout 600 秒加 30 秒緩衝，不會重置每日用量。上述設定需由部署環境實際注入 API 程序，未注入時使用安全預設值。

### Podcast 永久移除 API

`DELETE /api/admin/podcasts/{episode_id}` 接受 JSON `{"expected_version": 3}`；需登入的 `admin` 或 `asset_manager` 與有效 `X-CSRF-Token`。僅 `draft` 可移除，包含從未發布的節目。成功且檔案／DB 清理全部完成回傳無 body 的 `204`；版本衝突回傳 `409 episode_version_conflict`，已發布回傳 `409 episode_must_be_unpublished`。

移除開始會遞增版本並凍結節目。儲存或 DB 清理失敗回傳 `503 episode_removal_incomplete`（含 `episode_id`）。`GET /api/admin/podcasts` 的 `deletion` 欄位提供 `status: pending`、`total_objects`、`cleared_objects`、`retained_objects`，卡片繼續顯示。重新載入最新版本後以相同 DELETE 手動重試；凍結期間其他修改回傳 `409 episode_removal_pending`。已刪物件的 missing／NoSuchKey 視為成功，直到所有物件處理完成才刪除節目資料。共用 Asset 與未登記匯入來源保留；稽核與移除進度記錄不隨節目刪除。

上傳 ticket 綁定交易日 generation（舊 ticket 預設 `0`），移除開始遞增 generation；completion 或 legacy worker 的舊 generation 回傳／記錄衝突，防止舊請求在同日重建後重播。完成移除後才接受同日期全新簽署上傳。完成 DELETE 的重播可使用原請求版本或凍結版本，已完成 job 回傳 `204`，不影響同日新節目。

移除完成前會在排序後的物件鎖內重新檢查共用引用；不同日期同時移除共用 Asset 時，由最後一個引用的移除者先清除物件及 Asset。最後階段的儲存／DB 失敗同樣保留 pending 狀態，使用最新版本重試。

`migrate_podcast_assets` CLI 的 copy／verify／cutover 遵守相同日期凍結與物件鎖。已登記 manifest 綁定首次保存的 `podcast_generation`（舊紀錄預設 `0`），移除或同日重建後拒絕舊 manifest 的 `migration_generation_conflict`，不能靠重新保存 rebinding。多日期 inventory 以日期及 asset ID 排序取鎖；copy／verify 持鎖至原有 DB 交易完成，同日期管理操作可能等待匯入。失敗回滾 DB，保留來源與未登記的已複製物件供既有 manifest 復原／人工處理。

CLI 重跑 inventory 時，會在任何 copy／輸出寫入之前核對既有 manifest 的 cutover 狀態、entry identity 與 generation；已完成或過期的 inventory 被拒絕時不會重建已刪物件或改動新錄音。全新 inventory 與同一 generation 的 verified 重試仍可執行。

複製前檢查也跨 manifest 核對每個 target 與 Asset ID 的永久歸屬；subset、重組或混入新項目不能繞過既有紀錄。其他 manifest 已持有的項目回傳 `migration_entry_owned_by_another_manifest`，整份 inventory 在任何複製前被拒絕，同一 manifest 的合法 verified 重試仍保留。
