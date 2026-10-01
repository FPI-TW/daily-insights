# EC2 首次部署操作手冊

正式主機：

```text
Host: ec2-56-10-51-231.ap-southeast-1.compute.amazonaws.com
User: ubuntu
OS: Ubuntu 26.04 LTS
Architecture: x86_64
Capacity: 2 vCPU / 約 2 GB RAM / 19 GB root volume
```

2026-07-27 已透過 Docker 官方 Ubuntu repository 安裝並驗證：

- Docker Engine 29.6.2；
- containerd 2.2.6；
- Docker Buildx 0.35.0；
- Docker Compose plugin 5.3.1。

Docker 與 containerd 已設為開機啟動，`ubuntu` 已加入 `docker` 群組並通過
`hello-world` smoke test。EC2 不需要 Node.js、pnpm、uv、應用 Python
dependencies、nginx、PostgreSQL、AWS CLI 或 SSM Agent。

## 1. 部署與重開機模型

Daily Insights 比照 FindB：

1. GitHub Actions 從 protected `production` Environment 讀取 Secrets 與
   Variables；
2. `appleboy/ssh-action` 將值放入該次 SSH deployment process 的環境；
3. `docker compose up` 建立 container 時，將環境寫入 Docker container
   configuration；
4. API、Web、nginx 都使用 `restart: unless-stopped`；
5. EC2 reboot 後，systemd 啟動 Docker，Docker 以既有 container
   configuration 自動恢復服務。

不建立 `/etc/daily-insights/runtime/*.env`，不使用 Compose `env_file`，也不
安裝 Daily Insights application systemd unit。GitHub Secrets 不會寫入 EC2
檔案，但具有 Docker daemon 權限的管理者仍可透過 container inspect 讀取
container environment；Docker 權限應視同 root 權限管理。

## 2. GitHub production Environment

建立 GitHub Actions Environment `production` 並啟用必要 reviewer。

Always-required Secrets：

```text
DAILY_INSIGHTS_EC2_HOST
DAILY_INSIGHTS_EC2_USER
DAILY_INSIGHTS_EC2_SSH_KEY
DAILY_INSIGHTS_DATABASE_URL
DAILY_INSIGHTS_SESSION_SECRET
DAILY_INSIGHTS_PASSWORD_PEPPER
DAILY_INSIGHTS_R2_ACCESS_KEY_ID
DAILY_INSIGHTS_R2_SECRET_ACCESS_KEY
```

Conditional-required Secrets：

- `DAILY_INSIGHTS_MORNING_REPORTS_ENABLED=true`：
  `DAILY_INSIGHTS_TWELVE_DATA_API_KEY`；
- `DAILY_INSIGHTS_NEWSROOM_ENABLED=true`：`DAILY_INSIGHTS_NEWSROOM_LLM_API_KEY` 與
  `DAILY_INSIGHTS_NEWSROOM_EMBEDDING_API_KEY`；
- `DAILY_INSIGHTS_ANALYST_VIEWPOINTS_ENABLED=true`：
  `DAILY_INSIGHTS_ANALYST_VIEWPOINTS_API_KEY`；
- `DAILY_INSIGHTS_CHAT_ENABLED=true`：`DAILY_INSIGHTS_CHAT_MODEL_API_KEY`。

主機設定：

```text
DAILY_INSIGHTS_EC2_HOST=ec2-56-10-51-231.ap-southeast-1.compute.amazonaws.com
DAILY_INSIGHTS_EC2_USER=ubuntu
```

`DAILY_INSIGHTS_EC2_SSH_KEY` 儲存 `key/daily-insights-key.pem` 的完整內容，
private key 不得 commit。

Always-required Variables：

```text
PUBLIC_HOSTNAME
DAILY_INSIGHTS_ORCHESTRATION_ENABLED
DAILY_INSIGHTS_ORCHESTRATION_ACTIVATION_DATE
DAILY_INSIGHTS_MORNING_REPORTS_ENABLED
DAILY_INSIGHTS_NEWSROOM_ENABLED
DAILY_INSIGHTS_ANALYST_VIEWPOINTS_ENABLED
DAILY_INSIGHTS_YFINANCE_ENABLED
DAILY_INSIGHTS_TWSE_ENABLED
DAILY_INSIGHTS_CHAT_ENABLED
DAILY_INSIGHTS_R2_ENDPOINT_URL
DAILY_INSIGHTS_R2_BUCKET_NAME
DAILY_INSIGHTS_R2_SIGNED_URL_TTL_SECONDS
```

