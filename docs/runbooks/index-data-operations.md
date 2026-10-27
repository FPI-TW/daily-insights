# Index data operations

This runbook covers the daily-index data (Yahoo Finance, plus TWSE for
`^TWII`) used only by the US, Hong Kong and Taiwan equity report pages. It
does not change the public market API's one-year default range. Historical data is read directly from
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
`as_of` is `null`. The same response also carries `rsi` (Wilder, 14), `macd`
(EMA 12/26/9) and `kd` (9-3-3, from high/low/close) series for the same
sessions.

## Release gate

Before enabling the feature in production, confirm all of the following:

1. `DAILY_INSIGHTS_YFINANCE_ENABLED=true` is approved for the environment and the provider's
   current terms, permitted use, rate limits, and any required attribution have
   been reviewed by the product/legal owner. Record the decision in the release
   ticket; do not infer a license from a successful request.
   `DAILY_INSIGHTS_TWSE_ENABLED=true` is a separate gate for ^TWII, which the
   exchange supplies rather than Yahoo. It is refreshed by the TWSE run
   alongside the institutional flows, not by this scheduler, because the two
   share one client and one request interval. Leaving it false stops ^TWII and
   the flows together and leaves the ten Yahoo symbols unaffected.
2. The orchestration dispatcher and worker, with their 08:00 Asia/Taipei daily
   routine, are deployed, and the admin-only **Data management** page is
   reachable by an administrator.
3. When a release adds a tracked symbol, deploy the API and scheduler image
   first, complete the historical backfill and validation below, and only then
   deploy the web image that exposes its chart. Do not enable the chart for
   customers first and backfill later.

## One-time 25-month backfill

Run this once in the API scheduler environment after deploying a catalog
change, using the configured production database and provider credentials:

```sh
# Ten international indices from Yahoo:
python -m daily_insights_api.scripts.run_index_daily_bars --once --period 2y
```

The operation uses upserts and is safe to rerun after a provider interruption.
It must not be used to bypass provider terms or rate limits.

^TWII needs no manual backfill: the daily `twse_daily_update` job refreshes the
newest two months first, then keeps requesting any missing month in the
required 25-month window, so a partially successful run does not permanently
truncate chart history. The TWSE institutional datasets have no historical
backfill tool; the daily job stores only the latest published trading day, so
their history accumulates from the first scheduled run. The former
`run_institutional_flows --once` command was removed because it queued work for
the retired data-management worker and left a pending legacy run that blocks
the next deployment's legacy-queue check.

Validate each settled tracked series after the run. The database must contain at
least 450 bars per symbol, and the newest stored `trade_date` must be within
seven calendar days of the validation date in Asia/Taipei. Investigate market
holidays before declaring a failure. A rerun should leave the same date/symbol
keys in place (corrected close values may legitimately update).

For `^TWII`, additionally confirm every calendar month in the trailing
25-month window has at least one settled row and every row reports provider
`twse`; no `yfinance` row may remain. For the VIX chart rollout, explicitly
verify that `^VIX` meets both checks before deploying the web image. `^VIX` is
the Cboe Volatility Index itself.

## Routine and manual refresh

The daily routine fetches a trailing `7d` window, widened to cover the gap
after the newest stored bar when that bar is older than a week, and a two-year
bootstrap for a symbol with no stored bars. Administrators may use the Data
management page to enqueue the matching market refresh job
(`us_equity_refresh`, `hk_equity_refresh` or `tw_equity_refresh`). A run can be
partial: successful symbols are committed while failed symbols are reported for
retry.

If a run reports `yfinance_disabled`, check the feature flag and release gate.
If it times out, use the provider-specific command above, then revalidate. TWII
months are committed independently, so a timed-out or partial TWSE run may
already have durable progress. Preserve audit events when investigating who
initiated a refresh and which symbols failed.
