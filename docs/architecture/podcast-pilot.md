# Podcast 先行版

狀態：已確認為完整應用架構完成後的第一個客戶端垂直切片。八大市場正式內容與
報告前端在此期間維持 pending。

## 目標

Podcast 先行版用來驗證一條可上線的完整路徑：

```text
內部內容管理
  -> PostgreSQL episode/asset metadata
  -> private Cloudflare R2 audio
  -> API authorization and signed URL
  -> TanStack Start episode list/detail/player
```

這個切片必須沿用正式的 identity、RBAC、audit、i18n、R2、API client、nginx
與部署邊界，不建立 Podcast 專用微服務，也不把 R2 bucket 設為公開。

## 架構邊界

- `podcasts` 擁有 episode identity、發布狀態、排序、三語 metadata 與對
  audio/cover asset 的 reference。
- `assets` 擁有 R2 object key、MIME type、size、checksum、lifecycle 與
  signed URL；不擁有 Podcast 標題、摘要或發布規則。
- `admin` 組合 episode 與 asset 的 privileged workflow，並寫入 audit。
- 客戶 web 只取得可發布的 episode metadata；播放前再向 API 要求短效、
  object-scoped URL。
- audio bytes 由瀏覽器直接向 R2 取得，不流經 nginx 或 API。
- Podcast catalog 由所有具有效 membership 的 org 共用，不套用八市場
  visibility，也沒有 customer-specific episode policy。
- `trading_date` 是 episode 的唯一業務鍵，由後台指定，不從上傳時間、檔名或
  R2 metadata 推導；列表依交易日由新到舊排序。
- 使用原生 HTML `<audio>` element；產品不提供下載按鈕或離線下載功能。
  短效 signed GET URL 能降低長期分享風險，但無法技術上保證使用者不能擷取其
  已被授權播放的 bytes。

## R2 與語系音檔

語系屬於 episode 與 asset 的關聯，不從 R2 object key 推導，也不應只依賴
`assets.locale`。預定關聯為：

```text
podcast_episode_audio_variants
  episode_id
  locale       zh-TW | zh-CN | en
  asset_id
  UNIQUE (episode_id, locale)
```

- `zh-TW` 是每個已發布 episode 必備的預設音檔。
- 頁面為 `zh-CN` 或 `en` 時，API 先找完全相符的 audio variant；找不到就
  回退至 `zh-TW`，並在 response 明確回傳 requested/resolved locale。
- 目前只有繁體中文音檔；既有 R2 objects 會先複製到新的 canonical key 並
  登記為 `zh-TW` variant。只有完成逐檔驗證及整批 migration reconciliation
  後才切換 active mapping。
- 初版不做 browser/back-office upload。內部人員先手動上傳至 R2，再由
  back office 或受控管理指令登記 object key；API 必須以 R2 HEAD 驗證 object、
  MIME type 與 size 後才建立或啟用 asset metadata。
- 新 object 建議使用
  `podcasts/{trading-date}/audio/{locale}/podcast-{trading-date}-{locale}-{asset-id}.{ext}`。
  object key 與檔名只能由後端產生；`asset-id` 防止 R2 原地覆寫、舊 signed URL
  或 CDN cache 靜默指向不同 bytes。資料庫 mapping 才是語系的 source of
  truth，既有無語系結構的 key 仍完全支援。
- 初期手動上傳與既有 object 透過受控 migration/import workflow 處理：內部
  人員提供來源 key、`trading_date` 與 locale，後端不信任也不解析舊檔名，
  並將 object 複製到後端產生的 canonical key。正式 upload workflow 完成後
  也沿用相同 key generator。
- 使用者切換頁面 locale 後，播放器重新解析該 locale 的音檔；若發生 fallback，
  UI 仍維持使用者選擇的頁面語系。

## 先行版介面

客戶端：

- Podcast 列表；
- episode 詳細頁；
- cover、標題、摘要與 `trading_date`；不建立 show/series、season、episode
  number 或 scheduled publication；
- 原生 HTML `<audio>` 的播放、暫停、seek、載入與錯誤狀態；
- 保存與恢復每位使用者的播放進度；
- `zh-TW`、`zh-CN`、`en` 完整 metadata。

內部端：

- `admin` 建立與編輯 episode metadata、關聯 asset，以及執行 draft、publish、
  unpublish；
