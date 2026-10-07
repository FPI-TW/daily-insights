# Unified orchestration operations

The runtime has four durable layers: Provider, Function, Job, and Routine.
`orchestration-dispatcher` creates one `daily_market_update_v1` RoutineRun at 08:00
Asia/Taipei, including weekends and market holidays. A database uniqueness key
on routine/provider edition identity makes dispatcher restarts idempotent.

`orchestration-worker` executes functions under a process-wide provider lock.
Functions sharing Twelve Data, Yahoo Finance, or TWSE reuse the same provider
client; TWSE also retains one pacing context. Manual and automatic failures retry
5 minutes after a failed attempt, only for missing scopes. All functions and
projection jobs allow one initial
attempt plus at most three retries (four claims total), persisted across worker
restarts. The deadline may stop retries earlier. Expired leases consume an attempt;
exhausted runs become terminal with `retry_limit_reached`, retaining any partial
result and the provider failure details in FunctionAttempt history. Automatic
news market refreshes proceed independently when another market is waiting to
retry or has failed; provider locks still prevent concurrent provider access.
Manual news jobs retain their existing systemic-failure guard. Reconciliation
applies the same guard to an exhausted waiting refresh whose latest attempt failed,
including a fourth claim whose worker lease expired. Pending sibling markets become
terminal so publication can proceed; retained partial content remains available.
Local partial or unavailable outcomes do not stop healthy sibling refreshes.
Attempts persist a
bounded safe reason for every failed symbol, month, source, or model stage; operators must not infer a cause from
record counts alone. The 10:00 deadline is soft: no new automatic attempt
starts at or after the deadline, but an in-flight attempt may finish.
The Compose definitions start the worker with
`DAILY_INSIGHTS_RUNTIME_ROLE=orchestration-worker`; API containers do not run the
claim loop. The worker entry point does not independently reject a missing or
incorrect role, so operators must verify both the command and environment rather
than treating a heartbeat alone as proof of the least-privilege boundary.
Manual jobs use a one-hour soft deadline and the same five-minute interval and
three-retry limit (one initial attempt plus up to three retries). The deadline
may stop retries earlier without leaving the admin UI permanently pending. An
in-flight attempt may finish; after the deadline remaining retryable functions
become terminal and dependent degraded publications may proceed. Publication
functions and projection jobs that only become ready at the deadline
receive one initial attempt, but a failed publication attempt is not retried past
the deadline. Provider-specific HTTP refresh endpoints are removed, so operators start only the three market
jobs or the feature-specific news and analyst jobs.

Market reports, the Macro Dashboard, and news publish as soon as their upstream
dependencies become terminal; there is no approval gate for automatic or market
refresh publication. Reports and the dashboard freeze typed inputs and
provenance first and do not call external providers. A failed or partial refresh
does not remove previously persisted observations, including older fallback
observations within the 740-day history window. Publications keep the actual
source dates and attempt provenance; job results remain degraded when upstream
updates failed. Healthy sources continue to publish new observations while failed
sources retain their last successful values. If no usable report observations
remain after a failed refresh, publication is preserved rather than replaced with
an empty report. When raw fallback observations are absent and a failed refresh
would remove an available report block, the existing report is preserved. This
may defer healthy updates within that report until its missing inputs recover;
other markets continue updating. The Macro Dashboard restores missing failed-source
histories from its existing snapshot together with their original source references,
while healthy histories update. It preserves the entire snapshot when all sources
are unavailable. News refresh failures do not publish empty editions or
replace existing editions; zero-content editorial results remain distinct. News
refresh functions prepare candidate batches, so the automatic `news_publish` step only publishes
those prepared results after all three market refresh functions finish. The
admin “新聞候選與上架” workflow remains available and creates a separate manual
`news_publish_job`; that manual candidate path may refetch the selected article
and request missing localized summaries. Candidate batches remain visible even
when automatic publishing could not create an edition; the manual publish
request creates its unavailable base edition transactionally.

## Local development

`compose.yaml` starts the dispatcher and worker with orchestration enabled by
default. The two Compose interpolation values belong in the repository-root
`.env` (or the shell that starts Compose), not in `apps/api/.env`:

