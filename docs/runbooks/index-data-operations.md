# Index data operations

This runbook covers the Yahoo Finance daily-index data used only by the US and
Taiwan equity report-page charts. It does not change the public market API's
one-year default range. Historical data is read directly from
`index_daily_bars`; do not introduce a proxy or cache that substitutes another
instrument for a tracked index.

## Moving-average read contract

`GET /api/markets/indices/{symbol}/moving-averages` has the same authenticated
catalog visibility, `start`/`end` defaults, and ten-calendar-year limit as the
daily-bars endpoint. It calculates at query time from settled `close` values;
there is no materialized indicator table, cache, or scheduler work.

The response declares `method: "sma"`, `price_field: "close"`, and
`formula_version: "sma-close-v1"`, and always returns SMA periods 20, 60, 120,
and 240 in that order. Each requested trading session is present in each
series. A value includes that session's close and the preceding `period - 1`
settled sessions; it is `null` until sufficient sessions exist. Up to 239
earlier trading sessions are read solely as warm-up and are never exposed.
Weekend/holiday calendar rows are not filled. Numeric values are decimal
strings quantized to ten fractional places with half-even rounding. If the
requested window has no settled bars, all four point lists are empty and
`as_of` is `null`.

## Release gate

Before enabling the feature in production, confirm all of the following:

1. `DAILY_INSIGHTS_YFINANCE_ENABLED=true` is approved for the environment and the provider's
   current terms, permitted use, rate limits, and any required attribution have
   been reviewed by the product/legal owner. Record the decision in the release
   ticket; do not infer a license from a successful request.
2. The scheduler and its 08:00 Asia/Taipei run are deployed, and the admin-only
   **Index data** page is reachable by an administrator.
3. When a release adds a tracked symbol, deploy the API and scheduler image
   first, complete the historical backfill and validation below, and only then
   deploy the web image that exposes its chart. Do not enable the chart for
   customers first and backfill later.

## One-time two-year backfill

Run this once in the API scheduler environment after deploying a catalog
change, using the configured production database and provider credentials:

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

For the VIX chart rollout, explicitly verify that `^VIX` meets both checks
before deploying the web image. `^VIX` is the Cboe Volatility Index itself, not
the `VIXY` exchange-traded product used as a proxy in the morning report.

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
