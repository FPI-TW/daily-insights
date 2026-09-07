from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_UP, Decimal, localcontext

import pytest

from daily_insights_api.modules.data_sources.api import (
    DailyBar,
    DataSourceContractError,
    EodResult,
    Provenance,
)
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
    _commodity_metric_item,
    _commodity_ratio_window_dates,
    _error_blocks,
    _error_blocks_for_dataset,
    _input_digest,
    _latest_common_provider_dates,
    _market_datasets,
    _next_revision,
    _normalized_common_date_points,
    _normalized_points,
    _quantize,
    _ratio_common_date_points,
    _revision_lock_key,
    _scheduled_edition_lock_key,
    _validate_dataset_contract,
    _validate_manifest_output,
    block_precision,
    block_rounding,
    completed_history_for_eod,
)


def test_manifest_freezes_three_markets_and_block_order() -> None:
    assert MORNING_REPORT_DERIVATION_VERSION == "twelve-data.three-market.v9"
    assert ACTIVE_LAUNCH_MANIFEST.version == "three-market.v9"
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
        "macro.rates_fx",
        "macro.commodity_ratios",
        "crypto.overview",
        "crypto.normalized_performance",
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
    changed = round_trip.model_copy(update={"version": "three-market.v10"})
    assert changed.sha256 != round_trip.sha256


def test_manifest_keeps_atomic_dataset_contracts() -> None:
    assert {dataset.atomicity for dataset in ACTIVE_LAUNCH_MANIFEST.datasets} == {"all_or_error"}
    assert next(
        dataset.symbol_units
        for dataset in ACTIVE_LAUNCH_MANIFEST.datasets
        if dataset.key == "macro.commodity_eod"
    ) == {
        "WTI/USD": "USD",
        "XBR/USD": "USD",
        "XAU/USD": "USD",
        "XAG/USD": "USD",
        "HG1": "USD",
    }
    quotes = next(
        dataset
        for dataset in ACTIVE_LAUNCH_MANIFEST.datasets
        if dataset.key == "macro.commodity_eod"
    )
    # HG1 without a type resolves to a German stock; the manifest pins copper.
    assert quotes.endpoint == "/eod"
    assert quotes.symbol_types == {
        "WTI/USD": "commodity",
        "XBR/USD": "commodity",
        "XAU/USD": "commodity",
        "XAG/USD": "commodity",
        "HG1": "commodity",
    }
    assert quotes.minimum_history == 800
    assert quotes.expected_asset_types == {
        "WTI/USD": "Energy Resource",
        "XBR/USD": "Energy Resource",
        "XAU/USD": "Precious Metal",
        "XAG/USD": "Precious Metal",
        "HG1": "Industrial Metal",
    }
    assert tuple(dataset.key for dataset in _market_datasets("global_macro_bonds")) == (
        "macro.commodity_eod",
        "macro.rates_fx_quotes",
    )
    rates = next(
        dataset
        for dataset in ACTIVE_LAUNCH_MANIFEST.datasets
        if dataset.key == "macro.rates_fx_quotes"
    )
    assert rates.symbol_units["USD/TWD"] == "TWD" and rates.symbol_units["TLT"] == "USD"


def test_manifest_freezes_commodity_ratio_labels_and_precision() -> None:
    ratios = next(
        block
        for market in ACTIVE_LAUNCH_MANIFEST.markets
        for block in market.blocks
        if block.id == "macro.commodity_ratios"
    )
    assert ratios.unit_code == "ratio"
    assert ratios.precision == 6
    assert (
        "each WTI/USD, XAU/USD, and HG1 completed history must reach on or before "
        "the exact two-calendar-year start boundary"
    ) in ratios.formula
    commodity_dataset = next(
        dataset
        for dataset in ACTIVE_LAUNCH_MANIFEST.datasets
        if dataset.key == "macro.commodity_eod"
    )
    assert "each WTI/USD, XAU/USD, and HG1 history reaches that exact boundary" in (
        commodity_dataset.freshness
    )
    assert ratios.labels == {
        "zh-hant": "油金比 / 銅金比",
        "zh-hans": "油金比 / 铜金比",
        "en": "Oil-Gold / Copper-Gold Ratios",
    }
    assert ratios.series_labels == {
        "zh-hant": {"oil_gold_ratio": "油金比", "copper_gold_ratio": "銅金比"},
        "zh-hans": {"oil_gold_ratio": "油金比", "copper_gold_ratio": "铜金比"},
        "en": {
            "oil_gold_ratio": "Oil-Gold Ratio",
            "copper_gold_ratio": "Copper-Gold Ratio",
        },
    }


