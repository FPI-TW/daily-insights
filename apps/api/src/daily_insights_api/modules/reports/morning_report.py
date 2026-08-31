import asyncio
import hashlib
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.data_sources.api import (
    TWELVE_DATA_CONTRACT_HASH,
    TWELVE_DATA_CONTRACT_VERSION,
    DailyBar,
    DataSourceContractError,
    Provenance,
    TwelveDataAdapter,
)
from daily_insights_api.modules.operations.api import (
    PipelineBusyError,
    PipelineSpec,
    ReportPipelineRun,
    claim_pipeline_run,
    complete_source_run,
    fail_source_run,
    publish_completed_run,
    sanitize_error_code,
    start_source_run,
)
from daily_insights_api.modules.reports.contracts import (
    BlockStatus,
    ChartPoint,
    ChartSeries,
    Locale,
    LocalizedElementText,
    MetricBlock,
    MetricItem,
    PresentationContract,
    PublicationBundle,
    PublicationContent,
    ReportBlock,
    ReportStatus,
    SeriesBlock,
    TableBlock,
    TableCell,
    TableColumn,
)
from daily_insights_api.modules.reports.launch_manifest import (
    ACTIVE_LAUNCH_MANIFEST,
    DatasetManifest,
    LaunchMarketCode,
)
from daily_insights_api.modules.reports.models import ReportPublication

_TITLES = {
    "zh-hant": {
        "global_macro_bonds": "全球宏觀與債券",
        "crypto": "加密資產",
        "us_equity": "美國股票",
    },
    "zh-hans": {
        "global_macro_bonds": "全球宏观与债券",
        "crypto": "加密资产",
        "us_equity": "美国股票",
    },
    "en": {
        "global_macro_bonds": "Global macro & bonds",
        "crypto": "Crypto",
        "us_equity": "US equities",
    },
}

MORNING_REPORT_DERIVATION_VERSION = "twelve-data.three-market.v2"
ExecutionMode = Literal["scheduled", "one_shot"]


def authorize_morning_report_execution(
    settings: Settings,
    *,
    execution_mode: ExecutionMode,
    allow_draft_local: bool,
) -> None:
    if allow_draft_local:
        if settings.environment not in {"development", "test"}:
            raise RuntimeError("draft manifest execution is restricted to local environments")
        if execution_mode != "one_shot":
            raise RuntimeError("draft manifest execution requires one-shot mode")
        return
    if ACTIVE_LAUNCH_MANIFEST.status != "approved":
        raise RuntimeError("credentialed probe approval is required before publication")
    if settings.twelve_data_manifest_approved_hash != ACTIVE_LAUNCH_MANIFEST.sha256:
        raise RuntimeError("runtime approval hash does not match the active manifest")


async def run_morning_report_edition(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    adapter: TwelveDataAdapter,
    edition_date: date,
    *,
    execution_mode: ExecutionMode = "scheduled",
    allow_draft_local: bool = False,
) -> None:
    authorize_morning_report_execution(
        settings,
        execution_mode=execution_mode,
        allow_draft_local=allow_draft_local,
    )
    await asyncio.gather(
        *(
            _run_market(session_factory, adapter, market.market_code, edition_date)
            for market in ACTIVE_LAUNCH_MANIFEST.markets
        )
    )


