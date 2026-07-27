# EC2 首次部署操作手冊

本手冊把 repository 內可自動化的首次上線流程串起來。預設區域為新加坡
`ap-southeast-1`，EC2 僅執行 nginx、Web 與 API；PostgreSQL 使用私有 RDS，
Podcast 檔案使用私有 Cloudflare R2。

執行本手冊前，先完成 [`production.md`](production.md) 的網路、備份、告警與
容量決策。這些步驟會產生 AWS/Cloudflare 費用，因此 repository 不會自動建立
外部資源。

## 1. 建立 AWS 與 GitHub 邊界

1. 建立兩個 private ECR repositories（API、Web），啟用 tag immutability、
   enhanced scanning，並設定已核准的 lifecycle policy。
2. 在 AWS IAM 建立 GitHub OIDC provider 與 release role。以
   [`github-release-trust-policy.json`](../../infra/aws/iam/github-release-trust-policy.json)
   和
   [`github-release-policy.json`](../../infra/aws/iam/github-release-policy.json)
   為最小權限起點，替換所有大寫 placeholder。Trust policy 必須限制到本
   repository 的 GitHub `production` environment。
3. GitHub `production` environment 啟用必要 reviewer，並設定：
   `AWS_ACCOUNT_ID`、`AWS_REGION`、`AWS_RELEASE_ROLE_ARN`、
   `ECR_API_REPOSITORY`、`ECR_WEB_REPOSITORY`、`PUBLIC_HOSTNAME`、
   `NGINX_IMAGE`。`NGINX_IMAGE` 必須是已審核的 multi-architecture
   `image@sha256:digest`，不可使用 tag。
4. EC2 instance role 附加 AWS managed
   `AmazonSSMManagedInstanceCore`，並以
   [`ec2-application-policy.json`](../../infra/aws/iam/ec2-application-policy.json)
   限制 ECR pull、指定 SSM path 與 KMS key。不要建立 IAM user access key。

