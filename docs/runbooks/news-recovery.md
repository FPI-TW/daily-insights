# 新聞失敗與恢復操作

## 對讀者與管理員的承諾

讀者端維持原卡片、六則分頁及循環滑動，不新增新鮮度、延遲、更新中或錯誤提示。
新版本沒有可見新聞時，API 回退到最近可發布且含可見新聞的版本；已載入頁面刷新失敗
保留原資料。沒有任何歷史可用內容時沿用原空資料呈現，不使用未驗證內容填補。
基礎設施全面離線時，這不保證首次造訪仍能載入新聞。

所有排程、錯誤與恢復資訊集中於 `/zh-hant/admin/news-management`（另有簡中、英文）。
作業歷史缺乏新欄位時顯示「未記錄」，不從篇數猜測失敗原因。

## 失敗分類

| 類別                                                     | 處理                                                                               |
| -------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| Feed／文章逾時、DNS／暫時連線、408、5xx                  | 保存進度，只重試受影響來源／文章；其他來源繼續                                     |
| 來源或新聞模型 429                                       | 保存共享冷卻；按 host 或 `provider:news` 隔離，遵守較長 `Retry-After`              |
| Feed 無新內容／過舊                                      | 記錄最新文章時間；正常完成，不單獨重試                                             |
| 404／410、付費牆、短正文、來源存取限制、缺設定、解析失敗 | 略過並記錄原因，同一恢復鏈不反覆抓永久失敗文章                                     |
| SSRF／非白名單／超限                                     | 拒絕，不降低安全限制、不重試                                                       |
| 模型 401／402／403、缺金鑰                               | 暫停共享新聞供應商，修復後人工恢復                                                 |
| 模型 408／5xx／暫時連線                                  | 停止後續模型碰撞，保存已驗證語系，退避恢復                                         |
| JSON／欄位／摘要或翻譯數字驗證                           | 同輸入至多修正一次；次數先保存，續跑不重置。摘要／翻譯再失敗略過遞補，選題停止該批 |
| 模型參數錯誤／未知例外                                   | 安全錯誤代碼與請求編號，交人工處理，不盲目重試                                     |
| Worker／資料庫中斷                                       | 付費請求前檢查持久化及所有權；租約到期且 execution lock 釋放後續跑                 |
| 正常 0、1、4 則或其他不足額                              | 作業可為 succeeded；不因此重試。全面來源故障另外標示                               |

若 408／429／5xx 或暫時連線錯誤發生在已送出的唯一修正呼叫，修正額度不會因 worker
重啟而重置；該輸入改列人工處理，不再發出第二次修正。

`news_workflows.state` 與 `publication` 分離。edition `complete/partial/unavailable`
保留相容性；自動重試只讀 `waiting_retry` 和 `next_retry_at`，不再讀 edition 篇數。
來源永久問題可與完成並存，發布結果仍記為 technical_degradation。

## 自動時段與人工恢復

1. 統一 dispatcher 於台北每日 08:00 建立當日 RoutineRun，其中唯一的
   `internal_services_daily_update` JobRun 包含三個 refresh functions；三者 terminal
   後執行 `news_publish`。Refresh 與 publish 分離，publish 不再抓取外部資料。
2. 暫時故障每 30 分鐘只重試缺失 scopes；供應商要求更久時延後至其期限。
   任一市場的系統性錯誤等待重試時，同批其他市場不得 claim；原市場恢復成功後才繼續。
   不可重試的系統性錯誤會以同一安全代碼終止尚未執行的同批市場。這項跨市場閘門不適用
   於單篇摘要／翻譯驗證失敗所形成的 `partial`／`unavailable`。
3. 10:00:00 起不開始新的 automatic 外部 attempt；進行中的單次請求可完成並保存。
   `news_publish` 會在 dependencies terminal 後執行，避免 routine 永久等待。
   發布器會略過純 `failed`／`cancelled` 且沒有可保存 partial 批次的市場，不建立空
   `unavailable` revision；同批其他市場若已有保存成功或 partial 批次，仍可獨立發布。
4. 額度、金鑰、權限或模型設定修復後，從新聞管理頁建立新的 current-edition manual
   JobRun。完整重抓使用 `news_daily_update`；單一市場使用
   `news_global_refresh_job`、`news_tw_equity_refresh_job` 或
   `news_us_equity_refresh_job`。通用 API 為
   `POST /api/admin/orchestration/job-runs`，需 admin 登入及 CSRF。每次提交建立新 run；
   與 automatic work 或相同 Provider 衝突時排隊，不回傳 409。
5. 供應商暫停時一次只允許一個 worker 試做一筆待處理模型請求。成功才解除暫停；
   失敗記錄新原因，JSON 失敗也不在 probe 內額外修正。
6. Manual jobs 僅限當日，含 10:00 後；建立後使用一小時 soft deadline，允許立即執行
   與一次 30 分鐘重試。截止後不再開始 refresh attempt，已執行工作可完成，隨後
   `news_publish` 依 terminal dependencies 以可用批次取得一次發布機會；若該發布失敗，
   不再跨越 deadline 重試。失敗保留進度；跨日由新日作業處理。
   「重新抓取」是新執行，不是 checkpoint 續跑；修正預算是各恢復鏈中每個輸入獨立。
