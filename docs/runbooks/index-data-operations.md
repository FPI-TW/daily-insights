# Index data operations

This runbook covers the Yahoo Finance daily-index data used only by the US and
Taiwan equity report-page charts. It does not change the public market API's
one-year default range. Historical data is read directly from
`index_daily_bars`; do not introduce a proxy or cache that substitutes another
instrument for a tracked index.

## Release gate

Before enabling the feature in production, confirm all of the following:

1. `DAILY_INSIGHTS_YFINANCE_ENABLED=true` is approved for the environment and the provider's
   current terms, permitted use, rate limits, and any required attribution have
   been reviewed by the product/legal owner. Record the decision in the release
   ticket; do not infer a license from a successful request.
2. The scheduler and its 08:00 Asia/Taipei run are deployed, and the admin-only
   **Index data** page is reachable by an administrator.
3. The initial historical backfill has completed and its validation below has
   passed. Do not enable the chart for customers first and backfill later.

## One-time two-year backfill

Run this once in the API scheduler environment, using the configured production
database and provider credentials:

```sh
python -m daily_insights_api.scripts.run_index_daily_bars --once --period 2y
```

The operation uses upserts and is safe to rerun after a provider interruption.
It must not be used to bypass the provider's licensed use or rate limits.

Validate each settled tracked series after the run. The database must contain at
least 450 bars per symbol, and the newest stored `trade_date` must be within
seven calendar days of the validation date in Asia/Taipei. Investigate market
holidays before declaring a failure. A rerun should leave the same date/symbol
keys in place (corrected close values may legitimately update).

## Routine and manual refresh

The scheduler fetches the fixed trailing `7d` window. Administrators may use
the Index data page to issue the same refresh; the page sends one request at a
time and imposes a one-second cooldown. A response can be partial: successful
symbols are committed and visible in the result table while failed symbols are
reported for retry.

If the API returns 503, check the feature flag and release gate. If it returns
504, no endpoint transaction was committed; use the scheduler command above
for a large backfill, then revalidate. Preserve audit events when investigating
who initiated a refresh and which symbols failed.
