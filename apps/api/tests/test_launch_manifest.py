from datetime import date, timedelta
from decimal import ROUND_UP, Decimal, localcontext

import pytest

from daily_insights_api.modules.data_sources.api import DailyBar, DataSourceContractError
from daily_insights_api.modules.reports.contracts import MetricBlock, TableCell, TableColumn
from daily_insights_api.modules.reports.launch_manifest import (
    ACTIVE_LAUNCH_MANIFEST,
    LAUNCH_MARKET_ORDER,
    LaunchManifest,
)
from daily_insights_api.modules.reports.morning_report import (
    MORNING_REPORT_DERIVATION_VERSION,
    DatasetBuild,
    _bundle,
    _error_blocks,
    _error_blocks_for_dataset,
    _input_digest,
    _latest_common_provider_dates,
    _market_datasets,
    _next_revision,
    _normalized_common_date_points,
    _normalized_points,
    _quantize,
    _revision_lock_key,
    _validate_dataset_contract,
    _validate_manifest_output,
    block_precision,
    block_rounding,
)


def test_manifest_freezes_three_markets_and_block_order() -> None:
    assert MORNING_REPORT_DERIVATION_VERSION == "twelve-data.three-market.v5"
    assert ACTIVE_LAUNCH_MANIFEST.version == "three-market.v5"
    assert tuple(market.market_code for market in ACTIVE_LAUNCH_MANIFEST.markets) == (
        "global_macro_bonds",
        "crypto",
        "us_equity",
    )
    assert tuple(market.market_code for market in ACTIVE_LAUNCH_MANIFEST.markets) == (
        LAUNCH_MARKET_ORDER
    )
    assert [block.id for market in ACTIVE_LAUNCH_MANIFEST.markets for block in market.blocks] == [
        "macro.commodities",
        "macro.commodity_normalized_performance",
        "crypto.overview",
        "crypto.normalized_performance",
        "us.index_proxies",
        "us.mega_caps",
    ]
    assert {
        block.rounding for market in ACTIVE_LAUNCH_MANIFEST.markets for block in market.blocks
    } == {"ROUND_HALF_EVEN"}
    assert block_precision("crypto.overview") == 4
    assert block_precision("us.mega_caps") == 2
    assert block_rounding("crypto.overview") == "ROUND_HALF_EVEN"


def test_manifest_hash_is_stable_and_changes_with_content() -> None:
    round_trip = LaunchManifest.model_validate(ACTIVE_LAUNCH_MANIFEST.model_dump(mode="json"))
    assert round_trip.sha256 == ACTIVE_LAUNCH_MANIFEST.sha256
    changed = round_trip.model_copy(update={"version": "three-market.v6"})
    assert changed.sha256 != round_trip.sha256


def test_manifest_keeps_atomic_dataset_contracts() -> None:
    assert {dataset.atomicity for dataset in ACTIVE_LAUNCH_MANIFEST.datasets} == {"all_or_error"}
    assert next(
        dataset.symbol_units
        for dataset in ACTIVE_LAUNCH_MANIFEST.datasets
        if dataset.key == "macro.commodity_quotes"
    ) == {"XBR/USD": "USD", "XAU/USD": "USD", "HG1": "USD"}
    quotes = next(
        dataset
        for dataset in ACTIVE_LAUNCH_MANIFEST.datasets
        if dataset.key == "macro.commodity_quotes"
    )
    # HG1 without a type resolves to a German stock; the manifest pins copper.
    assert quotes.symbol_types == {"HG1": "commodity"}
    assert "previous_close" in quotes.required_fields
    history = next(
        dataset
        for dataset in ACTIVE_LAUNCH_MANIFEST.datasets
        if dataset.key == "macro.commodity_daily_bars"
    )
    assert history.endpoint == "/time_series"
    assert history.symbols == ("XBR/USD", "XAU/USD")
    assert history.minimum_history == 500
    assert history.expected_asset_types == {
        "XBR/USD": "Energy Resource",
        "XAU/USD": "Precious Metal",
    }
    assert tuple(dataset.key for dataset in _market_datasets("global_macro_bonds")) == (
        "macro.commodity_quotes",
        "macro.commodity_daily_bars",
    )


