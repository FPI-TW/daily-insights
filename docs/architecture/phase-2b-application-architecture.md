# Phase 2B 完整應用架構

狀態：本階段的程式碼與本機驗收已完成。Phase 3 Podcast 先行版已在此基礎上
開始實作。

## 已完成範圍

- API 模組以公開 `api.py` 契約隔離 persistence implementation，架構測試會
  拒絕未列入 composition root 的跨模組 models/service/router/schema import
  與跨表寫入入口。
- migration `20260724_0004` 建立 Podcast episode、三語 metadata、版本化 audio
  variant、asset migration manifest/entry 與全域 active model singleton。
- 對話、訊息、generation 與 model configuration 的保留紀錄具有
  `RESTRICT` foreign key 與 PostgreSQL mutation trigger；active model 只由
  singleton pointer 決定。
- R2 透過應用程式擁有的 `ObjectStore` protocol 與 boto3 S3-compatible
  adapter 存取。同步 SDK 呼叫與串流讀取由 `asyncio.to_thread` 隔離，不阻塞
  async event loop。
- Podcast 搬移指令支援 inventory、dry-run、可重跑 copy、size/MIME/SHA-256
  reconciliation、明確 cutover 與舊 source removal manifest。target 使用
  conditional create，既有 object 不會被覆寫；protocol 沒有 delete
  operation，工具不會移除來源物件。
- signed URL 契約只接受 active、metadata 完整且目前 R2 bytes checksum 相符的
  asset。HEAD 沒有可信 SHA-256 時會串流重算，不使用 ETag 當內容 checksum。
- staging/production 啟動時必須具備有效的 database、session、password
  pepper、FinDB 與 R2 設定；placeholder、非 HTTPS endpoint 與不合法 bucket
  會 fail closed。
- Web 與 API 使用各自的服務設定檔，不共享 application env。根目錄 `.env`
  只負責本機 Compose/PostgreSQL wiring；provider、model 與 R2 credential
  只會注入 API。
- Web 以 URL locale 作為 `zh-hant`、`zh-hans`、`en` 的 source of truth，完成
  登入、首次改密碼、登出、客戶／後台／admin route boundary，以及共用
  loading、error、403、404 狀態。
- browser 只以 same-origin `/api` 使用 HttpOnly session cookie；server
  transport 只轉送核准的 Cookie、request ID 與必要 content/CSRF headers。
  CSRF token 只保留於記憶體，重新整理後透過同源受保護 endpoint 輪替。
- OpenAPI 與 TypeScript client 可重現產生，CI 會檢查 drift；API response
  另以 Zod 在執行期 trust boundary 驗證。
- nginx 已區分一般 API、signed URL issuance 與未來 SSE transport。R2 audio
  bytes 不經 nginx/API，credential 不進入 browser bundle。
- operations 擁有 component readiness，分別呈現 database、FinDB
  configuration 與 R2 runtime initialization。FinDB/R2 的外部連線可用性由
  模組事件與告警觀察，不作為整個應用程式的 traffic-removal gate，避免短暫
  上游故障讓 last-known-good 報告與既有內容一起下線。結構化 HTTP/module
  event 會遮罩 credential、token、password 等敏感欄位。

本階段的原始驗收不包含 Podcast 列表、詳情、播放器或內容管理 HTTP API；
這些功能現已由後續 Phase 3 變更實作。報告、聊天與一般資產管理介面仍未完成。

## 驗收證據

管理者在共享工作區執行：

```text
make format
make check
make test-db
```

結果：

- Prettier、Ruff format、ESLint、Ruff lint、TypeScript 與 strict mypy 通過；
- OpenAPI/client drift check 通過；
- Web 5 tests、API client 3 tests、API 134 tests 通過；
- TanStack Start production build、browser secret scan 與 API wheel/sdist build
  通過；
- nginx configuration/transport contract 通過；
- PostgreSQL migration upgrade、Alembic check、downgrade 至 Phase 1 後
  re-upgrade 與第二次 check 通過。

## 已知風險與後續工作

- CSRF token 是每個 session 的單一輪替值。另一分頁重新取得 token 後，舊
  分頁下一次 mutation 可能先收到 `403`；目前可重新整理恢復，Phase 3 可加入
  一次性的重新取得及 retry UX。
- R2 缺少可信 SHA-256 metadata 時需要讀取完整物件。搬移前應評估音檔總量、
  migration host 的下載／上傳頻寬、暫存與記憶體上限、執行時間及 thread-pool
  headroom；conditional PUT 會把 source bytes 串流經過 migration host，
  canonical copy 會保存明確 checksum。
- boto3 是同步 SDK，雖然目前以 thread boundary 包裝，Phase 4 負載驗證仍需
  觀察 thread pool、連線池與 timeout。
- route guard 已有型別、unit test 與 production build 證據，但尚未導入
  Playwright browser E2E；Phase 3 的 Podcast 垂直切片應補上實際 browser
  導航、鍵盤、播放器與 failure-state 測試。
- 本階段沒有操作正式 bucket。實際搬移仍需核准的 source key、trading date
  與 locale inventory，並依 Podcast runbook 執行 dry-run、verified cutover
  與人工清理。
