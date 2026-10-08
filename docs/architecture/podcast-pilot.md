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
- 同 locale 替換警告、明確確認、expected current version，以及不可變
  object key 的 variant mapping 切換與版本遞增；
- 客戶共用 catalog、detail、requested locale 優先且其後依
  `zh-hant` → `zh-hans` → `en` fallback，以及短效 signed URL；
- TanStack Start 三語客戶頁、responsive Podcast 清單內的原生 audio element、
  loading/empty/failure state 與依 user/episode/resolved locale 隔離的
  `localStorage` 進度；
- 獨立的客戶與管理端登入入口、route guard、導覽與登出導向；
- `/admin/audio` 音檔管理頁、三個可點擊／拖放的語系 slot、R2 upload 及發布
  控制；
- browser 上傳使用無狀態簽署、不可變 UUID object key、直接 R2 PUT，以及
  API 同步驗證與資料庫登記；不再依賴音檔處理 worker 或批次輪詢；
- PostgreSQL + fake R2 端到端測試，覆蓋建立、發布拒絕、音檔登記、角色限制、
  locale fallback、同路徑覆寫及下架；
- Playwright + deterministic mock API browser E2E 共 23 個 specs，覆蓋兩個
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

## 2026-10-01 同步直傳流程

本分支已實作，尚未部署正式環境。日常音檔小於 20 MB，但保留既有每檔
256 MiB、MP3／MP4，以及每次 1–3 個語系檔案的硬性限制。

1. Frontend → Server：`POST /api/admin/podcasts/direct-uploads` 取得 signed
   PUT URL 與 HMAC 簽章憑證。驗證身份、角色、CSRF、檔案資訊與替換版本；
   憑證綁定使用者、asset ID、交易日、語系、大小、MIME、SHA、版本與期限。
   此步驟只讀 DB，不建立 batch／session，也不占用交易日。
2. Frontend → R2 → Frontend：以簽署要求的 headers 直接 PUT 音檔。
3. Frontend → Server → R2 → DB：
   `POST /api/admin/podcasts/direct-uploads/complete` 提交憑證，Server 同步
   檢查 HEAD、完整 SHA-256、時長與章節，以 transaction 寫入 Asset、variant
   與 audit，成功才回傳完成。日期鎖只用於資料切換，同一 asset ID 的提交
   與清理使用共同的 PostgreSQL advisory lock。

各語系獨立提交；部分失敗不回滾已完成語系。後端確認完成後，Frontend
清除該語系的選檔，保留完成進度與 checksum；正常完成及重試憑證取得成功結果
都使用相同行為。之後新增另一語系時，只提交仍選取的檔案，避免重送已完成檔案
或再次要求替換確認。失敗／取消語系的選檔保留供重試。

Frontend 顯示 PUT 進度及驗證等待，失敗後可重試或重新上傳。重試優先提交原憑證，避免回應遺失後重傳；
憑證已成功登記時，即使過期仍回傳原結果。重新整理後需重新選檔，但可立即
開始新上傳，不會建立原先的 `upload_in_progress` 占用。

確定驗證失敗、版本衝突或 DB 回滾且物件未被引用時，執行補償刪除。
提交結果不明時，在新交易重新查證；DB 不可用或 R2 暫時失敗時保留物件，
允許重試。API lifespan 每五分鐘掃描新 prefix，僅在憑證到期與最後修改時間
均超過 cleanup grace（預設 24 小時）後，刪除未被任何 Asset 引用的物件。
archived 版本同樣受保護；刪除失敗及晚到物件會在後續掃描重試。

正式 API 使用專用磁碟 volume `/var/spool/podcast-media`，不受 `/tmp` 的
64 MiB tmpfs 限制。同步內容驗證最長九分鐘，nginx complete endpoint 最長
等待十分鐘；其他一般 API 保留原本設定。實際 R2 網路耗時仍須正式驗收。

程式碼：`synchronous_upload.py`、`upload_cleanup.py` 與
`AudioManagementPage.tsx`。正式 Compose、部署腳本與 release workflow 已
移除 media worker 與專屬 credentials；舊 worker 的一次性清退、舊 session
盤點及 DB 處理依 [人工切換流程](../runbooks/podcast-upload-cutover.md)，不放入
CI/CD。舊 batch init／finalize 在 production 回傳 `410 legacy_upload_retired`；
歷史狀態讀取及本機 `legacy-podcast` profile 留供舊流程驗證。

### 同步直傳本機驗證（2026-10-01）

- `pnpm check` 通過：945 項 API（含隔離 PostgreSQL 整合）、235 項 Web、
  31 項 API client，以及格式、lint、型別、OpenAPI 產物一致性與建置。
- Podcast Playwright 23 項通過，涵蓋簽署、直接 PUT、同步登記與替換確認。
- 部署合約、nginx syntax 與 Docker DNS upstream 替換測試通過。
- 失敗注入涵蓋 checksum、完整交易回滾、提交回應遺失、DB 結果不明、
  R2 讀取／刪除失敗，以及 orphan sweep 與 complete 競態。
