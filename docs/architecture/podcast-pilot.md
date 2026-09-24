# Podcast 先行版

狀態：本機功能已完成第一輪實作、整合測試及 mock-based browser E2E；隔離的
live R2 adapter QA 亦已完成。實際 browser 對 R2 播放、live endpoint 流程與
Phase 4 上線驗收仍待執行。八大市場正式內容與報告前端在此期間維持 pending。

## 實作狀態

目前已完成：

- 上傳時以 mutagen 讀取音檔長度寫入 `podcast_episode_audio_variants.duration_seconds`，客戶端清單以「約 N 分鐘」顯示；從 R2 登記的音檔不讀取內容，長度為 null。
- `admin` 與 `asset_manager` 可從後台一次上傳 1–3 個語系音檔，成功上傳後
  episode 預設發布，兩種身份皆可發布及下架；
- 同交易日唯一性、任一語系 active audio 發布驗證，以及缺漏語系提示；
- 同 locale 替換警告、明確確認、expected current version、stable canonical
  key 的同格式原地覆寫／跨格式 key 切換，以及邏輯版本遞增；
- 客戶共用 catalog、detail、requested locale 優先且其後依
  `zh-hant` → `zh-hans` → `en` fallback，以及短效 signed URL；
- TanStack Start 三語客戶頁、responsive Podcast 清單內的原生 audio element、
  loading/empty/failure state 與依 user/episode/resolved locale 隔離的
  `localStorage` 進度；
- 獨立的客戶與管理端登入入口、route guard、導覽與登出導向；
- `/admin/audio` 音檔管理頁、三個可點擊／拖放的語系 slot、R2 upload 及發布
  控制；
- browser audio upload now uses idempotent batch init, immutable UUID object keys,
  direct signed R2 PUTs, locale-level finalize/status, and a durable media worker
  that checks size/MIME, computes SHA-256, extracts media metadata, and performs
  fenced database cutover;
- PostgreSQL + fake R2 端到端測試，覆蓋建立、發布拒絕、音檔登記、角色限制、
  locale fallback、同路徑覆寫及下架；
- Playwright + deterministic mock API browser E2E 共 15 個 specs，覆蓋兩個
  login 入口、錯角色 session 清除、跨 surface guard、各自 logout、後台 upload
  slot、replacement／unpublish confirmation、客戶清單內播放器、locale
  fallback、lazy signed URL、單集音檔重試、mounted session 過期導向、
  loading/empty/error、keyboard 與 390px viewport；
- 在 `2099-12-31` 隔離 prefix 完成 live R2 adapter QA：初次 upload、same-key
  overwrite、SHA/checksum metadata、signed full GET、MP3 → MP4 key switch 與
  old-key deletion，並已清理該次測試 objects。

尚待正式環境或 browser 驗收：

- 以實際 browser 透過 R2 signed URL 播放，驗證 CORS、byte range／seek 與
  browser audio 行為；
- signed URL 到期後的拒絕行為，以及 R2／network failure 的 browser UX；
- live API endpoint 的登入、CSRF、multipart upload、發布及播放授權完整流程；
- 正式容量、併發、失敗注入及網路條件驗收。

## 目標

Podcast 先行版用來驗證一條可上線的完整路徑：

```text
內部內容管理
  -> PostgreSQL episode/asset metadata
  -> private Cloudflare R2 audio
  -> API authorization and signed URL
  -> TanStack Start Podcast list with inline player
```

這個切片必須沿用正式的 identity、RBAC、audit、i18n、R2、API client、nginx
與部署邊界，不建立 Podcast 專用微服務，也不把 R2 bucket 設為公開。

## 架構邊界

- `podcasts` 擁有 episode identity、發布狀態、排序、由 canonical 路徑推導的
  顯示資訊，以及對 audio/cover asset 的 reference。
- `assets` 擁有 R2 object key、MIME type、size、checksum、lifecycle 與
  signed URL；不擁有 Podcast 標題、摘要或發布規則。
- `admin` 組合 episode 與 asset 的 privileged workflow，並寫入 audit。
- 客戶 web 只取得可發布的 episode metadata；播放前再向 API 要求短效、
  object-scoped URL。
- 播放 audio bytes 由瀏覽器直接向 R2 取得；browser upload 也由瀏覽器直接 PUT
  到 private R2。API 驗證身份、確認替換版本並簽發短效 create-only URL，worker
  驗證完整物件後才啟用資料庫 asset/variant。既有 multipart upload 與 import API
  保留給內部工具。
