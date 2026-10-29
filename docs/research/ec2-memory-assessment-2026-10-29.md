# EC2 記憶體使用與規格評估（2026-10-29）

目前設定：保留 `t3.medium`，瀏覽器上限 **1.5 GiB**，正式瀏覽器並行數 **2**、fetch **4**。
下文保留先前 1／2 調整的歷史觀測，恢復紀錄見文末。

## 升級前評估結論

升級前正式主機為 `t3.small`，2 vCPU、標稱 2 GiB RAM，Linux 實際可見約 1,905 MiB，沒有 swap。
19:01–19:05（台北時間）的唯讀取樣顯示，可用記憶體約 68–231 MiB，後續程序盤點時約 77 MiB。
19:06:50 再查可用 115 MiB，PSI full avg60 為 18.52%；瀏覽器曾因健康檢查逾時成為 unhealthy，
19:07 起連續兩次健康檢查成功，19:08 檢查時已自行恢復 healthy。
記憶體壓力已影響執行效率，建議至少升至 **`t3.medium`（2 vCPU、4 GiB）**，
並優先評估降低新聞瀏覽器並行數。以下保留升級前觀測；後續依使用者授權執行的變更與驗證另記於文末。

本次是部署後短時間、實際新聞處理中的觀察，不是 24 小時負載或記憶體洩漏測試。
目前沒有證據證明存在記憶體洩漏，也不能由這個視窗推算整天尖峰或費用。

## 取樣與計算方式

- 使用既有 SSH 連線，只讀取 IMDSv2 的 instance type／region、`free`、`vmstat`、PSI、Docker stats／inspect、
  `/proc/*/smaps_rollup`、cgroup v2 memory counters 與相關日誌。
- 未輸出環境變數、完整程序參數或瀏覽器頁面 URL，避免帶出認證與文章內容。
- 三輪 Docker stats 之間原定間隔 15 秒；主機壓力造成命令延遲，因此實際主機快照時間為
  19:01:58、19:02:18、19:03:57，並非固定等距取樣。
- Linux 上 Docker CLI 會扣除部分檔案快取，不能直接把容器數字相加當作整台主機已用記憶體。
  程序採 PSS 交叉核對，按比例計入共享頁，避免直接累加 Chromium 子程序 RSS 的重複計算。
  cgroup `memory.current`／`memory.peak` 又包含不同範圍的快取與核心費用，表格分開呈現。
- 本機原始紀錄：`/private/tmp/pr103-ec2-memory-audit.log`、
  `/private/tmp/pr103-ec2-dispatcher-restart.log`、`/private/tmp/pr103-ec2-memory-final.log`、
  `/private/tmp/pr103-ec2-browser-health.log`。

## 主機實測

| 台北時間 | MemAvailable | PSI memory some avg60 | PSI memory full avg60 |
| -------- | ------------ | --------------------- | --------------------- |
| 19:01:58 | 231 MiB      | 21.70%                | 15.30%                |
| 19:02:18 | 68 MiB       | 20.39%                | 12.55%                |
| 19:03:46 | 96 MiB       | 32.62%                | 23.70%                |
| 19:03:57 | 124 MiB      | 29.51%                | 20.74%                |
| 19:06:50 | 115 MiB      | 26.07%                | 18.52%                |

PSI `full` 表示所有非閒置工作同時等待記憶體的時間比例；上述 1 分鐘平均約 13–24%，
5 分鐘平均亦約 14–18%，已是持續的資源爭用，不只是部署瞬間或可回收快取占滿畫面。
`vmstat` 的部分短秒取樣出現明顯讀取 I/O、I/O wait 與 CPU 忙碌；不能只靠此證明 I/O 全由記憶體回收造成。

程序盤點時，`AnonPages` 約 1,433 MiB，Slab 約 146 MiB，其中不可回收部分約 91 MiB；
PageTables 約 23 MiB。快取存在，但可用記憶體與 PSI 同時顯示真實壓力。
沒有 swap，匿名記憶體缺少換出的緩衝空間。

## 主要占用來源