GitHub Actions 透過 OIDC 取得短期 AWS 憑證；設定方式以
[GitHub 官方 AWS OIDC 文件](https://docs.github.com/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws)
及
[AWS credentials action](https://github.com/aws-actions/configure-aws-credentials)
為準。SSM 敏感值使用 KMS `SecureString`，詳見
[AWS Parameter Store 文件](https://docs.aws.amazon.com/systems-manager/latest/userguide/systems-manager-parameter-store.html)。

## 2. 建立網路與資料服務

- EC2 可使用 Amazon Linux 2023，先選 x86_64 或 Graviton；release workflow
  會發布 `linux/amd64` 與 `linux/arm64` 映像。
- 不開放 SSH/22，管理連線使用
  [AWS Systems Manager Session Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager.html)。
- EC2 security group 的 443 僅允許目前 Cloudflare proxy CIDR。API 8000、
  Web 3000 與 Docker network 不對外發布。
- RDS 使用私有 subnet、無 public address；5432 僅允許 EC2 application
  security group。啟用 encryption、deletion protection、至少 14 天 automated
  backup 與 point-in-time recovery。
- 建立 CloudWatch 告警與已驗證的通知目的地。至少包含 EC2 status check、
  CPU、記憶體、磁碟、程序健康，以及 RDS CPU、storage、connections 與備份。

## 3. 建立 SSM runtime 參數

在 `/daily-insights/production/api/` 下建立以下直接子項。敏感值一律使用
customer-managed KMS key 的 `SecureString`；非敏感設定也可放在同一路徑，
讓 runtime env 只由單一受控來源產生。

```text
DAILY_INSIGHTS_ENVIRONMENT
DAILY_INSIGHTS_DATABASE_URL
DAILY_INSIGHTS_SESSION_SECRET
DAILY_INSIGHTS_PASSWORD_PEPPER
DAILY_INSIGHTS_TRUSTED_PROXY_CIDRS
DAILY_INSIGHTS_FINDB_BASE_URL
DAILY_INSIGHTS_FINDB_API_KEY
DAILY_INSIGHTS_R2_ENDPOINT_URL
DAILY_INSIGHTS_R2_BUCKET_NAME
DAILY_INSIGHTS_R2_ACCESS_KEY_ID
DAILY_INSIGHTS_R2_SECRET_ACCESS_KEY
DAILY_INSIGHTS_R2_SIGNED_URL_TTL_SECONDS  # optional
```

固定值：

```text
DAILY_INSIGHTS_ENVIRONMENT=production
DAILY_INSIGHTS_TRUSTED_PROXY_CIDRS=172.30.0.0/24
```

Secret 與 env value 必須是單行、無空白或 shell quoting 字元。Session secret
與 password pepper 各至少 32 字元且不得相同。Database URL 使用
`postgresql+psycopg://...?...sslmode=require`。

## 4. 安裝 EC2 host bundle

Host 需預先安裝並啟用 Docker Engine、Docker Compose v2、AWS CLI v2、
Python 3、OpenSSL 與 curl。Docker 安裝與更新以
[Docker Engine RHEL 文件](https://docs.docker.com/engine/install/rhel/)及
[Compose plugin 文件](https://docs.docker.com/compose/install/linux/)為準；
不要在 production 使用未固定版本的 convenience script。

先在 CI 對欲部署的 commit 執行：

```sh
make check-production-deployment
```

將同一 commit 的 repository bundle 經核准的 artifact 管道傳到 EC2，核對
SHA-256 後執行：

```sh
sudo ./scripts/production/install-host-bundle.sh "$PWD"
sudo cp /etc/daily-insights/ssm.env.example /etc/daily-insights/ssm.env
sudo chown root:root /etc/daily-insights/ssm.env
sudo chmod 0644 /etc/daily-insights/ssm.env
```

安裝 Cloudflare origin certificate：

```text
/etc/daily-insights/tls/origin.crt  root:root 0644
/etc/daily-insights/tls/origin.key  root:root 0600
```

取得並驗證 Cloudflare 官方 CIDR：

```sh
sudo /opt/daily-insights/scripts/production/update-cloudflare-realip.sh
```

更新 allowlist 後必須 restart `daily-insights.service` 才會套用新的 bind mount。

## 5. 發布映像與首次部署

從 GitHub Actions 手動執行 `Publish production images`。Production
environment approval 通過後，workflow 會：

1. 透過 OIDC 登入 AWS；
2. 建置及推送 API/Web 的 amd64、arm64 映像；
3. 以 registry 回傳的 digest 產生 `release.env`；
4. 上傳 `production-release-<commit>` artifact。

EC2 的 deploy 與 systemd 啟動流程會透過 instance role 取得短期 ECR login
token；不要把 registry password 寫進 SSM、release manifest 或 Dockerfile。

下載 artifact、核對 workflow commit 與 artifact digest，再透過核准的
artifact 管道放到 EC2，例如 `/var/tmp/daily-insights/release.env`。首次執行：

```sh
sudo /opt/daily-insights/scripts/production/validate-release.sh \
  /var/tmp/daily-insights/release.env
sudo /opt/daily-insights/scripts/production/materialize-runtime-env.sh \
  /etc/daily-insights/ssm.env \
  /var/tmp/daily-insights/release.env
sudo /opt/daily-insights/scripts/production/preflight.sh \
  /var/tmp/daily-insights/release.env
sudo /opt/daily-insights/scripts/production/deploy.sh \
  /var/tmp/daily-insights/release.env
```

後續 release 不要先覆蓋 `current.env`；直接把新的 candidate path 傳給
`deploy.sh`，才能保留上一版並自動 rollback。

## 6. Go-live 驗收

正式切換 Cloudflare DNS 前，至少保留以下證據：

- `systemctl status daily-insights.service` 與三個 container health；
- migration、deploy、health 與 rollback rehearsal transcript；
- customer/admin 登入、tenant isolation、三語系、會員/組織管理與 Podcast
  publish/unpublish；
- R2 CORS/range playback、signed URL expiry、missing object 與無效憑證；
- RDS automated backup 狀態與隔離還原演練；
- EC2 reboot/replacement recovery、CloudWatch alarm 實際送達；
- 低於 1,000 concurrent users 的量測報告與資源 headroom；
- security group、IAM、KMS、SSM 參數清冊及 rotation owner。

在上述外部驗收完成前，部署狀態是「可上機、不可切流量」。不要刪除 legacy
資料，也不要把 RDS、API、Web 或 R2 bucket 改成 public。
