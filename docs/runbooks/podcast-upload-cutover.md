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

4. 留下 pending_upload 的 session 不阻擋新流程。API 的背景清理仍會使用舊
   session 墓碑回收超過原有 cleanup_after 的未登記物件；失敗會重試。
   新 prefix 使用獨立掃描。確認最後沒有 queued／processing；記錄放棄或完成的
   session ID，保留 audit。舊 key 的保留與清理期限不縮短。
5. 執行一般 release。新 API 使用 `podcast-upload-spool` 磁碟 volume 做同步
   驗證。舊 batch init／finalize 在 production 回傳 410，避免舊頁面建立無人
   處理的工作；管理員重新載入頁面後使用 direct-uploads。
6. 依 [正式上傳驗收](production.md#direct-upload-production-verification) 驗證
   簽署、R2 PUT、同步 complete、播放、替換及失敗重試。確認容器清單不再包含
   `daily-insights-podcast-media-worker`。通過後依 Secret 管理流程移除舊 worker
   專用 key；歷史 schema 與原始音檔保持可追溯，不在此切換刪除。

重新整理遺失選檔時可立即選檔重新開始；新簽署不建立 DB 占用。
已 PUT 但未登記的音檔在簽署期限及最後修改時間皆超過預設 24 小時 grace 後，
由 API 每五分鐘掃描回收。已成功登記的憑證可冪等重送，即使期限已過。