Docker CLI 為三輪觀測範圍；PSS 為稍後的單次程序快照，兩者不是同時取樣。
cgroup peak 是各自容器 cgroup 存續期間峰值，不能把不同時間的峰值相加當作整機同時尖峰。

| 來源                   | Docker CLI 記憶體 | 程序 PSS | cgroup peak |
| ---------------------- | ----------------- | -------- | ----------- |
| CloakBrowser           | 452–542 MiB       | 455 MiB  | 654 MiB     |
| newsroom worker        | 322–325 MiB       | 322 MiB  | 360 MiB     |
| API                    | 159–171 MiB       | 156 MiB  | 240 MiB     |
| orchestration worker   | 約 150 MiB        | 151 MiB  | 219 MiB     |
| dispatcher             | 約 74 MiB         | 72 MiB   | 87 MiB      |
| browser-egress         | 約 60 MiB         | 59 MiB   | 72 MiB      |
| Web                    | 37–38 MiB         | 36 MiB   | 122 MiB     |
| nginx                  | 約 5 MiB          | 5 MiB    | 15 MiB      |
| 主機上的非容器程序合計 | 不適用            | 264 MiB  | 未取樣      |

瀏覽器加 newsroom worker 的 PSS 約 **777 MiB**，是最大的應用占用群組。
newsroom worker 內 Python 約 237 MiB PSS，Playwright driver（程序名 `MainThread`）約 85 MiB。
CloakBrowser 由多個 Chromium 程序、啟動器與 Xvfb 組成；Xvfb 約 20 MiB PSS，
不應把整個瀏覽器占用歸因於 Xvfb。主機 dockerd 約 55 MiB、containerd 約 35 MiB PSS，
其餘為 system services、容器 shim 等；本次盤點工具亦包含在當時主機程序快照中。

LLM 與 embedding 透過 `providers.py` 的 HTTP clients 呼叫外部端點；
這個觀測到的主要負載是文章瀏覽器、Playwright 與 Python workers，沒有發現本機載入模型權重的程序。
先前清理 Docker 映像改善的是磁碟容量，不能取代此處的 RAM 容量調整。

## 容器限制、生命週期與錯誤

- CloakBrowser 已設定 `mem_limit: 1g`，cgroup peak 約 654 MiB，`memory.events` 的
  `max`、`oom`、`oom_kill` 皆為 0。它沒有撞到自身 1 GiB 限制，整台主機仍可能先承受壓力。
- 其他 7 個容器目前沒有獨立記憶體上限；Docker 顯示的約 1.861 GiB 是主機上限，並非每個容器預留這麼多 RAM。
- `/tmp:size=512m` 是瀏覽器 tmpfs 上限，不是預先保留 512 MiB。
  cgroup 快照中 browser shmem 約 60 MiB，主要仍是約 405 MiB 的匿名記憶體。
  稍後容器內 `df` 顯示 `/tmp` 實用約 51 MiB，並非 512 MiB 上限已用滿。
- 所有當時存在的容器 cgroup，`oom` 與 `oom_kill` 都是 0；`State.OOMKilled` 為 false。
  這些狀態不能單獨排除已重啟或已移除 cgroup 的歷史 OOM。
- dispatcher 於 19:03 重啟一次，Docker die event exit code 為 1，沒有對應 oom event。
  其日誌明確記錄 psycopg／SQLAlchemy 的資料庫 DNS 解析失敗：`Temporary failure in name resolution`。
  後續已回到 healthy。不能把這次重啟直接稱為 OOM，或未經驗證就認定記憶體壓力造成 DNS 錯誤。
- 19:03 的補充 kernel journal 檢索（部署開始後的視窗）未找到 OOM 紀錄。
  主取樣腳本最後的日誌過濾因主機沒有 `rg` 而未執行成功，因此另外以 `grep` 補查；
  不把失敗的過濾命令當作「沒有 OOM」的證據。
  19:06 再查當天 00:00 起的可用 kernel journal，亦無匹配的 OOM 紀錄。