def test_us_equity_uses_fixed_mega_cap_basket_instead_of_provider_movers() -> None:
    keys = {dataset.key: dataset for dataset in ACTIVE_LAUNCH_MANIFEST.datasets}
    assert "us.market_movers" not in keys
    mega_caps = keys["us.mega_cap_quotes"]
    assert mega_caps.endpoint == "/quote"
    assert len(mega_caps.symbols) == 8 and "NVDA" in mega_caps.symbols
    assert set(mega_caps.symbol_units.values()) == {"USD"}
    assert tuple(dataset.key for dataset in _market_datasets("us_equity")) == (
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
    next(dataset for dataset in payload["datasets"] if dataset["key"] == "macro.commodity_eod")[
        "symbol_types"
    ] = {"ZZZ": "commodity"}

    with pytest.raises(ValueError, match="symbol type overrides"):
        LaunchManifest.model_validate(payload)


def test_manifest_allows_multiple_datasets_for_one_market() -> None:
    assert LaunchManifest.model_validate(ACTIVE_LAUNCH_MANIFEST.model_dump(mode="json"))


def test_manifest_rejects_blocks_with_multiple_dataset_references() -> None:
    payload = ACTIVE_LAUNCH_MANIFEST.model_dump(mode="json")
    payload["markets"][0]["blocks"][0]["datasets"] = (
        "macro.commodity_eod",
        "macro.rates_fx_quotes",
    )

    with pytest.raises(ValueError, match="every block must reference exactly one dataset"):
        LaunchManifest.model_validate(payload)


def test_manifest_rejects_dataset_reused_by_another_market() -> None:
    payload = ACTIVE_LAUNCH_MANIFEST.model_dump(mode="json")
    payload["markets"][1]["blocks"][0]["datasets"] = ("macro.commodity_eod",)

    with pytest.raises(ValueError, match="every dataset must be referenced by exactly one market"):
        LaunchManifest.model_validate(payload)


def test_manifest_rejects_duplicate_and_unreferenced_datasets() -> None:
    duplicate = ACTIVE_LAUNCH_MANIFEST.model_dump(mode="json")
    duplicate["datasets"] = (*duplicate["datasets"], duplicate["datasets"][0])
    with pytest.raises(ValueError, match="dataset keys must be unique"):
        LaunchManifest.model_validate(duplicate)

    unreferenced = ACTIVE_LAUNCH_MANIFEST.model_dump(mode="json")
    unreferenced["markets"][0]["blocks"][1]["datasets"] = ("macro.commodity_eod",)
    with pytest.raises(ValueError, match="exactly cover declared datasets"):
        LaunchManifest.model_validate(unreferenced)


def test_manifest_rejects_partial_asset_type_contract() -> None:
    payload = ACTIVE_LAUNCH_MANIFEST.model_dump(mode="json")
    next(dataset for dataset in payload["datasets"] if dataset["key"] == "macro.commodity_eod")[
        "expected_asset_types"
    ] = {"XBR/USD": "Energy Resource"}

    with pytest.raises(ValueError, match="asset-type contracts"):
        LaunchManifest.model_validate(payload)


def test_dataset_input_digest_is_stable_across_dataset_order() -> None:
    commodity, rates = _market_datasets("global_macro_bonds")
    commodity_failure = DatasetBuild(commodity, (), None, ValueError("commodity failed"))
    rates_failure = DatasetBuild(rates, (), None, ValueError("rates failed"))

    assert _input_digest(
        MORNING_REPORT_DERIVATION_VERSION, (commodity_failure, rates_failure)
    ) == _input_digest(MORNING_REPORT_DERIVATION_VERSION, (rates_failure, commodity_failure))


def test_macro_dataset_failure_preserves_the_other_block_and_status() -> None:
    as_of = date(2026, 8, 30)
    rates_block = MetricBlock(
        id="macro.rates_fx",
        status="ok",
        source_as_of=as_of,
        metrics=(),
    )
    commodity_errors = _error_blocks_for_dataset("global_macro_bonds", "macro.commodity_eod")
    partial_blocks = (commodity_errors[0], rates_block, commodity_errors[1])
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


def test_commodity_history_uses_eod_as_mutable_bar_cutoff_and_requires_reconciliation() -> None:
    eod_date = date(2026, 9, 4)
    provenance = Provenance(
        provider="twelve_data",
        contract_version="test.v1",
        contract_hash="a" * 64,
        endpoint="/eod",
        query_fingerprint="b" * 64,
        fetched_at=datetime(2026, 9, 5, tzinfo=UTC),
        as_of=eod_date,
        response_digest="c" * 64,
        record_count=1,
    )
    eod = EodResult(
        symbol="XBR/USD",
        currency="USD",
        as_of=eod_date,
        close=Decimal("94.60791741912"),
        provenance=provenance,
    )
    bars = tuple(
        DailyBar(
            instrument_source_id="XBR/USD",
            market="global_macro_bonds",
            symbol="XBR/USD",
            trade_date=item_date,
            close=close,
        )
        for item_date, close in (
            (date(2026, 9, 3), Decimal("90")),
            (eod_date, Decimal("94.60791741912")),
            (date(2026, 9, 5), Decimal("120")),
        )
    )

    completed = completed_history_for_eod(bars, eod)

    assert [bar.trade_date for bar in completed] == [date(2026, 9, 3), eod_date]
    metric = _commodity_metric_item("brent", eod, completed[-2].close)
    assert metric.value == Decimal("94.6079")
    assert metric.change == Decimal("5.1199")
    rounded_default_precision = (
        *bars[:1],
        bars[1].model_copy(update={"close": Decimal("94.60792")}),
    )
    with pytest.raises(DataSourceContractError, match="date or close"):
        completed_history_for_eod(rounded_default_precision, eod)
    with pytest.raises(DataSourceContractError, match="date or close"):
        completed_history_for_eod(bars[:-2], eod)
    with pytest.raises(DataSourceContractError, match="previous completed"):
        completed_history_for_eod((bars[1],), eod)


def test_commodity_ratio_window_uses_exact_common_completed_dates_for_two_calendar_years() -> None:
    latest = date(2026, 2, 28)
    start = date(2024, 2, 28)

    def history(symbol: str, skipped: set[date] | None = None) -> tuple[DailyBar, ...]:
        return tuple(
            DailyBar(
                instrument_source_id=symbol,
                market="global_macro_bonds",
                symbol=symbol,
                trade_date=start + timedelta(days=index),
                close=Decimal("10"),
            )
            for index in range((latest - start).days + 1)
            if start + timedelta(days=index) not in (skipped or set())
        )

    wti = history("WTI/USD")
    gold = history("XAU/USD", {date(2025, 1, 1)})
    copper = history("HG1")

    window = _commodity_ratio_window_dates((wti, gold, copper))

    assert window[0] == start
    assert window[-1] == latest
    assert date(2025, 1, 1) not in window
    with pytest.raises(DataSourceContractError, match="start boundary"):
        _commodity_ratio_window_dates((wti[1:], gold, copper))
    with pytest.raises(DataSourceContractError, match="no common"):
        _commodity_ratio_window_dates((wti[:1], gold[1:], copper))


def test_commodity_ratio_window_rejects_late_common_coverage_but_allows_week_one() -> None:
    start = date(2024, 8, 29)
    latest = date(2026, 8, 29)

    def history(symbol: str, dates: tuple[date, ...]) -> tuple[DailyBar, ...]:
        return tuple(
            DailyBar(
                instrument_source_id=symbol,
                market="global_macro_bonds",
                symbol=symbol,
                trade_date=item,
                close=Decimal("10"),
            )
            for item in dates
        )

    baseline = tuple(start + timedelta(days=index) for index in range((latest - start).days + 1))
    # Each history reaches the boundary, but their only common run begins ten
    # days later. The gap must not silently shorten the two-year ratio window.
    late = (
        history("WTI/USD", (start, *baseline[10:])),
        history("XAU/USD", (start - timedelta(days=1), *baseline[10:])),
        history("HG1", (start - timedelta(days=2), *baseline[10:])),
    )
    with pytest.raises(DataSourceContractError, match="more than seven days"):
        _commodity_ratio_window_dates(late)

    accepted = (
        history("WTI/USD", (start, *baseline[7:])),
        history("XAU/USD", (start - timedelta(days=1), *baseline[7:])),
        history("HG1", (start - timedelta(days=2), *baseline[7:])),
    )
    window = _commodity_ratio_window_dates(accepted)
    assert window[0] == start + timedelta(days=7)
    assert window[-1] == latest


@pytest.mark.parametrize(
    ("numerator_close", "denominator_close"),
    [
        (None, Decimal("10")),
        (Decimal("0"), Decimal("10")),
        (Decimal("-1"), Decimal("10")),
        (Decimal("10"), None),
        (Decimal("10"), Decimal("0")),
        (Decimal("10"), Decimal("-1")),
    ],
)
def test_commodity_ratio_rejects_non_positive_or_missing_inputs(
    numerator_close: Decimal | None, denominator_close: Decimal | None
) -> None:
    day = date(2026, 8, 29)

    def bars(symbol: str, close: Decimal | None) -> tuple[DailyBar, ...]:
        return (
            DailyBar(
                instrument_source_id=symbol,
                market="global_macro_bonds",
                symbol=symbol,
                trade_date=day,
                close=close,
            ),
        )

    with pytest.raises(DataSourceContractError, match="no usable completed close"):
        _ratio_common_date_points(
            bars("WTI/USD", numerator_close),
            bars("XAU/USD", denominator_close),
            (day,),
            precision=6,
            rounding="ROUND_HALF_EVEN",
        )


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
    scheduled = _scheduled_edition_lock_key("daily-market", "crypto", date(2026, 8, 30))
    assert first == _revision_lock_key("daily-market", "crypto", date(2026, 8, 30))
    assert scheduled == _scheduled_edition_lock_key("daily-market", "crypto", date(2026, 8, 30))
    assert scheduled != first
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