7. 心跳超過 90 秒顯示無回應，但不直接搶工作；仍需租約與鎖允許。取消撤銷發布權，
   未完成 probe 退回暫停，取消作業不得續建重試。

後台 GET 只對網路故障及 502／503／504 以 2、5 秒最多再試兩次；其後手動重新載入。
401 走既有登入失效流程，403 顯示權限不足，其餘錯誤附請求編號。POST 不自動重送。
初次資料保留 skeleton，刷新失敗保留原資料並顯示局部錯誤。

## 保存、安全與併發

- `news_checkpoints` 保存候選中繼資料、內容 SHA-256、成功選題、繁中摘要及驗證完成的翻譯；
  不保存正文、完整 prompt 或原始例外訊息。模型 audit 與正式新聞維持原保存政策。
- 每次模型修正嘗試皆保存安全 `error_code`、request id、token/latency metadata；
  FunctionAttempt 另保存候選對應的 bounded failure reason，不保存模型原始輸出。
- 統一編排在模型送出前，於 `FunctionRun.result._news_model_attempts` 保存以輸入 fingerprint
  為鍵的呼叫預約與已驗證結構化結果；相同候選、正文、模型、prompt、語系與繁中基準摘要
  在 worker 重啟後直接重用成功結果，且仍共用最多一次修正額度。所有文章皆因內容驗證
  耗盡時不建立自動重試；人工上架的 `summary_failed`／`fetch_failed` 亦為終態，不排入
  技術性 retry。
- 選題 key 包含候選內容、模型、prompt 摘要與市場政策；摘要 key 包含文章內容、候選、
  模型與摘要 prompt 版本；翻譯 key 另包含候選 identity、已驗證繁中摘要、目標語系及翻譯
  prompt 版本。變動僅使受影響階段失效；相同正文的不同候選不共用翻譯失敗狀態。
- Feed 成功中繼資料可重用；全文 feed 和必要文章正文會重新取得。失敗 feed 才再探索。
- 每次外部呼叫後短交易保存，不在付費呼叫期間持有長交易。文章必須三語都通過才發布。
- 翻譯沿用摘要的嚴格 JSON schema 與原文數字忠實度檢查；失敗分別記錄
  `translation_invalid_json` 或 `translation_ungrounded_number`，不得只因繁中摘要已通過就略過。
- 人工上架亦限當日，並於模型呼叫前與發布交易中檢查目前版本、人工隱藏、事件去重及
  配額；五星不設上限，四星最多十則，一至三星合計最多五則，不為湊足下限放寬品質。
- 市場／日期 advisory lock 隔離版本；發布交易再次鎖定作業並確認租約所有權。隱藏的
  URL／事件對所有 revision 生效，回退與續跑不會重新出現。
- Legacy 自動產生與人工上架共用同一市場／日期 advisory lock；模型執行中不建立 provisional
  edition，正常終態才一次提交完整 revision，因此取消或系統性失敗不需補償刪除，也不會刪到
  已成功人工上架的內容。
- Deadline 將 retry_wait／未執行市場終止為 `unavailable` 只代表排程終態，不代表可發布的正常
  零則結果；publish 必須再確認最新 attempt 與 batch 都是正常 `unavailable`，否則記為 blocked，
  不建立空 revision。
- Checkpoint 建立後 48 小時清理；orchestration worker 執行新聞工作時會清理過期資料。
  不清理正式新聞、JobRun／FunctionRun／Attempt 歷史或 audit。若長期停用新聞功能，
  需以維運程序安排清理，而不是恢復舊 scheduler。
- 外部請求成功、結果尚未落庫就中斷，仍可能重送；不是跨供應商的 exactly-once 保證。

## 本機代理驗收

本機與 production 使用同一固定 Nginx 映像。Docker DNS `127.0.0.11`、upstream
`zone`／`resolve` 支援 API／web IP 更換。內部 127.0.0.1:8080 的 health 路徑實際
代理 API readiness 與 web login，對外無法使用；不能只看 API 容器 healthy。

```sh
# 隔離 Docker mock 環境：換 API、web IP，佔住舊 IP，檢查不用 reload 即恢復。
sh scripts/test-production-nginx-dns.sh '' local
sh scripts/test-production-nginx-dns.sh

# 本機更新後，確認入口與內部健康檢查（不要重置 postgres volume）。
docker compose exec -T nginx nginx -t
curl -I http://localhost:8080/zh-hant/login
curl -i http://localhost:8080/api/admin/news/recovery
```

未登入 recovery 端點應是 401 而非 502；完整驗收仍須用已登入管理員開啟新聞管理頁、
確認狀態／來源面板，再到台股與美股頁檢查新聞及分頁。驗收不需要按產生或付費恢復。

新增式 migration `20260911_0025` 必須先於新 worker 完成；不得刪除有進度的資料表
作為回滾手段。需要回退應保留資料並暫停新版 worker，評估既有版本是否理解當前 gate。