- 19:06 快照其餘 7 個容器均 healthy，但 CloakBrowser 為 unhealthy。
  最後 5 筆 browser health log 中，19:04–19:06 三筆是 5 秒健康檢查逾時／啟動 health check 逾時，
  19:07:20 與 19:07:55 則成功，19:08 已 healthy，restart count 仍為 0。
  同期主機有顯著記憶體、CPU 與 I/O 壓力，但沒有隔離實驗，不能將每次 timeout 都單獨歸因於 RAM。
  透過 CDP 讀取 target 數的額外唯讀探測在 20 秒外層 timeout 內未完成，因此沒有可用的 context／target 計數。

## 優化優先順序

1. **降低瀏覽器並行數，先做可比較的負載量測。**
   `Settings.newsroom_browser_concurrency` 預設 2，可先測試 1；fetch concurrency 預設 4，可同步評估 2。
   這可減少同時存活的 renderer／DOM 與抓取內容，代價是降低抓取吞吐量，需看 queue lag 與逾時是否仍可接受。
   升級時曾將 1／2 寫入正式 Compose；後續依使用者決策恢復 2／4，待實測確認記憶體不足再調整（見文末）。
   節省量需用相同工作負載驗證，本輪沒有聲稱已量到可回收多少 RAM。
2. **保留已有的節省機制。**
   `browser.py` 已阻擋圖片／影音／字型、每篇文章建立 context，並在 `finally` 關閉 context。
   不是缺少一般頁面關閉，也不必重複加入相同資源攔截。
   若後續長時間 RSS／PSS 單調上升，再用 context／target 數及 heap profile 判斷洩漏或套件快取。
3. **擴充後再訂容器預算與告警。**
   依代表性工作負載的 P95／peak 為 API、各 worker 設定預算，為 OS、部署與圖表／媒體工作保留餘裕。
   直接把瀏覽器限制下調到 512 MiB 已低於本次 654 MiB 峰值，可能製造容器 OOM，不能當作已驗證的優化。
   建議告警起點為 MemAvailable 持續低於 20%、memory PSI full 持續高於 1%、任何 OOM kill；
   這些是本案的營運起始門檻，不是 AWS 的固定標準。
4. **swap 只當短暫緩衝選項。**
   可評估小量 swap 作為突發保護，但會把壓力轉為 EBS I/O；當前已出現等待，不能靠 swap 取代足夠 RAM。
   本輪沒有建立 swap、清除 page cache、重啟程序或停用新聞功能。

## 規格建議與限制

| 選項                   | 評估                                                                                                                                         |
| ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| 維持 t3.small（2 GiB） | 目前餘裕與 PSI 不適合繼續承載完整 8 服務；只降低並行數能否足夠尚未實測，不能先假設可以。                                                     |
| t3.medium（4 GiB）     | 建議的最低擴充，維持 x86 與 2 vCPU，記憶體加倍；依已觀測到約 1.7–1.8 GiB 需求可增加明顯餘裕，仍需驗證尖峰。                                  |
| t3.large（8 GiB）      | 若需同時跑新聞抓取、08:00 orchestration、截圖／Podcast 工作，或 4 GiB 上仍頻繁低於 20% available，可選此容量。現有證據尚不足以說必須 8 GiB。 |
| 持續 CPU 高負載的機型  | 若長期 CPU 使用超過 T 系列 baseline，再比較固定效能的 M 系列或拆出瀏覽器 worker；本輪短取樣不足以定案。                                      |

AWS 規格中 t3.small 與 t3.medium 都是 2 vCPU、20% baseline／vCPU；升至 medium 主要解決 RAM，
不會增加 CPU 核心或 baseline。三次 CloakBrowser CPU 取樣約 22%、161%、188%（Docker 以每核心 100% 計），
有明顯工作波動，不能將最高值當作整日平均。

盤點時 AWS CLI session 已過期，該輪未取得 CloudWatch 的 24 小時 CPU、CPU credit balance／surplus 與費用；
後續升級時已依使用者確認重新登入。下一次容量驗收應至少涵蓋一個完整工作日與 08:00 尖峰，
確認 MemAvailable 通常高於 20%、PSI full 接近 0、沒有 OOM／非預期重啟，以及 queue lag／API latency 可接受。

