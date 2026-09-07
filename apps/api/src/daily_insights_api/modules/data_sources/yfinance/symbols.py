"""The indices this project tracks on Yahoo Finance.

Probed on 2026-09-03 with yfinance 1.7.0. Dropped symbols and why:

- `HSTECH.HK`, `399006.SZ`, `399106.SZ`, `^TELI`: Yahoo has no history for them
  at all, only the current session's still-open bar, which is useless without a
  settled close.
- `000300.SS` (CSI 300): Yahoo's settled series stops at 2026-07-17 and then
  jumps straight to today's open bar, roughly seven weeks of missing sessions.
"""

from typing import Literal

from daily_insights_api.modules.data_sources.dto import MarketCode

# The tracked set is a type, not a runtime check, so it reaches the request
# schema and the generated client as an enum instead of a bare string. mypy
# rejects a TRACKED_INDICES key that is not a member; the reverse direction is
# covered by a test.
IndexSymbol = Literal[
    "^DJI",
    "^GSPC",
    "^NDX",
    "^RUT",
    "^SOX",
    "^HSI",
    "^TWII",
    "000001.SS",
]

TRACKED_INDICES: dict[IndexSymbol, MarketCode] = {
    "^DJI": "us_equity",
    "^GSPC": "us_equity",
    "^NDX": "us_equity",
    "^RUT": "us_equity",
    "^SOX": "us_equity",
    "^HSI": "hk_equity",
    "^TWII": "tw_equity",
    "000001.SS": "cn_equity",
}