- 此次使用 fake R2／mock browser；未部署正式環境，live R2 權限、網路耗時
  與 Cloudflare 代理期限需依正式 runbook 驗收。

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
  到 private R2。API 驗證身份、確認替換版本並簽發短效 create-only URL，再於 complete
  同步驗證完整物件後啟用資料庫 asset/variant。既有 multipart upload 與 import API
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
  UNIQUE (episode_id, locale) WHERE is_active
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
- browser direct upload 使用
  `podcasts/direct/{expires}/{trading-date}/{locale}/{asset-uuid}.{ext}`，僅支援
  `mp3`／`mp4`；來源檔名不進入 key。每次簽署建立新 key，不覆寫舊音檔。
- 每檔提供 64 位小寫 SHA-256，與檔案資訊綁定簽章憑證。R2 PUT 簽署
  `Content-Type`、`If-None-Match: *`、`x-amz-meta-sha256`；API 比對 HEAD
  與串流 bytes 的 checksum，前後 HEAD 必須一致，才啟用 Asset。
- R2 暫時失敗回傳可重試錯誤，不建立 queue 或 processing lease。多語系各自
  完成，資料切換時以 locale 的 expected current version 防止競態覆蓋。
- 初次成功音檔沿用自動發布；若管理員在簽署後下架或編輯 episode，
  後續登記不會重新自動發布。不同語系可獨立完成。
- 清理遵守上節的期限與引用檢查，不刪除已登記的 active／archived 版本。
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
  complete 取得 episode 與 variant 鎖後確認 expected current locale version，
  以單一 DB transaction 切換版本、遞增 episode version 並寫 audit。
  同語系競態的後完成者回傳衝突；不同語系可各自完成。介入的下架／編輯
  會阻止自動發布，不影響其他語系檔案的獨立登記。
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
- browser replacement 必須明確確認並帶 expected current version，以新 UUID
  key 及原子 variant mapping 切換；已登記舊版本保留，不由孤兒清理刪除。
  內部 multipart 工具保留原有 stable-key 覆寫與跨格式 key 切換行為。
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

## 未發布節目的永久移除

`admin` 與 `asset_manager` 可移除整集 `draft` Podcast，包含從未發布的節目。已發布節目的移除按鈕停用並提示先下架；確認視窗列出日期、所有語言、歷史音檔與不可復原性。移除包含已登記的封面及 active／archived 音檔，保留其他節目共用的 Asset／檔案與未登記的匯入來源。

移除會先提交 `podcast_deletion_jobs`／`podcast_deletion_objects` 清單、凍結節目並遞增版本，再逐檔刪除及提交進度；檔案不存在視為成功。所有檔案處理完畢才在同一交易刪除節目、翻譯、variants 與未被引用的 Assets，並記錄完成稽核。稽核、移除 job／object tombstones、upload batches／sessions 與匯入操作紀錄保留，支援回復與重播辨識。

儲存或 DB 錯誤回傳 `503 episode_removal_incomplete`，凍結卡片及進度保留，使用最新版本手動重試同一端點；已完成的檔案不重複處理，外部刪除後 DB 回滾則可安全重做冪等刪除。完成交易回應遺失時，可用原請求版本或凍結版本重播，已完成 tombstone 回傳 `204`。

`podcast_date_generations` 永久保存每個交易日的 upload generation，移除開始即遞增。新簽署 ticket 綁定 generation，舊格式預設 `0`；completion（含已完成重播）及 legacy worker／batch 都檢查 generation，因此舊 ticket 在節目移除或同日重建後不能恢復檔案。移除期間禁止發布、metadata／chapters 編輯、上傳與匯入；完成後才允許同日期新上傳。鎖順序統一為日期、節目／batch、物件 advisory lock、Asset row；orphan cleanup 只取物件鎖，不反向等待日期。

完成交易會以 asset ID 排序取得所有物件鎖，重新檢查先前判定共用的物件。不同日期的節目同時移除同一 Asset 時，最後一個引用的移除者必須先清除物件，才提交節目／Asset 清理及完成狀態；最後階段儲存或 DB 失敗仍可重試，不會永久豁免曾經共用的檔案。

支援的 `migrate_podcast_assets` CLI 也遵守日期凍結與物件鎖，並在 `asset_migration_entries.podcast_generation` 保存首次登記時的 generation（既有紀錄預設 `0`）。舊 verified／cutover manifest 在移除或同日重建後會因 `migration_generation_conflict` 被拒絕，不能重新綁定新 generation。多日期 inventory 按日期、asset ID 排序取鎖；copy／verify 在原有 DB 交易內持鎖，避免移除期間重新建立已刪檔案，因此同日期管理操作可能等待匯入完成。失敗時 DB 交易回滾，來源及已複製但尚未登記的物件仍依既有 manifest 流程保留供人工處理。

CLI 重跑 inventory 時，會在任何 copy／輸出寫入之前核對既有 manifest 的 cutover 狀態、entry identity 與 generation；已完成或過期的 inventory 被拒絕時不會重建已刪物件或改動新錄音。全新 inventory 與同一 generation 的 verified 重試仍可執行。

複製前檢查也跨 manifest 核對每個 target 與 Asset ID 的永久歸屬；subset、重組或混入新項目不能繞過既有紀錄。其他 manifest 已持有的項目回傳 `migration_entry_owned_by_another_manifest`，整份 inventory 在任何複製前被拒絕，同一 manifest 的合法 verified 重試仍保留。