目前 release workflow 只建置 `linux/amd64` 映像，不能把 Graviton／t4g 視為可直接就地換型的方案；
必須另行確認全部映像、瀏覽器及 AMI 的 arm64 支援。此評估沒有估算新加坡區帳戶實際月費。

## 依授權執行升級與調整（2026-10-29）

使用者授權升級至 `t3.medium`、瀏覽器並行數降至 1、fetch 降至 2，並驗證健康與記憶體。
另經確認執行 `aws login`，核對登入帳戶與 EC2 instance identity 一致。

### 已完成變更

- 對原 instance `i-008d2ee0de37aff31` 執行正常 stop、修改 instance type、start，沒有 force stop。
  19:18 確認 stopped 後修改規格，19:19 確認已以 `t3.medium` running。
  AWS API 確認 2 vCPU／4,096 MiB、x86_64、EBS root，Linux 可見約 3,831 MiB RAM。
- 操作前確認原公開位址為 Elastic IP；重啟後仍為 `56.10.51.231`，公開 DNS 未變，root volume 保留。
  沒有重建 EC2、切換架構或變更資料庫。
- 正式 `/opt/daily-insights/compose.production.yaml` 與本地 Compose 的 newsroom worker 環境值均為：
  `DAILY_INSIGHTS_NEWSROOM_BROWSER_CONCURRENCY=1`、`DAILY_INSIGHTS_NEWSROOM_FETCH_CONCURRENCY=2`。
  設定固定於正式 Compose，開發環境的應用預設仍為 2／4。
- 先核對主機 Compose 與已審查基準的 SHA-256，一致後才安裝新檔；原檔保留在
  `/opt/daily-insights/compose.production.yaml.before-memory-tuning-20261029`。
  以既有容器環境在程序記憶體內組合 Compose 所需設定，驗證新 worker 的其他環境值與所有映像 digest 不變；
  沒有輸出 secrets、建立含 secrets 的 env 檔或重新取得部署金鑰。
- 只用既有映像重建 newsroom worker；其他 7 個容器 ID 在這次重建操作前後一致。
  容器在 EC2 重啟時皆依 Docker restart policy 恢復。
  19:21 額外透過應用的 `Settings` 確認實際生效值為 browser 1、fetch 2。

### 驗證結果

19:21 的正式 `health.sh` 通過，涵蓋 8 個容器與 nginx 對 API、Web 的內部探測。
19:22:51 再查 8 個容器全部 running／healthy、`State.OOMKilled=false`，restart count 為 0；
本次開機的 kernel journal 沒有匹配的 OOM 紀錄。重啟後的 counters 與開機前歷史分開解讀。
EC2 system／instance status 均為 `ok`；外部登入頁 HTTP 200，該次回應約 1.14 秒。
資料庫仍為 `20261029_0055 (head)`，API／Web／瀏覽器映像 digest 與升級前相同。
磁碟仍可用約 7.9 GB（57% 使用率），未因本次調整增加映像下載或建立 swap。

| 19:21–19:22 取樣 | MemAvailable | 占可見 RAM 比例 | memory PSI full avg60 |
| ---------------- | ------------ | --------------- | --------------------- |
| 19:21:57         | 2,027 MiB    | 52.9%           | 0.01%                 |
| 19:22:22         | 1,778 MiB    | 46.4%           | 0.00%                 |
| 19:22:46         | 1,979 MiB    | 51.7%           | 0.00%                 |

相較升級前 48–231 MiB 的可用記憶體，以及 PSI full avg60 約 13–24%，
本次變更後 RAM 餘裕明顯改善，取樣中記憶體等待接近 0。
瀏覽器仍在處理工作：Docker CPU 三次約 129%、162%、178%，並非全部服務閒置時的快照。
本次沒有固定相同文章與工作負載做 A/B 實驗，且重啟會清除部分程序狀態與重新暖機，
因此不把改善量分別歸功於換型或降低並行數，也不聲稱整日 CPU 或記憶體尖峰已完成驗證。