```dotenv
DAILY_INSIGHTS_ORCHESTRATION_ENABLED=true
DAILY_INSIGHTS_ORCHESTRATION_ACTIVATION_DATE=1970-01-01
```

The historical activation date is deliberate for local development: a fresh or
restarted dispatcher may create the current Taipei edition. Starting after 10:00
still creates the RoutineRun for observability, but automatic provider attempts
past the soft deadline remain terminal; manual admin jobs remain runnable.

Use these checks without resetting the PostgreSQL volume:

```sh
docker compose ps
docker compose logs --tail=200 orchestration-worker
docker compose logs --tail=200 orchestration-dispatcher
```

Every isolated orchestration process must import the complete ORM model registry
before configuring mappers or creating rows. In particular, dispatcher startup
must register `users` because `job_runs.requested_by_user_id` has a foreign-key
relationship to that table.

## Verification harness and evidence limits

The following checks use repository fixtures, an isolated test database, and
placeholder or mocked provider values; they do not require production secrets:

```sh
uv run --project apps/api pytest -c apps/api/pyproject.toml \
  apps/api/tests/test_orchestration_registry.py
make check-production-deployment
pnpm format:check:docs
```

Use `make test-db` when database integration coverage is required; it creates a
temporary PostgreSQL container and must never target a shared or production
database. Pass the exact commands, non-secret environment values, and any known
limitations to an independent reviewer. In particular, these offline checks do
not prove provider credentials, licensing, live endpoint freshness, production
network reachability, or an authenticated admin browser session. A local start
after 10:00 also cannot demonstrate same-day automatic provider execution by
design; use a manual current-edition job or a controlled pre-deadline test.

## Deployment cutover

1. Deploy after 10:00 Asia/Taipei.
2. Set `DAILY_INSIGHTS_ORCHESTRATION_ENABLED=true` and set
   `DAILY_INSIGHTS_ORCHESTRATION_ACTIVATION_DATE` to the next Taipei date.
3. The deploy script detects whether `routine_runs` exists. For the first
   cutover it stops and verifies the API, every legacy scheduler, the legacy
   `data-management-worker`, and any orchestration worker before migration
   `20260916_0028`. It then refuses migration while either legacy management or
   report queue still has a pending/running row; resolve that work deliberately
   before retrying so its original history remains intact.
4. Run the migration. It renames `data_management_runs` to
   `legacy_data_management_runs`; the history remains available only through
   `GET /api/admin/orchestration/legacy-runs`.
5. Start `orchestration-worker` and wait for health, then start the dispatcher.
6. On the next day, verify one RoutineRun, eight jobs (six provider jobs and two
   projection jobs), provider function attempts, projection publications, and
   the 10:00 terminal state.

Do not run a legacy scheduler after migration. Recovery is forward-only while
new orchestration or typed-fact rows exist.

Later releases are steady-state deployments: keep the original activation date
unchanged (it must not be in the future). They may run before 10:00, but the
script still stops the API, dispatcher, and orchestration worker before every
schema migration and restores them only after migration succeeds.
If migration succeeded but no RoutineRun exists yet, the installation is
activation-pending: a same-day retry may run at any hour with activation set to
today or the next Taipei date.

## Retry-limit rollout

Apply migration `20261005_0031` before starting the updated worker. It adds
`job_runs.attempt_count` for durable projection claims. Already-started projection
jobs are backfilled to one attempt because historical projection attempt counts
were not recorded; new claims are counted exactly. FunctionRun attempt counts
already exist and are retained, so exhausted waiting functions are reconciled
without another provider request.

## Twelve Data 已完成日線契約

契約 `2026-10-07.v9` 在所有 Twelve Data 日線入口（自動、手動 completed-price
與通用歷史讀取）按供應商原始回應順序，對同日保留最後一筆完整 row。這是本專案
採用的確定性政策，不代表供應商保證最後一筆是正確或最新修訂。