- Podcast catalog 由所有具有效 membership 的 org 共用，不套用八市場
  visibility，也沒有 customer-specific episode policy。`admin` 與
  `asset_manager` 以前台虛擬 `admin` 組織存取同一份 catalog；此 scope
  不建立 Organization 或 Membership、不占用席位，也不改變兩種內部角色既有的
  後台 RBAC。
- `trading_date` 是 episode 的唯一業務鍵，由後台指定，不從上傳時間、檔名或
  R2 metadata 推導；列表依交易日由新到舊排序。
- 使用原生 HTML `<audio>` element；產品不提供下載按鈕或離線下載功能。
  短效 signed GET URL 能降低長期分享風險，但無法技術上保證使用者不能擷取其
  已被授權播放的 bytes。

## R2 與語系音檔

語系同時存在 canonical R2 path 與 episode/asset 關聯；資料庫關聯仍是授權及
版本切換的 source of truth，讀取端可由固定 path 格式驗證語系。關聯為：

```text
podcast_episode_audio_variants
  episode_id
  locale       zh-hant | zh-hans | en
  asset_id
  UNIQUE (episode_id, locale)
```

- 已發布 episode 只需至少一個有效語系音檔。
- API 先找頁面語系完全相符的 audio variant；找不到時依
  `zh-hant` → `zh-hans` → `en` 選擇第一個可用檔案，並在 response 明確回傳
  requested/resolved locale。
- 既有 R2 objects 必須在 inventory 時逐一人工審核並指定 locale，不得從 legacy
  key 推斷語系；object 會先複製到新的 migration target key，並以該 verified
  locale 登記 variant。只有完成逐檔驗證及整批 migration reconciliation 後才
  切換 active mapping。
- 後台提供固定對應 `zh-hant`、`zh-hans`、`en` 的三個 slot，每個皆可點擊或
  拖放。一次請求至少一檔、最多三檔，不要求固定必備語系。
- 上傳原因使用固定選單：`initial_upload`（初次上傳）、`update_file`
  （更新檔案）、`other`（其他）。
- browser direct upload 的 object 使用
  `podcasts/{trading-date}/audio/{locale}/{asset-uuid}.{ext}`，其中 `{ext}` 僅支援
  `mp3` 與 `mp4`。每一 locale 都寫入新的不可變 key；替換成功後以 PostgreSQL
  variant mapping 原子切換，不覆寫舊 bytes。每個 init file 必須提供 64 位小寫
  SHA-256；API 將其與 locale、大小、MIME、replacement expected version 綁定到
  upload session。R2 PUT 簽名包含 `Content-Type`、`If-None-Match: *` 與
  `x-amz-meta-sha256`，讓 R2 在 HEAD metadata 中保留該 checksum。
- finalize 與 worker 都會確認 R2 HEAD 的 `sha256` metadata 等於 session 預期值；
  worker 仍會把 object 串流到 bounded spool，獨立計算 bytes 的 SHA-256，並要求
  同時符合 session 預期值與前後 HEAD metadata，才會啟用 Asset。播放簽 URL 可透過
  R2 HEAD metadata 比對 Asset checksum，不必為新上傳檔案重新下載整段音訊。
- R2 暫時性網路或服務錯誤會將該 locale 保留為 `processing`，以資料庫 lease
  延後重試；重試等待從 15 秒起逐次增加，最多執行五次，耗盡後標記 `failed`。
  延後中的 locale 不阻塞同批其他 queued locale；未預期的程式錯誤仍會向上拋出。
- batch 有一至三個獨立 locale session。任一 locale 驗證與 cutover 成功就會立即
  啟用；同批其他 locale 可繼續處理。狀態以 locale 回報，失敗不回滾已完成語系。
  批次從 draft 或不存在 episode 開始時，首個成功 locale 會發布 episode。之後若
  有管理者下架或修改 episode，episode version fence 會讓剩餘 session 進入 conflict，
  worker 不會重新發布 episode。
- init 以 PostgreSQL transaction advisory lock 序列化相同交易日；同日已有
  `pending_upload`、`queued` 或 `processing` session 時，另一批次回傳
  `409 upload_in_progress`，包括不同語系，以維持 episode version fence。相同批次仍可
  一次初始化多個語系。同一 idempotency key 會在取得日期鎖後重新讀取並回傳原 batch。
  過期的 `pending_upload` 會先轉成 `expired`，不再阻擋新批次；`queued` 與 `processing`
  即使 presign 時間已過仍會阻擋，直到處理完成或進入 terminal 狀態。
