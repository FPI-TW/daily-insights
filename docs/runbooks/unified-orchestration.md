# Unified orchestration operations

The runtime has four durable layers: Provider, Function, Job, and Routine.
`orchestration-dispatcher` creates one `daily_market_update_v1` RoutineRun at 08:00
Asia/Taipei, including weekends and market holidays. A database uniqueness key
on routine/provider edition identity makes dispatcher restarts idempotent.

`orchestration-worker` executes functions under a process-wide provider lock.
Functions sharing Twelve Data, Yahoo Finance, or TWSE reuse the same provider
client; TWSE also retains one pacing context. Failures retry every 30 minutes,
only for missing scopes. The 10:00 deadline is soft: no new automatic attempt
starts at or after the deadline, but an in-flight attempt may finish.
Manual jobs have no automatic deadline and keep retrying until they complete or
an operator cancels them. Provider-specific
HTTP refresh endpoints are removed, so operators start only the three market
jobs or the feature-specific news and analyst jobs.

Market reports, the Macro Dashboard, and news publish as soon as their upstream
dependencies become terminal. They do not call external providers. Reports and
the dashboard freeze typed inputs and provenance first; news refresh functions
prepare candidate batches, and `news_publish` publishes after all three market
refresh functions finish. The admin “新聞候選與上架” workflow remains available
and creates a separate manual `news_publish_job`. Candidate batches remain
visible even when automatic publishing could not create an edition; the manual
publish request creates its unavailable base edition transactionally.

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