def test_us_equity_uses_fixed_usd_baskets_instead_of_provider_movers() -> None:
    keys = {dataset.key: dataset for dataset in ACTIVE_LAUNCH_MANIFEST.datasets}
    assert "us.market_movers" not in keys
    proxies = keys["us.index_proxy_quotes"]
    mega_caps = keys["us.mega_cap_quotes"]
    assert proxies.endpoint == mega_caps.endpoint == "/quote"
    assert proxies.symbols == ("SPY", "QQQ", "DIA", "IWM", "VIXY")
    assert len(mega_caps.symbols) == 8 and "NVDA" in mega_caps.symbols
    assert set(proxies.symbol_units.values()) == set(mega_caps.symbol_units.values()) == {"USD"}
    assert tuple(dataset.key for dataset in _market_datasets("us_equity")) == (
        "us.index_proxy_quotes",
        "us.mega_cap_quotes",
    )
    formulas = [
        block.formula
        for market in ACTIVE_LAUNCH_MANIFEST.markets
        for block in market.blocks
        if block.id.startswith("us.")
    ]
    assert all("previous_close" in formula for formula in formulas)
    assert not any("as returned by provider" in formula for formula in formulas)


def test_manifest_rejects_symbol_types_outside_the_dataset() -> None:
    payload = ACTIVE_LAUNCH_MANIFEST.model_dump(mode="json")
    next(dataset for dataset in payload["datasets"] if dataset["key"] == "macro.commodity_quotes")[
        "symbol_types"
    ] = {"ZZZ": "commodity"}

    with pytest.raises(ValueError, match="symbol type overrides"):
        LaunchManifest.model_validate(payload)


def test_manifest_allows_multiple_datasets_for_one_market() -> None:
    assert LaunchManifest.model_validate(ACTIVE_LAUNCH_MANIFEST.model_dump(mode="json"))


def test_manifest_rejects_blocks_with_multiple_dataset_references() -> None:
    payload = ACTIVE_LAUNCH_MANIFEST.model_dump(mode="json")
    payload["markets"][0]["blocks"][0]["datasets"] = (
        "macro.commodity_quotes",
        "macro.commodity_daily_bars",
    )

    with pytest.raises(ValueError, match="every block must reference exactly one dataset"):
        LaunchManifest.model_validate(payload)


def test_manifest_rejects_dataset_reused_by_another_market() -> None:
    payload = ACTIVE_LAUNCH_MANIFEST.model_dump(mode="json")
    payload["markets"][1]["blocks"][0]["datasets"] = ("macro.commodity_quotes",)

    with pytest.raises(ValueError, match="every dataset must be referenced by exactly one market"):
        LaunchManifest.model_validate(payload)


def test_manifest_rejects_duplicate_and_unreferenced_datasets() -> None:
    duplicate = ACTIVE_LAUNCH_MANIFEST.model_dump(mode="json")
    duplicate["datasets"] = (*duplicate["datasets"], duplicate["datasets"][0])
    with pytest.raises(ValueError, match="dataset keys must be unique"):
        LaunchManifest.model_validate(duplicate)

    unreferenced = ACTIVE_LAUNCH_MANIFEST.model_dump(mode="json")
    unreferenced["markets"][0]["blocks"][1]["datasets"] = ("macro.commodity_quotes",)
    with pytest.raises(ValueError, match="exactly cover declared datasets"):
        LaunchManifest.model_validate(unreferenced)


def test_manifest_rejects_partial_asset_type_contract() -> None:
    payload = ACTIVE_LAUNCH_MANIFEST.model_dump(mode="json")
    next(
        dataset for dataset in payload["datasets"] if dataset["key"] == "macro.commodity_daily_bars"
    )["expected_asset_types"] = {"XBR/USD": "Energy Resource"}

    with pytest.raises(ValueError, match="asset-type contracts"):
        LaunchManifest.model_validate(payload)


def test_dataset_input_digest_is_stable_across_dataset_order() -> None:
    quotes, history = _market_datasets("global_macro_bonds")
    quote_failure = DatasetBuild(quotes, (), None, ValueError("quote failed"))
    history_failure = DatasetBuild(history, (), None, ValueError("history failed"))

    assert _input_digest(
        MORNING_REPORT_DERIVATION_VERSION, (quote_failure, history_failure)
    ) == _input_digest(MORNING_REPORT_DERIVATION_VERSION, (history_failure, quote_failure))


def test_macro_dataset_failure_preserves_the_other_block_and_status() -> None:
    as_of = date(2026, 8, 30)
    quote_block = MetricBlock(
        id="macro.commodities",
        status="ok",
        source_as_of=as_of,
        metrics=(),
    )
    partial_blocks = (
        quote_block,
        *_error_blocks_for_dataset("global_macro_bonds", "macro.commodity_daily_bars"),
    )
    _validate_manifest_output("global_macro_bonds", partial_blocks)

    partial = _bundle("global_macro_bonds", partial_blocks)
    unavailable = _bundle("global_macro_bonds", _error_blocks("global_macro_bonds"))

    assert partial.content.status == "partial"
    assert partial.content.as_of == as_of
    assert unavailable.content.status == "unavailable"
    assert unavailable.content.as_of is None