Workflow 也會把 build job 產生的 `API_IMAGE` 與 `WEB_IMAGE` 視為 always-required
deployment values，並驗證 immutable SHA-256 digest；兩者不是人工設定的 GitHub
Environment Secret 或 Variable。

`DAILY_INSIGHTS_MORNING_REPORTS_ENABLED`、`DAILY_INSIGHTS_NEWSROOM_ENABLED`、
`DAILY_INSIGHTS_ANALYST_VIEWPOINTS_ENABLED`、`DAILY_INSIGHTS_YFINANCE_ENABLED` 與
`DAILY_INSIGHTS_TWSE_ENABLED`、`DAILY_INSIGHTS_CHAT_ENABLED` 都必須明確設為 `true`
或 `false`。**未設定時 deployment validation 會直接中止**，不會退回 Compose
default。

`DAILY_INSIGHTS_ORCHESTRATION_ENABLED` 在統一架構切換後必須固定為 `true`。
`DAILY_INSIGHTS_ORCHESTRATION_ACTIVATION_DATE` 第一次切換時設為部署日的下一個台北
日期；之後部署保留原值且不得晚於當日。若 migration 已完成但第一個 RoutineRun 尚未
建立，重試時可設為當日或下一個台北日期。

Conditional-required Variables：

- `DAILY_INSIGHTS_MORNING_REPORTS_ENABLED=true`：
  `DAILY_INSIGHTS_TWELVE_DATA_BASE_URL`；
- `DAILY_INSIGHTS_ANALYST_VIEWPOINTS_ENABLED=true`：
  `DAILY_INSIGHTS_ANALYST_VIEWPOINTS_BASE_URL` 與
  `DAILY_INSIGHTS_ANALYST_VIEWPOINTS_TIMEOUT_SECONDS`；
- `DAILY_INSIGHTS_CHAT_ENABLED=true`：`DAILY_INSIGHTS_CHAT_MODEL_PROVIDER`、
  `DAILY_INSIGHTS_CHAT_MODEL_NAME`、`DAILY_INSIGHTS_CHAT_MODEL_API_BASE_URL` 與
  `DAILY_INSIGHTS_CHAT_TIMEOUT_SECONDS`。

重點新聞（newsroom）除 always-required `DAILY_INSIGHTS_NEWSROOM_ENABLED` 與啟用時必填的
`DAILY_INSIGHTS_NEWSROOM_LLM_API_KEY`、`DAILY_INSIGHTS_NEWSROOM_EMBEDDING_API_KEY` 外，
下列都是 optional/defaulted configuration，空值時使用括號內的 Compose default：

- Variables `DAILY_INSIGHTS_NEWSROOM_LLM_BASE_URL`（`https://api.deepseek.com`）與
  `DAILY_INSIGHTS_NEWSROOM_EMBEDDING_BASE_URL`（`https://api.openai.com/v1`）：有設定時
  必須是 absolute HTTPS URL；
- Variables `DAILY_INSIGHTS_NEWSROOM_TRIAGE_MODEL`、`DAILY_INSIGHTS_NEWSROOM_EDITOR_MODEL`、
  `DAILY_INSIGHTS_NEWSROOM_ANALYSIS_MODEL`、`DAILY_INSIGHTS_NEWSROOM_TRANSLATE_MODEL`
  （皆為 `deepseek-chat`）與 `DAILY_INSIGHTS_NEWSROOM_EMBEDDING_MODEL`
  （`text-embedding-3-small`，向量必須是 1536 維）；
- Secret `DAILY_INSIGHTS_NEWSROOM_SLACK_WEBHOOK_URL`：Slack incoming webhook，有設定時
  必須以 `https://hooks.slack.com/` 開頭；未設定時通知只寫入 worker log；
- Variables `DAILY_INSIGHTS_NEWS_EXTRA_HOSTNAMES`、`DAILY_INSIGHTS_NEWS_BLOCKED_HOSTNAMES`
  與 `DAILY_INSIGHTS_SEC_CONTACT_EMAIL`，以及 Secret `DAILY_INSIGHTS_GUARDIAN_API_KEY`：
  可留空；需要憑證的來源在未設定時略過。