- unfinalized object 只在簽名 PUT 到期並超過設定 grace period 後開始清理。過期
  session/key 會保留清理墓碑並再次檢查，處理 URL 到期前已開始、之後才完成的 PUT；
  版本 fence 產生的 conflict session 也會在 grace period 後清理 orphan object。
  R2 清理錯誤會透過 cleanup lease 延後五分鐘重試；object key 不會重用，也不會
  刪除已有 Asset row 的 key。
- object key 與檔名只由後端產生，來源檔名不進入 R2 key。上傳時不輸入標題或
  摘要；後台 `admin` 可輸入三語標題與摘要（`metadata_source = manual`）；尚未
  輸入時顯示由固定檔名 `podcast` 與 trading date 推導的
  `Podcast | YYYY-MM-DD`（`derived`）。
- 既有 object 透過受控 migration/import workflow 處理：內部人員提供來源
  key、`trading_date` 與 locale，後端不信任也不解析舊檔名，並將 object
  複製到不可變 target key
  `podcasts/{trading-date}/audio/{locale}/podcast-{trading-date}-{locale}-{asset-id}.{ext}`。
  migration 使用含 asset ID 的 key 與 conditional create，不與 browser upload
  共用 stable key generator，也不覆寫已存在的 target。
- 使用者切換頁面 locale 後，播放器重新解析該 locale 的音檔；若發生 fallback，
  UI 仍維持使用者選擇的頁面語系。

## 先行版介面

客戶端：

- Podcast 列表與清單內播放器；既有 detail URL 安全轉回清單；
- cover、標題、摘要與 `trading_date`；不建立 show/series、season、episode
  number 或 scheduled publication；
- 使用者明確選擇收聽後才請求短效 signed URL；單集載入或媒體失敗可獨立重試；
- 原生 HTML `<audio>` 的播放、暫停、seek、載入與錯誤狀態；
- 保存與恢復每位使用者的播放進度；
- 標題、摘要與章節依人工輸入或音檔標記顯示；章節區在
  沒有章節時隱藏。

內部端：

- `admin` 與 `asset_manager` 可透過三語 slot 建立 episode、上傳或替換 audio，
  成功上傳後 episode 預設發布，且兩種身份皆可發布及下架；
- 檢查至少一個 audio variant、asset 狀態與 MIME type 後才允許發布；
- 每個有效音檔可編輯章節（「分:秒 標題」每行一段），`admin` 可編輯三語
  標題與摘要；
- privileged mutation audit。

下列功能不納入先行版：RSS feed、公開匿名播放、離線下載、scheduled
publication、show/series/season、episode number、收聽分析、留言、訂閱通知、
逐字稿與自動摘要。

## 重複交易日與替換

- `podcast_episodes.trading_date` 必須唯一，一個交易日只有一個 logical episode。
- 同一 episode 新增尚不存在的 locale audio variant 是正常增補，不顯示覆蓋
  警告。
- 同一 `trading_date + locale` 已有 active audio 時，登記或上傳新檔第一次
  必須回傳 replacement-required 警告，不得直接改變 active mapping。
- 管理者明確確認後，direct upload 會使用新的 UUID R2 key，不覆寫原 object；
  worker 取得 episode 與 variant 鎖後確認 batch base episode version、已套用 locale
  數與 expected current locale version，再以單一 DB transaction 建立 active Asset、
  切換 variant、遞增 episode version、套用首個 locale 的自動發布並寫 audit。互不相關
  的 episode 編輯、下架或另一批次先完成 cutover 都會 fence 剩餘 locales。
- 舊的 multipart endpoint 仍保留原有 stable-key 行為；新 browser workflow 使用
  immutable key，舊 bytes 由既有資產保留政策管理。

## 既有 R2 搬移流程

搬移必須是可重跑的 copy/verify/cutover 流程，並產生 migration manifest，至少
記錄 source key、target key、trading date、locale、size、MIME type、SHA-256、
copy/verify 狀態與時間：

1. inventory 舊 objects，逐一人工審核並補上 trading date 與 locale；locale
   必須是明確確認的 `zh-hant`、`zh-hans` 或 `en`，不得由 legacy source key
   推斷；
