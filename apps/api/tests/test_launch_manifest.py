from datetime import date, timedelta
from decimal import ROUND_UP, Decimal, localcontext

import pytest

from daily_insights_api.modules.data_sources.api import DailyBar, DataSourceContractError
from daily_insights_api.modules.reports.contracts import TableCell, TableColumn
from daily_insights_api.modules.reports.launch_manifest import (
    ACTIVE_LAUNCH_MANIFEST,
    LAUNCH_MARKET_ORDER,
    LaunchManifest,
)
from daily_insights_api.modules.reports.morning_report import (
    _error_blocks,
    _next_revision,
    _normalized_points,
    _quantize,
    _revision_lock_key,
    _validate_dataset_contract,
    _validate_manifest_output,
    block_precision,
    block_rounding,
)


def test_manifest_freezes_three_markets_and_block_order() -> None:
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
        "crypto.overview",
        "crypto.normalized_performance",
        "us.market_movers",
    ]
    assert {
        block.rounding for market in ACTIVE_LAUNCH_MANIFEST.markets for block in market.blocks
    } == {"ROUND_HALF_EVEN"}
    assert block_precision("crypto.overview") == 4
    assert block_rounding("crypto.overview") == "ROUND_HALF_EVEN"


def test_manifest_hash_is_stable_and_changes_with_content() -> None:
    round_trip = LaunchManifest.model_validate(ACTIVE_LAUNCH_MANIFEST.model_dump(mode="json"))
    assert round_trip.sha256 == ACTIVE_LAUNCH_MANIFEST.sha256
    changed = round_trip.model_copy(update={"version": "three-market.v2"})
    assert changed.sha256 != round_trip.sha256


def test_unprobed_manifest_remains_a_fail_closed_launch_gate() -> None:
    assert ACTIVE_LAUNCH_MANIFEST.status == "draft"
    assert {dataset.atomicity for dataset in ACTIVE_LAUNCH_MANIFEST.datasets} == {"all_or_error"}


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