當時 Docker CLI 的 browser 記憶體約 633–716 MiB、newsroom worker 約 288–349 MiB；
兩者仍是主要來源，沒有因 host 換型就消失。t3.medium 的 CPU 核心數與 baseline 沒有增加，
後續仍須涵蓋完整工作日、08:00 尖峰與 CPU credit；目前驗證支持「RAM 壓力已改善且服務健康」。

本地 `make check-production-deployment`（含 nginx DNS replacement regression）、
變更檔案的 Prettier、validator 的 Ruff lint／format，以及 `git diff --check` 均通過。
主機調整階段沒有修改應用邏輯或重新執行先前已通過的完整產品 CI。
後續提交與推送由既有 hooks 執行 format、lint、type 檢查，pre-push 另執行隔離 PostgreSQL migration 與完整單元／整合測試。
操作輸出保留在 `/private/tmp/pr103-apply-memory-tuning.log`、
`/private/tmp/pr103-ec2-medium-health.log` 與 `/private/tmp/pr103-ec2-medium-memory.log`。

Compose、validator 與維運文件已同步至修正分支 `fix/production-newsroom-concurrency`，透過 PR 合併至 main。
原先 PR 用途是避免舊 bundle 將主機的 1／2 蓋回應用 2／4 預設；後續決策已改為保留 2／4，PR 與主機設定同步調整。
規格升級為 EC2 控制平面上的持久變更，不受該 Compose 覆寫影響。

## 後續決策：恢復原有並行數（2026-10-29）