2. 對 source 執行 HEAD，拒絕不存在、零 bytes 或不允許的 MIME type；
3. 由後端產生 canonical target key，使用 `If-None-Match: *` conditional
   object create 原子建立 target；因 R2/S3 `CopyObject` 沒有 destination
   precondition，工具會以受控串流讀取 source 並寫入 target，避免
   HEAD-then-copy 競態覆寫已存在的不同內容；
4. 對 source 與 target 驗證 size、MIME type 與 SHA-256。ETag 不得單獨視為
   checksum，因 multipart object 的 ETag 不保證等於內容 MD5；
5. 只有 verified object 才建立 active asset/audio-variant candidate；
6. 全部 manifest entries verified、episode/locale 對應完整且 reconciliation
   無缺漏後，由 `admin` 明確確認 cutover；
7. cutover 後輸出舊 source keys removal manifest。應用程式與 migration tool
   不自動刪除舊 object；內部人員確認新路徑可播放及數量/checksum 相符後，才
   手動移除舊路徑檔案。

copy 與驗證失敗不得改變 active mapping。舊路徑在人工清理前是 rollback
來源，但不向客戶簽發。人工清理結果應回填 manifest，方便確認沒有漏刪或誤刪
不相關 objects。

## 播放進度

播放進度只存在瀏覽器 `localStorage`，不寫入 API 或 PostgreSQL，也不跨裝置
同步。建議 versioned key：

```text
daily-insights:podcast-progress:v1:{user-id}:{episode-id}:{resolved-audio-locale}
```

value 至少包含 `positionSeconds`、`durationSeconds` 與 `updatedAt`，讀取時以 Zod
驗證並將 position clamp 在目前音檔 duration 內。播放器以節流方式在
`timeupdate` 保存，並在 pause、ended 與頁面離開前補寫；storage quota、private
mode 或損壞資料不得阻止播放。

key 使用 resolved audio locale：例如英文頁面 fallback 至 `zh-hant` 時，讀寫
`zh-hant` 進度；日後補上英文音檔後，英文 variant 使用自己的獨立進度。

## 驗收重點

- 未登入、停權或不具 membership 的 `org_member` 無法取得客戶 Podcast
  catalog 或 signed URL；`admin` 與 `asset_manager` 則使用前台虛擬 `admin`
  組織 scope，不需要持久化 membership。
- draft/unpublished episode 永遠不出現在客戶 API。
- 特定日期的發布與下架不要求填寫理由；後台執行下架前必須顯示確認警告。
- audio asset 必須為 active、允許的 MIME type，且 checksum/size 與 metadata
  完整。
- 所有 org 看到同一份已發布 catalog；任何八市場 policy 調整都不改變
  Podcast 結果。
- 同一交易日不得建立第二個 logical episode；同 locale replacement 未經明確
  確認不得改變 active audio。
- replacement 必須明確確認並帶 expected current version；格式相同時覆寫同一
  stable canonical key，格式改變時先寫入新 stable-extension key，再刪除被
  取代的舊格式 key。
- requested locale variant 存在時必須播放相符檔案；不存在時依
  `zh-hant` → `zh-hans` → `en` 穩定回退。
- 既有 legacy 音檔的 locale 必須逐一人工審核，不得由 source key 推斷；
  copy/verify 後的 migration target 以該 verified locale 登記。cutover 前不得
  只因 source object 存在就標記 migration complete。
- signed URL 有短效期限、只對應單一 object，response 與 log 不含 R2
  credential。
- asset 被 quarantine、遺失或失效後，不再簽發新 URL；播放器呈現可理解的
  unavailable 狀態。
- 任一交易日已有音檔但語系不完整時，後台需在該交易日旁列出缺少語系。
- 只有一個任意語系音檔也可發布；完全沒有音檔時不可發布。
- 原生 audio element 的 current time 能保存並在重新進入清單後恢復。
- localStorage 依 user、episode 與 resolved locale 隔離；無效或超出 duration
  的資料會被忽略或修正，且不影響播放。
- 列表與播放器具備 responsive、keyboard、loading、empty 與 failure-state
  測試；舊 detail URL 具備 redirect regression。
- 從內部建立 episode 到客戶播放的整合測試使用隔離 PostgreSQL 與 fake R2
  signer，不依賴正式 bucket。

## 決策狀態

Podcast 先行版目前沒有會阻擋 domain schema 的產品問題。通用 asset upload、
掃毒、刪除／復原與版本保留期限仍屬 Phase 6A；Podcast 搬移僅授權 verified
copy、cutover 與人工清理舊路徑，不等同完成通用 asset deletion。