`DAILY_INSIGHTS_NEWSROOM_ADMIN_BASE_URL` 不需要設定：workflow 以
`https://` 加上 `PUBLIC_HOSTNAME` 組成，供 Slack 通知中的後台連結使用。
`DAILY_INSIGHTS_NEWS_FETCH_TIMEOUT_SECONDS`（25）與
`DAILY_INSIGHTS_NEWS_DISCOVERY_TIMEOUT_SECONDS`（30）目前不由 workflow 傳入，固定使用
Compose default。

`DAILY_INSIGHTS_YFINANCE_ENABLED` 控制 Yahoo Finance functions；
`DAILY_INSIGHTS_TWSE_ENABLED` 控制 TWSE 加權指數、個股法人與市場法人 functions。
這三個 TWSE functions 在同一 Provider context 共用 client 與全域請求間隔。
`DAILY_INSIGHTS_ANALYST_VIEWPOINTS_ENABLED` 控制 Internal Services 的
`analyst_viewpoints_sync`。所有 automatic functions 由每日 08:00 的統一 routine 建立，
不再使用個別 scheduler container。`DAILY_INSIGHTS_NEWSROOM_ENABLED` 同時控制常駐的
`newsroom-worker` 與 routine 中的 `newsroom_assemble`；旗標關閉時 worker 仍啟動並維持
健康，只是不處理任何新聞。金鑰或額度修復後的重新排入步驟見
[重點新聞維運手冊](news-recovery.md)。

資料功能停用時 API、worker 與 dispatcher 仍可維持健康，但對應 function 不執行
provider request。啟用晨報不需要額外設定 manifest 核准狀態或 hash。

`infra/production/env/remote.*.env` 只作為本機設定清單，已被 Git 忽略；workflow
不會讀取或上傳這些檔案。

## 3. GHCR 與 nginx image

[`release.yml`](../../.github/workflows/release.yml)：

1. 呼叫 [`ci.yml`](../../.github/workflows/ci.yml)；
2. 建置 `linux/amd64` API 與 Web images；
3. 發布至 `ghcr.io/fpi-tw/daily-insights-api` 與
   `ghcr.io/fpi-tw/daily-insights-web`；
4. 部署 build action 回傳的 immutable digest；
5. 使用短期 `GITHUB_TOKEN` 登入 GHCR，部署完成後 logout。

比照 FindB，nginx image 由
[`compose.production.yaml`](../../compose.production.yaml) 管理，不是 GitHub
Variable；Daily Insights 額外以 immutable digest 鎖定官方 nginx image。
nginx 設定由 CD 同步，Cloudflare private key 不會進入 image。

## 4. Cloudflare DNS 與 TLS

正式域名由 Cloudflare 管理：

1. 建立 proxied DNS record，將 `PUBLIC_HOSTNAME` 指向 EC2 public address；
2. SSL/TLS mode 使用 **Full (strict)**，不可使用 Flexible；
3. 在 Cloudflare Origin Server 建立涵蓋 `PUBLIC_HOSTNAME` 的 Origin CA
   certificate；
4. 將 certificate 與 private key 安裝為：

```text
/etc/daily-insights/tls/origin.crt  root:root 0644
/etc/daily-insights/tls/origin.key  root:root 0600
```

CD 每次從 Cloudflare 官方 IPv4/IPv6 endpoint 重新產生並驗證：

```text
/etc/daily-insights/cloudflare-realip.conf  root:root 0644
```

EC2 security group 的 443 僅允許 Cloudflare proxy CIDRs；SSH/22 僅允許核准
管理來源。Compose 只公開 443，不公開 API、Web 或 port 80。

## 5. EC2 檔案

CD 只安裝非敏感的部署資產與 TLS material：

```text
/opt/daily-insights/
├── compose.production.yaml
├── infra/production/nginx/
└── scripts/production/

/etc/daily-insights/
├── cloudflare-realip.conf
└── tls/
    ├── origin.crt
    └── origin.key
```

API/Web Secrets 不會寫入上述目錄。

## 6. Deployment lifecycle

Workflow 在 SSH process 中執行：