completed-price 路徑以官方 `/eod` 日期與 exact Decimal close
為錨點，`/time_series` 維持 `order=ASC`、`dp=11`，至少請求 4 筆原始資料，
最大 `outputsize` 仍為 5000；不足兩個不同的已完成日期仍拒收，不補值或改取未收盤資料。

先驗證完整回應 schema、symbol、interval、currency 與預期 asset type，以及全部
原始日期沒有倒序，再排除 EOD 日期之後的日線。未來資料即使不會採用，若 schema
異常仍拒收。所有保留候選均須符合有限 OHLC、非負 optional volume，以及
`low <= open/close <= high`，包含最後不被選取的衝突候選；不另加通用價格正值限制。

同日無論兩筆、三筆、完全相同或 OHLC／nullable volume 不同，都採最後完整 row，
不混用欄位、不排序修補、不跳過非法候選。`None` 與 `0` 保留各自原值。完成去重後，
最新已完成日的日期與最後 row 的 exact Decimal close 必須符合 `/eod`；只有較前
row 符合時仍拒收，不改選該 row，也不以 warning 放行。EOD 僅佐證日期與 close，
OHL／volume 仍是通過範圍驗證的供應商資料。OHLC 範圍檢查維持原作用範圍：
completed-price 的未來 row 不檢查範圍，但完整 schema 與原始日期順序仍須有效；
通用歷史讀取的所有 raw row 都須通過 OHLC 範圍檢查。

通用 `get_daily_bars` 未指定 `minimum_items` 時，原始回應筆數仍須達到實際
`outputsize`，允許去重後略少（例如 400 筆原始 row 得到 399 個日期），不補抓。
`outputsize >= 2` 至少須有兩個不同日期，`outputsize=1` 保留單日支援。
明確指定的 `minimum_items` 須在 1 到 `outputsize` 之間，並按接受的不同日期數
檢查。completed-price 一律至少需要兩個不同已完成日期。

每次通過 schema、metadata 與原始日期順序檢查的重複回應，記錄一則彙總 warning：
symbol、重複日期數、移除 row 數及至多十個日期樣本，不記錄憑證或完整回應。
彙總涵蓋全部原始日期，包括 EOD 之後被排除的日期；移除 row 數指同日多餘筆數，
不包含 EOD cutoff 排除的筆數。warning 在 OHLC／有效筆數／EOD 最終核對之前
記錄，因此有 warning 不代表回應已被接受。

Provenance 的 response digest 保留完整原始回應，query fingerprint 對應實際
請求；record count 與 as-of 則對應接受的唯一日線（completed-price 僅含已完成日期）。
新來源使用 v9 契約；既有 `MarketDailySeries.contract_version` 是初建標記，不回寫。
已存日期的行情修訂以新的 `MarketDailyObservation.version` 保存，缺少日期新增
version 1，重複更新相同 OHLC／volume 不新增版本；attempt metadata 與 dashboard
source references 追蹤實際來源。重試維持五分鐘間隔、最多四次嘗試。EOD 不符、
schema／metadata 異常、日期倒序或有效日期不足時，應調查回應，不能以 warning
取代拒收。

## Data management display

Data Management shows localized status icons at the routine, job, function, and
attempt levels. Refresh jobs (`function`) and publication jobs (`projection`)
have distinct labels and icons. The publication-only `news_publish_job` also
uses the publication label despite its function kind; mixed refresh jobs retain
the refresh label. Displayed execution times use Asia/Taipei.
The daily routine section includes the catalog's
Taipei date and preceding four calendar days, newest edition first; future dates
and older routines are excluded from the fetched routine records.

Latest results retain native pagination. Jobs are grouped only by a shared
`routine_run_id` or explicit `depends_on` / `downstream_jobs` links; separate
manual executions with the same job key and edition remain separate. Within each
page-local group, dependency precedence determines the displayed steps, then
start time (or queue time for pending jobs) breaks ties. This is a display order;
independent jobs may run concurrently, and scheduler execution is unchanged.
Linked job IDs absent from the page are disclosed rather than fetched or inferred.
Step numbers restart within each group and page. A thicker outer outline marks
each task group; individual steps retain their thin borders. Native details, errors, and
active-job cancellation remain available.