async def _run_market(
    session_factory: async_sessionmaker[AsyncSession],
    adapter: TwelveDataAdapter,
    market_code: LaunchMarketCode,
    edition_date: date,
) -> None:
    owner = f"morning-report-{uuid.uuid4()}"
    dataset = next(
        dataset
        for dataset in ACTIVE_LAUNCH_MANIFEST.datasets
        if dataset.key
        in {
            key
            for market in ACTIVE_LAUNCH_MANIFEST.markets
            if market.market_code == market_code
            for block in market.blocks
            for key in block.datasets
        }
    )
    _validate_dataset_contract(dataset)
    provenance: Provenance | None = None
    error: Exception | None = None
    try:
        blocks, provenances = await _build_blocks(adapter, market_code)
        _validate_manifest_output(market_code, blocks)
        provenance = _aggregate_provenance(provenances)
        source_status = "succeeded"
        source_marker = provenance.response_digest
    except Exception as caught:
        error = caught
        blocks = _error_blocks(market_code)
        _validate_manifest_output(market_code, blocks)
        source_status = "failed"
        source_marker = sanitize_error_code(type(caught).__name__)
    derivation_version = MORNING_REPORT_DERIVATION_VERSION
    input_digest = hashlib.sha256(
        f"{derivation_version}|twelve_data:{dataset.key}:{source_status}:{source_marker}".encode()
    ).hexdigest()

    async with session_factory() as database:
        while True:
            now = datetime.now(UTC)
            await database.execute(
                select(
                    func.pg_advisory_xact_lock(
                        _revision_lock_key("daily-market", market_code, edition_date)
                    )
                )
            )
            latest = (
                await database.scalars(
                    select(ReportPublication)
                    .where(
                        ReportPublication.report_key == "daily-market",
                        ReportPublication.market_code == market_code,
                        ReportPublication.edition_date == edition_date,
                    )
                    .order_by(ReportPublication.revision.desc())
                    .limit(1)
                    .with_for_update()
                )
            ).one_or_none()
            revision = _next_revision(
                latest_input_digest=latest.input_digest if latest is not None else None,
                latest_manifest_hash=latest.manifest_hash if latest is not None else None,
                latest_revision=latest.revision if latest is not None else None,
                candidate_input_digest=input_digest,
                candidate_manifest_hash=ACTIVE_LAUNCH_MANIFEST.sha256,
            )
            if revision is None:
                await database.rollback()
                return
            latest_run = (
                await database.scalars(
                    select(ReportPipelineRun)
                    .where(
                        ReportPipelineRun.report_key == "daily-market",
                        ReportPipelineRun.market_code == market_code,
                        ReportPipelineRun.edition_date == edition_date,
                    )
                    .order_by(ReportPipelineRun.revision.desc())
                    .limit(1)
                    .with_for_update()
                )
            ).one_or_none()
            if latest_run is not None and latest_run.revision >= revision:
                if _has_active_lease(latest_run, now):
                    await database.rollback()
                    await asyncio.sleep(0.1)
                    continue
                revision = (
                    latest_run.revision
                    if latest_run.manifest_hash == ACTIVE_LAUNCH_MANIFEST.sha256
                    else latest_run.revision + 1
                )
            spec = PipelineSpec(
                report_key="daily-market",
                market_code=market_code,
                edition_date=edition_date,
                revision=revision,
                derivation_version=derivation_version,
                content_schema_version="three-market.v1",
                manifest_version=ACTIVE_LAUNCH_MANIFEST.version,
                manifest_hash=ACTIVE_LAUNCH_MANIFEST.sha256,
            )
            try:
                run = await claim_pipeline_run(
                    database,
                    spec,
                    lease_owner=owner,
                    now=now,
                    lease_for=timedelta(minutes=10),
                )
            except PipelineBusyError:
                await database.rollback()
                await asyncio.sleep(0.1)
                continue
            await database.commit()
            if run.status == "published":
                continue
            break
        attempt = run.attempt_count
        fingerprint = hashlib.sha256(
            json.dumps(dataset.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()
        source = await start_source_run(
            database,
            pipeline_run_id=run.id,
            lease_owner=owner,
            lease_attempt=attempt,
            provider="twelve_data",
            dataset_key=dataset.key,
            attempt=1,
            contract_version=TWELVE_DATA_CONTRACT_VERSION,
            contract_hash=adapter_contract_hash(),
            endpoint=dataset.endpoint,
            request_fingerprint=fingerprint,
            now=datetime.now(UTC),
        )
        await database.commit()

        if provenance is not None:
            complete_source_run(
                source,
                source_as_of=provenance.as_of or edition_date,
                fetched_at=provenance.fetched_at,
                record_count=provenance.record_count,
                payload_sha256=provenance.response_digest,
                provider_request_id=provenance.request_id,
                finished_at=datetime.now(UTC),
            )
        else:
            assert error is not None
            fail_source_run(
                source,
                error_code=type(error).__name__,
                error_detail=str(error),
                finished_at=datetime.now(UTC),
            )
        await database.commit()

        bundle = _bundle(market_code, blocks)
        await publish_completed_run(
            database,
            pipeline_run_id=run.id,
            lease_owner=owner,
            lease_attempt=attempt,
            bundle=bundle,
            source_run_ids=(source.id,),
            required_dataset_keys=frozenset((dataset.key,)),
            now=datetime.now(UTC),
        )
        await database.commit()


async def _build_blocks(
    adapter: TwelveDataAdapter, market_code: LaunchMarketCode
) -> tuple[tuple[ReportBlock, ...], tuple[Provenance, ...]]:
    if market_code == "global_macro_bonds":
        dataset = next(
            item for item in ACTIVE_LAUNCH_MANIFEST.datasets if item.key == "macro.commodity_quotes"
        )
        quotes = await asyncio.gather(
            *(
                adapter.get_quote(
                    market=market_code,
                    symbol=symbol,
                    expected_currency=dataset.symbol_units[symbol],
                )
                for symbol in dataset.symbols
            )
        )
        as_of = min(item.as_of for item in quotes)
        macro_block = MetricBlock(
            id="macro.commodities",
            status="ok",
            source_as_of=as_of,
            metrics=tuple(
                MetricItem(
                    id=identifier,
                    value=_quantize(
                        item.close,
                        block_precision("macro.commodities"),
                        block_rounding("macro.commodities"),
                    ),
                    change=_quantize(
                        item.percent_change,
                        block_precision("macro.commodities"),
                        block_rounding("macro.commodities"),
                    ),
                    unit_code=item.currency.lower(),
                )
                for identifier, item in zip(("brent", "gold", "copper"), quotes, strict=True)
            ),
        )
        return (macro_block,), tuple(item.provenance for item in quotes)
    if market_code == "crypto":
        dataset = next(
            item for item in ACTIVE_LAUNCH_MANIFEST.datasets if item.key == "crypto.daily_bars"
        )
        symbols = dataset.symbols
        results = await asyncio.gather(
            *(
                adapter.get_daily_bars(
                    market=market_code,
                    symbol=symbol,
                    expected_currency=dataset.symbol_units[symbol],
                    outputsize=dataset.minimum_history,
                )
                for symbol in symbols
            )
        )
        as_of = min(result.items[-1].trade_date for result in results)
        overview = TableBlock(
            id="crypto.overview",
            status="ok",
            source_as_of=as_of,
            columns=(
                TableColumn(id="asset"),
                TableColumn(id="price", unit_code="usd"),
                TableColumn(id="change", unit_code="percent"),
            ),
            rows=tuple(
                (
                    TableCell(text=symbol.split("/")[0]),
                    TableCell(
                        value=_quantize(
                            result.items[-1].close,
                            block_precision("crypto.overview"),
                            block_rounding("crypto.overview"),
                        )
                    ),
                    TableCell(
                        value=_quantize(
                            _daily_change(result.items[-1].open, result.items[-1].close),
                            block_precision("crypto.overview"),
                            block_rounding("crypto.overview"),
                        )
                    ),
                )
                for symbol, result in zip(symbols, results, strict=True)
            ),
        )
        normalized = SeriesBlock(
            id="crypto.normalized_performance",
            status="ok",
            source_as_of=as_of,
            unit_code="index",
            series=tuple(
                ChartSeries(
                    id=symbol.split("/")[0].lower(),
                    points=_normalized_points(
                        result.items,
                        precision=block_precision("crypto.normalized_performance"),
                        rounding=block_rounding("crypto.normalized_performance"),
                    ),
                )
                for symbol, result in zip(symbols, results, strict=True)
            ),
        )
        return (overview, normalized), tuple(result.provenance for result in results)
    gainers, losers = await asyncio.gather(
        adapter.get_stock_movers(direction="gainers", outputsize=2),
        adapter.get_stock_movers(direction="losers", outputsize=2),
    )
    items = gainers.items + losers.items
    movers_block = TableBlock(
        id="us.market_movers",
        status="ok",
        source_as_of=min(item.as_of for item in items),
        columns=(
            TableColumn(id="instrument"),
            TableColumn(id="price", unit_code="usd"),
            TableColumn(id="change", unit_code="percent"),
        ),
        rows=tuple(
            (
                TableCell(text=item.symbol),
                TableCell(
                    value=_quantize(
                        item.close,
                        block_precision("us.market_movers"),
                        block_rounding("us.market_movers"),
                    )
                ),
                TableCell(
                    value=_quantize(
                        item.percent_change,
                        block_precision("us.market_movers"),
                        block_rounding("us.market_movers"),
                    )
                ),
            )
            for item in items
        ),
    )
    return (movers_block,), (gainers.provenance, losers.provenance)


def _normalized_points(
    bars: tuple[DailyBar, ...],
    *,
    precision: int = 4,
    rounding: str = "ROUND_HALF_EVEN",
) -> tuple[ChartPoint, ...]:
    window = bars[-30:]
    base = window[0].close
    if base is None or base == 0:
        return tuple(ChartPoint(x=str(bar.trade_date), value=None) for bar in window)
    assert base is not None
    return tuple(
        ChartPoint(
            x=str(bar.trade_date),
            value=_quantize(_normalize_close(bar.close, base), precision, rounding),
        )
        for bar in window
    )


def _normalize_close(close: Decimal | None, base: Decimal) -> Decimal | None:
    return close / base * Decimal(100) if close is not None else None


def _daily_change(open_value: Decimal | None, close: Decimal | None) -> Decimal | None:
    if open_value is None or open_value == 0 or close is None:
        return None
    return (close / open_value - Decimal(1)) * Decimal(100)


_ROUNDING_MODES = {"ROUND_HALF_EVEN": ROUND_HALF_EVEN}


def _quantize(
    value: Decimal | None,
    precision: int,
    rounding: str = "ROUND_HALF_EVEN",
) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(
        Decimal(1).scaleb(-precision),
        rounding=_ROUNDING_MODES[rounding],
    )


def block_precision(block_id: str) -> int:
    return next(
        block.precision
        for market in ACTIVE_LAUNCH_MANIFEST.markets
        for block in market.blocks
        if block.id == block_id
    )


def block_rounding(block_id: str) -> str:
    return next(
        block.rounding
        for market in ACTIVE_LAUNCH_MANIFEST.markets
        for block in market.blocks
        if block.id == block_id
    )


def _error_blocks(market_code: LaunchMarketCode) -> tuple[ReportBlock, ...]:
    market = next(
        market for market in ACTIVE_LAUNCH_MANIFEST.markets if market.market_code == market_code
    )
    blocks: list[ReportBlock] = []
    for block in market.blocks:
        status: BlockStatus = "error"
        if block.kind == "metric":
            blocks.append(
                MetricBlock(
                    id=block.id,
                    status=status,
                    source_as_of=None,
                    caveat="provider request failed",
                    metrics=(),
                )
            )
        elif block.kind == "table":
            blocks.append(
                TableBlock(
                    id=block.id,
                    status=status,
                    source_as_of=None,
                    caveat="provider request failed",
                    columns=(),
                    rows=(),
                )
            )
        else:
            blocks.append(
                SeriesBlock(
                    id=block.id,
                    status=status,
                    source_as_of=None,
                    caveat="provider request failed",
                    unit_code="index",
                    series=(),
                )
            )
    return tuple(blocks)


def _bundle(market_code: LaunchMarketCode, blocks: tuple[ReportBlock, ...]) -> PublicationBundle:
    _validate_manifest_output(market_code, blocks)
    ok_dates = [
        block.source_as_of for block in blocks if block.status == "ok" and block.source_as_of
    ]
    status: ReportStatus = (
        "complete"
        if all(block.status == "ok" for block in blocks)
        else "partial"
        if ok_dates
        else "unavailable"
    )
    content = PublicationContent(
        schema_version="three-market.v1",
        market_code=market_code,
        as_of=min(ok_dates) if ok_dates else None,
        status=status,
        caveat=None if status == "complete" else "One or more provider datasets were unavailable.",
        blocks=blocks,
    )
    presentations: dict[Locale, PresentationContract] = {}
    locales: tuple[Locale, ...] = ("zh-hant", "zh-hans", "en")
    for locale in locales:
        title = _TITLES[locale][market_code]
        presentations[locale] = PresentationContract(
            schema_version="three-market.v1",
            locale=locale,
            title=title,
            summary=title,
            labels={
                block.id: LocalizedElementText(
                    title=next(
                        manifest_block.labels[locale]
                        for market in ACTIVE_LAUNCH_MANIFEST.markets
                        for manifest_block in market.blocks
                        if manifest_block.id == block.id
                    )
                )
                for block in blocks
            },
        )
    return PublicationBundle(content=content, presentations=presentations)


def _aggregate_provenance(values: tuple[Provenance, ...]) -> Provenance:
    material = "|".join(sorted(value.response_digest for value in values))
    return Provenance(
        provider="twelve_data",
        contract_version=values[0].contract_version,
        contract_hash=values[0].contract_hash,
        endpoint=values[0].endpoint,
        query_fingerprint=hashlib.sha256(material.encode()).hexdigest(),
        fetched_at=max(value.fetched_at for value in values),
        as_of=min(value.as_of for value in values if value.as_of is not None),
        response_digest=hashlib.sha256(material.encode()).hexdigest(),
        record_count=sum(value.record_count for value in values),
    )


def adapter_contract_hash() -> str:
    return TWELVE_DATA_CONTRACT_HASH


_ENDPOINT_FIELDS: dict[str, frozenset[str]] = {
    "/quote": frozenset(
        {
            "symbol",
            "name",
            "currency",
            "datetime",
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "change",
            "percent_change",
        }
    ),
    "/time_series": frozenset({"datetime", "open", "high", "low", "close", "volume"}),
    "/market_movers/stocks": frozenset(
        {"symbol", "name", "datetime", "last", "high", "low", "volume", "change", "percent_change"}
    ),
}


def _validate_dataset_contract(dataset: DatasetManifest) -> None:
    unsupported = set(dataset.required_fields) - _ENDPOINT_FIELDS[dataset.endpoint]
    if unsupported:
        fields = ", ".join(sorted(unsupported))
        raise DataSourceContractError(
            f"manifest dataset {dataset.key} requires unsupported fields: {fields}"
        )
    if dataset.atomicity != "all_or_error":
        raise DataSourceContractError(
            f"manifest dataset {dataset.key} must use all_or_error atomicity"
        )


def _validate_manifest_output(
    market_code: LaunchMarketCode,
    blocks: tuple[ReportBlock, ...],
) -> None:
    market = next(
        market for market in ACTIVE_LAUNCH_MANIFEST.markets if market.market_code == market_code
    )
    expected_ids = tuple(block.id for block in market.blocks)
    actual_ids = tuple(block.id for block in blocks)
    if actual_ids != expected_ids:
        raise DataSourceContractError(
            f"report blocks for {market_code} must exactly match manifest order {expected_ids}"
        )
    expected_kinds = tuple(block.kind for block in market.blocks)
    actual_kinds = tuple(block.kind for block in blocks)
    if actual_kinds != expected_kinds:
        raise DataSourceContractError(
            f"report block kinds for {market_code} must exactly match the manifest"
        )

    output_by_id = {block.id: block for block in blocks}
    manifest_by_id = {block.id: block for block in market.blocks}
    for block in blocks:
        precision = manifest_by_id[block.id].precision
        if any(_exceeds_precision(value, precision) for value in _decimal_values(block)):
            raise DataSourceContractError(
                f"report block {block.id} exceeds manifest precision {precision}"
            )
    for dataset_key in {key for block in market.blocks for key in block.datasets}:
        statuses = {
            output_by_id[block.id].status
            for block in market.blocks
            if dataset_key in block.datasets
        }
        if len(statuses) != 1:
            raise DataSourceContractError(
                f"dataset {dataset_key} produced mixed block statuses "
                "despite all_or_error atomicity"
            )


def _decimal_values(block: ReportBlock) -> tuple[Decimal, ...]:
    if isinstance(block, MetricBlock):
        return tuple(
            value
            for metric in block.metrics
            for value in (metric.value, metric.change)
            if value is not None
        )
    if isinstance(block, TableBlock):
        return tuple(
            cell.value
            for row in block.rows
            for cell in row
            if cell is not None and cell.value is not None
        )
    return tuple(
        point.value for series in block.series for point in series.points if point.value is not None
    )


def _exceeds_precision(value: Decimal, precision: int) -> bool:
    exponent = value.as_tuple().exponent
    return isinstance(exponent, int) and exponent < -precision


def _next_revision(
    *,
    latest_input_digest: str | None,
    latest_manifest_hash: str | None,
    latest_revision: int | None,
    candidate_input_digest: str,
    candidate_manifest_hash: str,
) -> int | None:
    if latest_revision is None:
        return 1
    if (
        latest_input_digest == candidate_input_digest
        and latest_manifest_hash == candidate_manifest_hash
    ):
        return None
    return latest_revision + 1


def _revision_lock_key(report_key: str, market_code: str, edition_date: date) -> int:
    material = f"{report_key}|{market_code}|{edition_date.isoformat()}".encode()
    value = int(hashlib.sha256(material).hexdigest()[:16], 16)
    return value - 2**64 if value >= 2**63 else value


def _has_active_lease(run: ReportPipelineRun, now: datetime) -> bool:
    if run.status != "running" or run.lease_expires_at is None:
        return False
    expires_at = run.lease_expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at > now