1. 驗證 GitHub Secrets、Variables、image digests 與 EC2 Docker 狀態；
2. 產生 Cloudflare real-IP allowlist；
3. SCP Compose、nginx 與 deployment scripts；
4. 安裝 root-owned host bundle；
5. 驗證 Origin CA certificate、private key 與 Cloudflare allowlist；
6. `docker compose config` 與 `docker compose pull`；
7. 用 disposable nginx container 渲染 template 並執行 `nginx -t`；
8. 在舊 API／Web 仍存活時，以 `--force-recreate --no-deps nginx` 單獨重建
   nginx，使 Docker DNS 動態解析先開始運作；
9. 停止 API、`orchestration-dispatcher`、`orchestration-worker`、
   `podcast-media-worker`、`newsroom-worker`，以及仍存在的所有 legacy
   scheduler／`data-management-worker`，並確認 quiescence，避免任何舊、新 runtime
   跨越 migration boundary；
10. 檢查 legacy management/report queues 沒有 pending 或 running work；若仍有工作，
    保留原始歷史並由 operator 明確處理後再部署；
11. 使用 API image 執行 `alembic upgrade head`；首次切換會把
    `data_management_runs` 搬入 `legacy_data_management_runs`；
12. 依序以 `--force-recreate --no-deps` 單獨啟動 `orchestration-worker`、
    `podcast-media-worker` 與 `newsroom-worker`，每個都等待 health check 通過；
13. 所有 worker healthy 後才 convergence API、Web 與 `orchestration-dispatcher`，且不
    再次重建 nginx；
14. 等待所有 container health，並從 nginx container 內分別主動驗證 API
    readiness 與 Web login route；
15. 輸出失敗 container state/logs，並從 GHCR logout。

若 migration、worker 啟動或 health、final convergence、final health 任一階段失敗，
deployment 會再次停止所有 schema-boundary services，並確認 legacy schedulers 與新
orchestration 都未執行；若 Docker 無法確認 quiescence，錯誤訊息會要求 operator 先
手動停止並確認。Operator 應依 diagnostics 修正問題後重新執行 `deploy.sh`。
Deployment 不做自動 downgrade 或 rollback；實際 application rollback 仍需另行協調
provider、orchestration 與資料版本。

nginx 以 Docker embedded DNS 重新解析 `api`／`web` service alias，TTL 為兩秒。
後端換址期間 deployment 會保持 pending；兩條 upstream probe 都成功前不得回報部署
完成。`scripts/test-production-nginx-dns.sh` 會在 CI 中以不重啟 nginx 的方式替換 mock
upstream，驗證換址後能在五秒內恢復。

第一次部署前執行：

```sh
make check-production-deployment
```

部署後可在不需要 Secrets 的情況下檢查既有 container：

```sh
docker ps --filter label=com.docker.compose.project=daily-insights-production
docker logs --tail=200 daily-insights-api
docker logs --tail=200 daily-insights-web
docker logs --tail=200 daily-insights-nginx
```

設定或 image rollback 透過重新執行指定版本的 GitHub workflow 完成；不在 EC2
保存包含 Secrets 的 rollback env，且 image rollback 不會回滾 database migration。

## 7. Go-live acceptance

正式切換 DNS 前仍需保存：

- deployment、migration、health 與 rollback transcript；
- customer/admin 登入、tenant isolation、會員/組織管理與三語系；
- Podcast publish/unpublish、R2 CORS/range playback 與 signed URL expiry；
- RDS backup/PITR 隔離還原結果；
- EC2 reboot 後 7 個 production container 由 Docker 自動恢復且通過 health check 的
  證據：`api`、`web`、`nginx`、`orchestration-worker`、`podcast-media-worker`、
  `newsroom-worker` 與 `orchestration-dispatcher`；另須證明 worker 先 healthy，
  dispatcher 才啟動；
- 下一個台北 08:00 只有一個 RoutineRun，包含七個 Provider function jobs（含
  `newsroom_daily_assemble`）、兩個 projection jobs、functions／attempts、實際
  provenance，以及 10:00 soft deadline 後的 terminal 狀態；
- 啟用重點新聞時，三個市場版在 08:00 後產生草稿、09:00 自動發布，Slack 收到
  「草稿完成」通知；
- 告警實際送達與目標流量的 CPU、memory、disk、database、latency headroom。

外部驗收完成前，狀態是「可部署，不可正式切流量」。