使用者改為保留原有抓取吞吐量，待後續實測確認記憶體不足再調整。
正式主機與 [PR #128](https://github.com/FPI-TW/daily-insights/pull/128) 的 Compose 已同步恢復
`DAILY_INSIGHTS_NEWSROOM_BROWSER_CONCURRENCY=2`、`DAILY_INSIGHTS_NEWSROOM_FETCH_CONCURRENCY=4`。
主機規格維持 `t3.medium`，沒有再次重啟 EC2 或變更映像／資料庫。

先核對正式 Compose 與先前 1／2 版本的 hash，保存
`/opt/daily-insights/compose.production.yaml.before-concurrency-restore-20261029` 後才更新檔案。
只重建 newsroom worker，驗證其他 7 個容器 ID 與全部映像 reference 維持一致；
19:44 透過應用 `Settings` 確認實際生效為 browser 2、fetch 4。

19:44:02、19:44:26 兩次正式 `health.sh` 均通過，8 個容器皆 healthy，外部登入頁 HTTP 200。
兩次 MemAvailable 分別為 1,947 與 1,464 MiB（約 1.4–1.9 GiB）；
memory PSI full avg60 分別為 0.53% 與 1.58%。這是重建後短時間快照，並非完整工作日或尖峰容量驗證，
也不能直接套用前一階段 1／2 設定時 PSI 接近 0 的結論。
保留目前 2／4 設定，後續若有持續壓力、OOM、queue lag 或服務異常，再依證據評估調整。

補查發現 CloakBrowser 的 cgroup `memory.current` 約 1,022 MiB，接近既有 `memory.max=1 GiB`；
`memory.events max=16072`、`oom=0`、`oom_kill=0`，memory PSI full avg60 約 3.66%。
這些是自本次開機以來同一 browser cgroup 的累計／峰值，沒有恢復前的對照值，不能歸因為全由 2／4 造成。
主機仍有餘裕，不能把容器觸及自身限制描述為 EC2 整體 RAM 已耗盡；
後續評估須同時區分容器上限、主機 MemAvailable 與實際服務影響。本次沒有修改瀏覽器記憶體上限。

本次部署契約檢查通過；操作紀錄在 `/private/tmp/pr128-restore-concurrency.log` 與
`/private/tmp/pr128-restored-health.log`。

## 1 GiB 瀏覽器上限的連續驗證（2026-10-29）

使用者要求維持 browser 2、fetch 4，檢查是否受容器記憶體上限限制，並評估是否應調高。
本輪只讀取正式主機與容器指標，沒有調整上限、並行數、容器或 EC2 規格。
取樣開始前核對 worker 的實際環境值為 2／4；CloakBrowser 容器與啟動時間在取樣前後一致，
因此下列差值來自同一 cgroup，不是重啟後計數重設或不同工作階段的累計比較。

### 連續取樣證據

台北時間 19:50:39–19:52:19，每 20 秒取樣一次，共 6 次、約 100 秒。
`memory.max` 確認為 1,073,741,824 bytes（1 GiB），`memory.high=max`，CPU cgroup 沒有配額上限。
主機沒有 swap；cgroup 的 swap 上限值不代表主機有實際可用 swap。

| 台北時間 | memory.current | memory.events max | browser memory PSI full avg60 | 主機 MemAvailable |
| -------- | -------------- | ----------------- | ----------------------------- | ----------------- |
| 19:50:39 | 907 MiB        | 50,267            | 0.45%                         | 1,532 MiB         |
| 19:50:59 | 718 MiB        | 50,267            | 0.32%                         | 1,815 MiB         |
| 19:51:19 | 735 MiB        | 50,456            | 0.34%                         | 1,752 MiB         |
| 19:51:39 | 861 MiB        | 50,840            | 0.49%                         | 1,644 MiB         |
| 19:51:59 | 1,008 MiB      | 51,153            | 0.43%                         | 1,488 MiB         |
| 19:52:19 | 819 MiB        | 51,705            | 0.35%                         | 1,557 MiB         |

- **這段期間又新增 1,438 次 max boundary 命中**，不只依賴先前累計 peak 推斷。
  依核心定義，`memory.events max` 計數的是配置即將越過上限、需要回收的次數，不是失敗的文章數、OOM 次數或超限 bytes。
  20 秒間隔的 memory.current 未必截到每個瞬間尖峰；max 計數增量確認期間確實再次觸頂。
- 同一時段 direct scan 增加 170,059 pages、direct steal 增加 104,709 pages，
  file workingset refault 增加 120,543，major fault 增加 8,235，cgroup I/O 讀取約增加 471 MiB。
  此組合支持正在進行回收與檔案頁重新讀取，不能把所有讀取量都視為上限造成，因為沒有放寬上限的對照實驗。
- 由 PSI total 的差值計算，期間 browser some stall 約 2.21%、full stall 約 0.44%。
  memory.high 未設定，因此不是 memory.high 的主動節流；也不能把此數字直接解釋為文章吞吐量損失百分比。
- 匿名記憶體約 408–614 MiB，file 約 244–377 MiB，kernel 約 23–32 MiB。
  shmem 約 71–130 MiB 已包含在 file 中，不能再相加；容器占用並非全部都是可無成本回收的閒置快取。

### 健康與其他瓶頸

取樣期間及其後 `oom`、`oom_kill` 皆為 0，`State.OOMKilled=false`、restart count 為 0。
最後 5 次 browser health check 均成功，耗時約 0.7–1.4 秒，容器為 healthy。
有界的近期日誌檢索未匹配 browser timeout／crash，但 worker 當時沒有可用的應用事件輸出，
因此不能僅靠日誌搜尋宣稱所有抓取皆成功。

同一時段 browser 平均使用約 1.57 個 CPU core，主機 CPU PSI some avg60 約 90–92%，
另有明顯 CPU 排隊壓力。這個 PSI 是至少部分工作等候 CPU 的時間比例，並非 CPU 使用率。
即使放寬 RAM 上限，也不能承諾消除 CPU 瓶頸或既有抓取逾時。

### 容量評估與建議

**1 GiB 確實在尖峰限制瀏覽器；建議先以 1.5 GiB（Compose `1536m`）做對照試調，維持 browser 2、fetch 4。**
目前沒有 OOM 或健康失敗證據，所以此建議是降低回收成本、增加尖峰餘裕，並非已證明需要緊急擴容。
沒有證據支持直接取消容器上限或再升級 EC2。

取樣中的 host MemAvailable 約 1,488–1,815 MiB。
若新增的 512 MiB 全部使用，依當時最低餘裕作一階估算，主機仍約有 976 MiB（約 25%）可用；
此估算沒有包含其他服務的新尖峰。若直接從 1 GiB 調到 2 GiB，同樣估算的低點只剩約 464 MiB（約 12%），
因此先選 1.5 GiB 較符合目前 4 GiB 主機的餘裕。

後續若執行試調，應同步正式 Compose 與主機限制，對照相近文章量與來源組合下：
max 命中增量、PSI、回收／refault、抓取耗時、queue lag，以及 host MemAvailable 是否仍通常高於 20%。
若回收等待降低而 CPU 排隊仍高，須分開評估 CPU 容量，不再用降低並行數同時改變比較條件。
該評估階段尚未試調；後續依授權套用 1.5 GiB 的結果另記於下節，全天尖峰安全性仍待驗證。

原始唯讀取樣與工具分別保留在 `/private/tmp/pr128-browser-limit-audit.jsonl`、
`/private/tmp/pr128-browser-limit-audit.py`。

## 套用 1.5 GiB 上限與恢復驗證（2026-10-29）

使用者授權提高至 1.5 GiB，保留 browser 2、fetch 4，驗證健康與記憶體壓力，並同步 Compose、文件及 PR #128。

### 新發現：1 GiB 階段已發生 OOM

20:12–20:13 的新基準仍為 1 GiB，`memory.events` 已累計 `max=299013`、`oom=3`、`oom_kill=2`。
核心日誌確認 **20:04:49、20:09:08** 在 browser cgroup 內各殺掉一個 `chrome` 程序；
這發生在本次調整之前，更新了前次 19:50–19:52 取樣「當時尚未 OOM」的歷史狀態。
容器仍為 running／healthy、restart count 0，但 CDP `/json/version/` 實際回覆 502。
因此根端點的現有 healthcheck 不足以證明 Chrome 後端可用；未將該 healthy 狀態當作功能恢復證據。

此時 browser CPU 平均僅約 0.019 core、回收計數沒有增量，不能把這段失效／低負載時窗的 PSI 0
當作有效的調整前活躍負載對照。比較另列出先前同為 2／4、仍在活躍處理的 19:50–19:52 時窗。

### 已套用的變更

- 本地與正式 `/opt/daily-insights/compose.production.yaml` 的 `cloakbrowser.mem_limit` 均為 `1536m`，
  即 1,610,612,736 bytes；Docker HostConfig 與核心 `memory.max` 都已核對。
- 安裝前先渲染新舊 Compose，在記憶體內確認有效模型只有 browser mem_limit 改變；
  原正式檔保留為 `/opt/daily-insights/compose.production.yaml.before-browser-1536m-20261029`。
  沒有輸出 secrets 或建立包含 secrets 的 env 檔。
- 先用 `docker update --memory 1536m` 動態套用，所有容器 ID、映像、命令與環境維持原值。
  既有 `MemorySwap=2 GiB` 的 memory+swap 總額設定未變；主機沒有 swap，也未建立 swap。
- 提高上限後，先前被 OOM 殺掉的 Chrome 程序未自動復原，CDP 仍回 502。
  因此在 **20:17:33** 僅重啟 browser 容器，以 1.5 GiB 恢復 Chrome 後端。
  容器 ID 與映像不變，StartedAt 更新；其他 7 個容器未重啟，worker 的 2／4 設定未變。
- 已同步首次部署手冊與新聞架構文件，修正先前把 browser 執行時占用描述為固定約 400 MB 的文字。

### 壓力與功能驗證

恢復後 20:19:15–20:20:55 再採 6 次、每 20 秒一次的約 100 秒時窗：

| 指標                                  | 先前 1 GiB 活躍時窗（19:50–19:52）           | 1.5 GiB 恢復後活躍時窗（20:19–20:20） |
| ------------------------------------- | -------------------------------------------- | ------------------------------------- |
| browser／fetch 並行                   | 2／4                                         | 2／4                                  |
| CPU 平均使用核心數                    | 1.57                                         | 1.58                                  |
| memory.current 取樣範圍               | 718–1,008 MiB                                | 976–1,171 MiB                         |
| cgroup 存續期間 peak                  | 約 1,024 MiB                                 | 約 1,350 MiB                          |
| 時窗內 max 命中增量                   | 1,438                                        | 0                                     |
| 時窗內 direct scan 增量               | 170,059 pages                                | 0                                     |
| 時窗內 file refault 增量              | 120,543                                      | 525                                   |
| 由 PSI total 差值計算 some／full 等待 | 2.21%／0.44%                                 | 0%／0%                                |
| 主機 MemAvailable 範圍                | 1,488–1,815 MiB                              | 1,521–1,772 MiB                       |
| OOM／oom_kill                         | 該早期時窗仍為 0／0；20:04、20:09 後發生 OOM | 新 cgroup 累計 0／0                   |

browser 實際占用已超過原 1 GiB，並在新上限下沒有新增 max 命中、direct reclaim 或 PSI total 等待。
這支持新增空間能承接實際工作量，主機仍保有約 40–46% 的可用 RAM。
這些視窗未固定相同文章、來源與 queue 狀態，而且中間為恢復故障而重啟了 browser，
因此不能宣稱嚴格 A/B 實驗、把所有差異都歸因於上限，或保證完整工作日不再觸頂。
重啟重設了 cgroup 計數器，表格比較時窗內增量，沒有用「歸零」抹除先前兩個程序遭 OOM 殺掉的事實。
第一次跨越重啟的取樣因 StartedAt 改變而被工具判為無效，沒有用於比較。

`health.sh` 已通過，8 個正式容器均 healthy。另用 worker 內的 Playwright 實際連上 CDP，
在獨立 context／page 中執行 DOM 寫入、讀取與 JavaScript，結果成功；probe 不導覽外部 URL、不呼叫 LLM，
結束後只關閉自己的 context。先前取用工作中頁面的探測遇到頁面被 worker 關閉的 race；
以 `set_content` 等待 load 的探測亦曾逾時，最終使用獨立頁面的直接 DOM evaluate 完成驗證。
這個功能測試避免僅依賴根端點的 HTTP 200；現有 Compose healthcheck 仍使用根端點，後續宜評估加入 CDP 後端探測。

後續觀察重點仍為 1.5 GiB 下的 max／OOM 增量、PSI、抓取錯誤與 queue lag，並維持 2／4 以免同時改變比較條件。
若 peak 再次接近 1.5 GiB，應先核對工作量與主機餘裕；本輪沒有進一步提高上限、降低並行數或變更 EC2 規格。

操作及取樣紀錄：`/private/tmp/pr128-browser-1536m-apply.log`、
`/private/tmp/pr128-browser-before-1536m.jsonl`、`/private/tmp/pr128-browser-after-recovery-1536m.jsonl`、
`/private/tmp/pr128-browser-1536m-cdp-dom.log`。
部署契約（含 nginx DNS replacement regression）已通過；提交與推送沿用既有 hooks 的完整檢查。

## 參考資料

- [AWS T3 規格](https://aws.amazon.com/ec2/instance-types/t3/)：CPU、記憶體與 baseline；頁面價格為美東區，不套用為新加坡區報價。
- [Linux PSI 定義](https://docs.kernel.org/accounting/psi.html)：some、full 與時間窗解釋。
- [Docker stats 記憶體計算](https://docs.docker.com/reference/cli/docker/container/stats/)：Linux CLI 扣除快取的口徑。
- [Linux cgroup v2 記憶體介面](https://docs.kernel.org/admin-guide/cgroup-v2.html)：memory.max、events、stat 與回收的定義。
- [Docker resource constraints](https://docs.docker.com/engine/containers/resource_constraints/)：容器硬性記憶體上限與主機資源的關係。
- [Docker container update](https://docs.docker.com/reference/cli/docker/container/update/)：動態變更運行中容器的 memory limit。
- [AWS EC2 監控](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/monitoring_ec2.html)：主機與 CloudWatch agent 監控機制。
