"""Keep the Python ingestion catalog aligned with the API client's ordered catalog."""

import json
from pathlib import Path

from daily_insights_api.modules.data_sources.api import TRACKED_INDICES


def test_tracked_index_catalog_matches_the_api_client_contract() -> None:
    """CI runs this with the API test suite; ordering is part of the contract."""
    contract_path = (
        Path(__file__).resolve().parents[3]
        / "packages"
        / "api-client"
        / "src"
        / "tracked-index-catalog.json"
    )
    api_client_catalog = json.loads(contract_path.read_text(encoding="utf-8"))

    assert api_client_catalog == [
        {"symbol": symbol, "marketCode": market} for symbol, market in TRACKED_INDICES.items()
    ]
