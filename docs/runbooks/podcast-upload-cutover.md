# Podcast 同步直傳的一次性人工切換

此文件只供首次切換操作。停止舊 worker、盤點與處理舊 session 不寫入
GitHub Actions、deploy.sh 或 migration。一般部署只啟動新 Compose 的五個服務。
本分支尚未執行以下正式環境操作。

1. 暫停管理員上傳，備份資料庫並記錄舊容器 image digest。確認 API 的 R2 key
   具 bucket 範圍的 list、read、write、delete 權限。舊 worker key 在切換後不再
   是部署必要 Secret。
2. 以授權 DB 工具盤點狀態；只輸出 ID、語系與狀態，不列簽署 URL 或 credentials：

   ```sql
   SELECT b.id AS batch_id, b.trading_date, s.id AS session_id,
          s.locale, s.status, s.expires_at, s.lease_until
   FROM podcast_upload_batches b
   JOIN podcast_upload_sessions s ON s.batch_id = b.id
   WHERE s.status IN ('pending_upload', 'queued', 'processing')
   ORDER BY b.trading_date, s.locale;
   ```

3. 讓 queued／processing 音檔由既有 worker 完成，確認沒有進行中的內容處理後，
   在 EC2 手動停止並移除舊容器：

   ```sh
   docker stop --time 600 daily-insights-podcast-media-worker
   docker rm daily-insights-podcast-media-worker
   ```

   不刪 volume 或已登記的 R2 音檔。若程序卡住，先檢查 log 及 lease；只在人工
   確認放棄指定 session 且 worker 已停止後，於 transaction 將那些 ID 改為
   `failed`、`error_code = 'manual_cutover_abandoned'`，清空 processing lease，
   保留原本的 `cleanup_after` 與 object key。不要批次改寫 completed session，
   不要直接刪除整張 batch／session 表，也不要將此修復加入常態部署。

4. 留下 pending_upload 的 session 不阻擋新流程。舊 session 與無環境 scope 的
   key 保留供人工核對；API 與舊 worker 的背景迴圈均不再自動清理舊 session。
   不可只因本資料庫沒有 Asset 引用就刪除共用桶的物件。確認最後沒有
   queued／processing；記錄放棄或完成的 session ID，保留 audit。
5. 執行一般 release。新 API 使用 `podcast-upload-spool` 磁碟 volume 做同步
   驗證。舊 batch init／finalize 在 production 回傳 410，避免舊頁面建立無人
   處理的工作；管理員重新載入頁面後使用 direct-uploads。
6. 依 [正式上傳驗收](production.md#direct-upload-production-verification) 驗證
   簽署、R2 PUT、同步 complete、播放、替換及失敗重試。確認容器清單不再包含
   `daily-insights-podcast-media-worker`。通過後依 Secret 管理流程移除舊 worker
   專用 key；歷史 schema 與原始音檔保持可追溯，不在此切換刪除。

重新整理遺失選檔時可立即選檔重新開始；新簽署不建立 DB 占用。
v3 scope 內已 PUT 但未登記的暫存檔，在簽署期限及最後修改時間皆超過預設
24 小時 grace 後，由 API 每五分鐘掃描回收。正式檔與無 scope 的舊暫存檔不掃描。已成功登記的憑證可冪等重送，即使期限已過。

## v2 正式儲存路徑相容性

新簽署發出內部 v3 HMAC 憑證，browser PUT 使用
`podcasts/staging/{scope}/{expires}/{trading_date}/{locale}/{asset_id}.{mp3|mp4}` 暫存。
歷史 v1/v2 憑證仍沿用 `podcasts/direct/` 路徑並可完成；v2/v3 的正式路徑相同。
complete 先驗證 generation／目前版本、HEAD、完整 SHA、時長和章節，再由 spool
以 conditional create 建立 `podcasts/YYYY/MM/DD/{locale}/podcast_{version}.{mp3|mp4}`。
月份日期補零，version 與該語系 variant 一致；DB commit 後清暫存。

不用搬移既有物件、改 DB schema 或重簽未到期 v1 憑證；v1 仍登記原路徑。
驗收時確認舊播放、v1 complete、v2 替換及 response-lost retry 都正常。
日期鎖與物件鎖內只清理本 scope 超過 grace 的未引用暫存檔；正式孤兒、
archived 引用、舊路徑及不相關物件保留。碰撞不覆寫，只有未引用且 size／MIME／完整 SHA 一致才可重用。
DB 結果不明或儲存逾時保留物件供重試；不要人工刪除仍被 Asset 引用的版本。

## 2026-10-23 清理隔離修正部署

scope 由 session secret 對環境、資料庫連線身分（不含密碼）、R2 endpoint 與
bucket 計算 HMAC。各環境仍必須使用獨立桶及只授權該桶的 token；複製環境時須
先改環境／資料庫與權限。scope 不是儲存端權限控制，也無法區別完整複製的設定。

須更新所有可存取該桶的 API／舊 worker 並停止舊程式，才算完成防護部署；不得只
更新其中一個副本。新 v3 憑證無法交給舊 API 處理，回退前須處理進行中的上傳。
新程式接受既有 v1/v2 憑證，不需要 DB migration 或搬移音檔。

成功刪除記錄 `podcast.object.deleted`；背景清理另記錄
`podcast.staging_cleanup.deleted`，含 environment、bucket、object key。
旋轉 session secret 或變更資料庫身分後，舊 scope 保留人工核對；只旋轉 DB 密碼
不變更 scope。保留正式孤兒與舊暫存會增加儲存用量，人工清理前須跨所有擁有者的
資料庫核對明確 key 清單；不可恢復整桶掃描推斷所有權。