- `admin` 與 `asset_manager` 可登記或替換已手動放入 R2 的 audio/cover
  object；初版沒有上傳 API，且 `asset_manager` 不取得 episode 發布權限；
- 檢查三語 metadata、必備 `zh-TW` audio variant、asset 狀態與 MIME type
  後才允許發布；
- privileged mutation audit。

下列功能不納入先行版：RSS feed、公開匿名播放、離線下載、scheduled
publication、show/series/season、episode number、收聽分析、留言、訂閱通知、
逐字稿、章節與自動摘要。

## 重複交易日與替換

- `podcast_episodes.trading_date` 必須唯一，一個交易日只有一個 logical episode。
- 同一 episode 新增尚不存在的 locale audio variant 是正常增補，不顯示覆蓋
  警告。
- 同一 `trading_date + locale` 已有 active audio 時，登記或上傳新檔第一次
  必須回傳 replacement-required 警告，不得直接改變 active mapping。
- 管理者明確確認後才建立新 asset/version 並切換 active mapping；請求需帶
  expected current version，避免兩位管理者同時操作造成 lost update。
- 「覆蓋」是產品上的 logical replacement，不是以相同 R2 key 原地改寫 bytes。
  舊 object 與版本關聯保留、不可再簽發給客戶，且 mutation 必須寫入 audit。

## 既有 R2 搬移流程

搬移必須是可重跑的 copy/verify/cutover 流程，並產生 migration manifest，至少
記錄 source key、target key、trading date、locale、size、MIME type、SHA-256、
copy/verify 狀態與時間：

1. inventory 舊 objects，人工補上 trading date 與 locale；目前全部為
   `zh-TW`；
2. 對 source 執行 HEAD，拒絕不存在、零 bytes 或不允許的 MIME type；
3. 由後端產生 canonical target key，使用 R2 server-side copy，且不得覆寫
   已存在但 checksum 不同的 target；
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

key 使用 resolved audio locale：例如英文頁面 fallback 至 `zh-TW` 時，讀寫
`zh-TW` 進度；日後補上英文音檔後，英文 variant 使用自己的獨立進度。

## 驗收重點

- 未登入、停權或不具 membership 的請求無法取得客戶 Podcast catalog 或
  signed URL。
- draft/unpublished episode 永遠不出現在客戶 API。
- audio asset 必須為 active、允許的 MIME type，且 checksum/size 與 metadata
  完整。
- 所有 org 看到同一份已發布 catalog；任何八市場 policy 調整都不改變
  Podcast 結果。
- 同一交易日不得建立第二個 logical episode；同 locale replacement 未經明確
  確認不得改變 active audio。
- replacement 使用新的後端 canonical object key，舊 object 不被原地覆寫。
- `zh-CN`／`en` variant 存在時必須播放相符檔案；不存在時穩定回退
  `zh-TW`，且不得回退至任意其他語系。
- 既有無 locale object key 的音檔完成 copy/verify 後，canonical copy 登記為
  `zh-TW`；cutover 前不得只因 source object 存在就標記 migration complete。
- signed URL 有短效期限、只對應單一 object，response 與 log 不含 R2
  credential。
- asset 被 quarantine、遺失或失效後，不再簽發新 URL；播放器呈現可理解的
  unavailable 狀態。
- 三語欄位缺漏時不可發布。
- 沒有 `zh-TW` 音檔時不可發布；缺少其他語系音檔不阻擋發布。
- 原生 audio element 的 current time 能保存並在重新進入 episode 後恢復。
- localStorage 依 user、episode 與 resolved locale 隔離；無效或超出 duration
  的資料會被忽略或修正，且不影響播放。
- 列表、詳細頁與播放器具備 responsive、keyboard、loading、empty、404 與
  failure-state 測試。
- 從內部建立 episode 到客戶播放的整合測試使用隔離 PostgreSQL 與 fake R2
  signer，不依賴正式 bucket。

## 決策狀態

Podcast 先行版目前沒有會阻擋 domain schema 的產品問題。通用 asset upload、
掃毒、刪除／復原與版本保留期限仍屬 Phase 6A；Podcast 搬移僅授權 verified
copy、cutover 與人工清理舊路徑，不等同完成通用 asset deletion。