def test_runtime_rejects_required_fields_outside_the_adapter_contract() -> None:
    dataset = ACTIVE_LAUNCH_MANIFEST.datasets[0].model_copy(
        update={"required_fields": ("close", "unsupported")}
    )

    with pytest.raises(DataSourceContractError, match="unsupported fields: unsupported"):
        _validate_dataset_contract(dataset)


def test_runtime_enforces_manifest_block_order_and_atomic_status() -> None:
    blocks = _error_blocks("crypto")
    _validate_manifest_output("crypto", blocks)

    with pytest.raises(DataSourceContractError, match="exactly match manifest order"):
        _validate_manifest_output("crypto", tuple(reversed(blocks)))

    wrong_kind = (
        blocks[0].model_copy(update={"kind": "series"}),
        blocks[1],
    )
    with pytest.raises(DataSourceContractError, match="block kinds"):
        _validate_manifest_output("crypto", wrong_kind)

    mixed = (
        blocks[0].model_copy(update={"status": "ok"}),
        blocks[1],
    )
    with pytest.raises(DataSourceContractError, match="mixed block statuses"):
        _validate_manifest_output("crypto", mixed)

    excessive_precision = blocks[0].model_copy(
        update={
            "columns": (TableColumn(id="asset"),),
            "rows": ((TableCell(value=Decimal("1.00001")),),),
        }
    )
    with pytest.raises(DataSourceContractError, match="exceeds manifest precision"):
        _validate_manifest_output("crypto", (excessive_precision, blocks[1]))


def test_normalized_performance_uses_first_close_inside_thirty_day_window() -> None:
    bars = tuple(
        DailyBar(
            instrument_source_id="BTC/USD",
            market="crypto",
            symbol="BTC/USD",
            trade_date=date(2026, 7, 1) + timedelta(days=index),
            close=Decimal(index + 1),
            volume=1,
        )
        for index in range(31)
    )
    points = _normalized_points(bars)
    assert len(points) == 30
    assert points[0].value == Decimal(100)
    assert points[-1].value == Decimal(1550)


def test_macro_normalization_uses_latest_thirty_exact_common_provider_dates() -> None:
    start = date(2026, 1, 1)
    brent = tuple(
        DailyBar(
            instrument_source_id="XBR/USD",
            market="global_macro_bonds",
            symbol="XBR/USD",
            trade_date=start + timedelta(days=index),
            close=Decimal(index + 1),
        )
        for index in range(35)
    )
    gold = tuple(
        DailyBar(
            instrument_source_id="XAU/USD",
            market="global_macro_bonds",
            symbol="XAU/USD",
            trade_date=start + timedelta(days=index),
            close=Decimal(index + 2),
        )
        for index in range(1, 36)
    )

    dates = _latest_common_provider_dates((brent, gold))

    assert len(dates) == 30
    assert dates[0] == start + timedelta(days=5)
    assert dates[-1] == start + timedelta(days=34)
    points = _normalized_common_date_points(brent, dates)
    assert points[0].x == str(dates[0])
    assert points[0].value == Decimal(100)
    assert points[-1].value == Decimal("583.3333")
    with pytest.raises(DataSourceContractError, match="fewer than 30 common"):
        _latest_common_provider_dates((brent[:29], gold[:29]))


def test_manifest_rounding_does_not_depend_on_decimal_context() -> None:
    with localcontext() as context:
        context.rounding = ROUND_UP
        assert _quantize(Decimal("1.23445"), 4, "ROUND_HALF_EVEN") == Decimal("1.2344")


def test_revision_is_noop_only_for_same_input_and_manifest() -> None:
    assert (
        _next_revision(
            latest_input_digest="a" * 64,
            latest_manifest_hash="b" * 64,
            latest_revision=3,
            candidate_input_digest="a" * 64,
            candidate_manifest_hash="b" * 64,
        )
        is None
    )
    assert (
        _next_revision(
            latest_input_digest="a" * 64,
            latest_manifest_hash="b" * 64,
            latest_revision=3,
            candidate_input_digest="c" * 64,
            candidate_manifest_hash="b" * 64,
        )
        == 4
    )


def test_revision_lock_is_stable_and_scoped_to_market_and_edition() -> None:
    first = _revision_lock_key("daily-market", "crypto", date(2026, 8, 30))
    assert first == _revision_lock_key("daily-market", "crypto", date(2026, 8, 30))
    assert first != _revision_lock_key("daily-market", "us_equity", date(2026, 8, 30))
    assert first != _revision_lock_key("daily-market", "crypto", date(2026, 8, 31))
    assert -(2**63) <= first < 2**63
    assert (
        _next_revision(
            latest_input_digest="a" * 64,
            latest_manifest_hash="b" * 64,
            latest_revision=3,
            candidate_input_digest="a" * 64,
            candidate_manifest_hash="d" * 64,
        )
        == 4
    )
