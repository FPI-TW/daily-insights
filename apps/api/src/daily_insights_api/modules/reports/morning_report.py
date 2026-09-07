import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api.modules.data_sources.api import (
    TWELVE_DATA_CONTRACT_HASH,
    TWELVE_DATA_CONTRACT_VERSION,
    DailyBar,
    DataSourceContractError,
    EodResult,
    Provenance,
    QuoteResult,
    QuotesResult,
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

MORNING_REPORT_DERIVATION_VERSION = "twelve-data.three-market.v9"


@dataclass(frozen=True)
class DatasetBuild:
    dataset: DatasetManifest
    blocks: tuple[ReportBlock, ...]
    provenance: Provenance | None
    error: Exception | None

    @property
    def status(self) -> str:
        return "succeeded" if self.provenance is not None else "failed"

    @property
    def marker(self) -> str:
        return (
            self.provenance.response_digest
            if self.provenance is not None
            else sanitize_error_code(
                type(self.error).__name__ if self.error is not None else "unknown"
            )
        )


@dataclass(frozen=True)
class MorningDatasetExecution:
    dataset_key: str
    status: str
    fetched_at: datetime | None
    source_as_of: date | None
    record_count: int | None
    error: str | None


@dataclass(frozen=True)
class MorningMarketExecution:
    market_code: LaunchMarketCode
    publication_action: str
    revision: int | None
    report_status: str | None
    source_date: date | None
    datasets: tuple[MorningDatasetExecution, ...]


async def run_morning_report_edition(
    session_factory: async_sessionmaker[AsyncSession],
    adapter: TwelveDataAdapter,
    edition_date: date,
    *,
    market_codes: tuple[LaunchMarketCode, ...] | None = None,
) -> tuple[MorningMarketExecution, ...]:
    requested_markets = market_codes or tuple(
        market.market_code for market in ACTIVE_LAUNCH_MANIFEST.markets
    )
    executions = await asyncio.gather(
        *(
            _run_market(session_factory, adapter, market_code, edition_date)
            for market_code in requested_markets
        )
    )
    return tuple(executions)


async def unpublished_morning_report_markets(
    session_factory: async_sessionmaker[AsyncSession], edition_date: date
) -> tuple[LaunchMarketCode, ...]:
    """Return launch markets without any published revision for this edition.

    Scheduler restarts must treat complete, partial, and unavailable
    publications alike: each is an immutable terminal publication and must not
    trigger another provider request. Manual runs intentionally bypass this
    guard so they retain the existing revision/no-op behavior.
    """
    market_order = tuple(market.market_code for market in ACTIVE_LAUNCH_MANIFEST.markets)
    async with session_factory() as database:
        existing = set(
            (
                await database.scalars(
                    select(ReportPublication.market_code)
                    .where(
                        ReportPublication.report_key == "daily-market",
                        ReportPublication.edition_date == edition_date,
                        ReportPublication.market_code.in_(market_order),
                    )
                    .distinct()
                )
            ).all()
        )
    return tuple(market for market in market_order if market not in existing)


async def run_scheduled_morning_report_markets(
    session_factory: async_sessionmaker[AsyncSession],
    adapter: TwelveDataAdapter,
    edition_date: date,
    market_codes: tuple[LaunchMarketCode, ...],
) -> tuple[LaunchMarketCode, ...]:
    """Run missing scheduled markets under per-edition advisory locks.

    The earlier startup preflight is only an optimization. This definitive
    check occurs while a transaction-scoped scheduler lock is held, before any
    provider call, so a second scheduler instance waits and then observes the
    first publication instead of rebuilding it.
    """
    outcomes = await asyncio.gather(
        *(
            _run_scheduled_market(session_factory, adapter, market_code, edition_date)
            for market_code in market_codes
        )
    )
    return tuple(
        market_code
        for market_code, was_already_published in zip(market_codes, outcomes, strict=True)
        if was_already_published
    )


async def _run_scheduled_market(
    session_factory: async_sessionmaker[AsyncSession],
    adapter: TwelveDataAdapter,
    market_code: LaunchMarketCode,
    edition_date: date,
) -> bool:
    """Return whether a concurrent scheduler had already published the market."""
    # This session-scoped lock remains held while Twelve Data is called.  A
    # transaction-scoped lock would be released by the guard's commit before
    # the provider request, allowing a manual rerun to spend duplicate credit.
    async with session_factory() as database:
        key = _provider_lock_key("daily-market", market_code, edition_date)
        await database.execute(select(func.pg_advisory_lock(key)))
        try:
            publication_id = await database.scalar(
                select(ReportPublication.id)
                .where(
                    ReportPublication.report_key == "daily-market",
                    ReportPublication.market_code == market_code,
                    ReportPublication.edition_date == edition_date,
                )
                .limit(1)
            )
            if publication_id is not None:
                return True
            await _run_market_unlocked(session_factory, adapter, market_code, edition_date)
            return False
        finally:
            await database.execute(select(func.pg_advisory_unlock(key)))
            await database.rollback()


async def _run_market(
    session_factory: async_sessionmaker[AsyncSession],
    adapter: TwelveDataAdapter,
    market_code: LaunchMarketCode,
    edition_date: date,
) -> MorningMarketExecution:
    # Share exactly the same lock with the scheduled path and CLI --once. The
    # lock is acquired before _build_blocks, which is the first provider call.
    async with session_factory() as lock_database:
        key = _provider_lock_key("daily-market", market_code, edition_date)
        await lock_database.execute(select(func.pg_advisory_lock(key)))
        try:
            return await _run_market_unlocked(session_factory, adapter, market_code, edition_date)
        finally:
            await lock_database.execute(select(func.pg_advisory_unlock(key)))
            await lock_database.rollback()


async def _run_market_unlocked(
    session_factory: async_sessionmaker[AsyncSession],
    adapter: TwelveDataAdapter,
    market_code: LaunchMarketCode,
    edition_date: date,
) -> MorningMarketExecution:
    owner = f"morning-report-{uuid.uuid4()}"
    datasets = _market_datasets(market_code)
    builds = await _build_blocks(adapter, market_code, datasets)
    blocks = _manifest_ordered_blocks(market_code, builds)
    _validate_manifest_output(market_code, blocks)
    derivation_version = MORNING_REPORT_DERIVATION_VERSION
    input_digest = _input_digest(derivation_version, builds)

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
                execution = _market_execution(builds, market_code, latest, "no_change")
                await database.rollback()
                return execution
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
        sources = []
        for build in builds:
            fingerprint = hashlib.sha256(
                json.dumps(build.dataset.model_dump(mode="json"), sort_keys=True).encode()
            ).hexdigest()
            sources.append(
                await start_source_run(
                    database,
                    pipeline_run_id=run.id,
                    lease_owner=owner,
                    lease_attempt=attempt,
                    provider="twelve_data",
                    dataset_key=build.dataset.key,
                    attempt=1,
                    contract_version=TWELVE_DATA_CONTRACT_VERSION,
                    contract_hash=adapter_contract_hash(),
                    endpoint=build.dataset.endpoint,
                    request_fingerprint=fingerprint,
                    now=datetime.now(UTC),
                )
            )
        await database.commit()

        for source, build in zip(sources, builds, strict=True):
            if build.provenance is not None:
                complete_source_run(
                    source,
                    source_as_of=build.provenance.as_of or edition_date,
                    fetched_at=build.provenance.fetched_at,
                    record_count=build.provenance.record_count,
                    payload_sha256=build.provenance.response_digest,
                    provider_request_id=build.provenance.request_id,
                    finished_at=datetime.now(UTC),
                )
            else:
                assert build.error is not None
                fail_source_run(
                    source,
                    error_code=type(build.error).__name__,
                    error_detail=str(build.error),
                    finished_at=datetime.now(UTC),
                )
        await database.commit()

        bundle = _bundle(market_code, blocks)
        published = await publish_completed_run(
            database,
            pipeline_run_id=run.id,
            lease_owner=owner,
            lease_attempt=attempt,
            bundle=bundle,
            source_run_ids=tuple(source.id for source in sources),
            required_dataset_keys=frozenset(dataset.key for dataset in datasets),
            now=datetime.now(UTC),
        )
        await database.commit()
        return _market_execution(builds, market_code, published.publication, "published")


def _market_execution(
    builds: tuple[DatasetBuild, ...],
    market_code: LaunchMarketCode,
    publication: ReportPublication | None,
    publication_action: str,
) -> MorningMarketExecution:
    return MorningMarketExecution(
        market_code=market_code,
        publication_action=publication_action,
        revision=publication.revision if publication is not None else None,
        report_status=(str(publication.content.get("status")) if publication is not None else None),
        source_date=publication.source_as_of if publication is not None else None,
        datasets=tuple(
            MorningDatasetExecution(
                dataset_key=build.dataset.key,
                status=build.status,
                fetched_at=build.provenance.fetched_at if build.provenance else None,
                source_as_of=build.provenance.as_of if build.provenance else None,
                record_count=build.provenance.record_count if build.provenance else None,
                error=(
                    sanitize_error_code(type(build.error).__name__)
                    if build.error is not None
                    else None
                ),
            )
            for build in builds
        ),
    )


def _market_datasets(market_code: LaunchMarketCode) -> tuple[DatasetManifest, ...]:
    market = next(
        item for item in ACTIVE_LAUNCH_MANIFEST.markets if item.market_code == market_code
    )
    keys = tuple(dict.fromkeys(key for block in market.blocks for key in block.datasets))
    datasets_by_key = {dataset.key: dataset for dataset in ACTIVE_LAUNCH_MANIFEST.datasets}
    return tuple(datasets_by_key[key] for key in keys)


def _manifest_ordered_blocks(
    market_code: LaunchMarketCode, builds: tuple[DatasetBuild, ...]
) -> tuple[ReportBlock, ...]:
    blocks = {block.id: block for build in builds for block in build.blocks}
    expected_ids = tuple(
        block.id
        for market in ACTIVE_LAUNCH_MANIFEST.markets
        if market.market_code == market_code
        for block in market.blocks
    )
    if set(blocks) != set(expected_ids):
        raise DataSourceContractError("dataset blocks did not exactly cover the launch manifest")
    return tuple(blocks[identifier] for identifier in expected_ids)


def _input_digest(derivation_version: str, builds: tuple[DatasetBuild, ...]) -> str:
    material = "|".join(
        sorted(f"twelve_data:{build.dataset.key}:{build.status}:{build.marker}" for build in builds)
    )
    return hashlib.sha256(f"{derivation_version}|{material}".encode()).hexdigest()


async def _build_blocks(
    adapter: TwelveDataAdapter,
    market_code: LaunchMarketCode,
    datasets: tuple[DatasetManifest, ...] | None = None,
) -> tuple[DatasetBuild, ...]:
    builds = await asyncio.gather(
        *(
            _build_dataset(adapter, market_code, dataset)
            for dataset in (datasets if datasets is not None else _market_datasets(market_code))
        )
    )
    return tuple(builds)


async def _build_dataset(
    adapter: TwelveDataAdapter,
    market_code: LaunchMarketCode,
    dataset: DatasetManifest,
) -> DatasetBuild:
    try:
        _validate_dataset_contract(dataset)
        blocks, provenance = await _build_dataset_blocks(adapter, market_code, dataset)
        return DatasetBuild(dataset=dataset, blocks=blocks, provenance=provenance, error=None)
    except Exception as caught:
        return DatasetBuild(
            dataset=dataset,
            blocks=_error_blocks_for_dataset(market_code, dataset.key),
            provenance=None,
            error=caught,
        )


async def _build_dataset_blocks(
    adapter: TwelveDataAdapter,
    market_code: LaunchMarketCode,
    dataset: DatasetManifest,
) -> tuple[tuple[ReportBlock, ...], Provenance]:
    if dataset.key == "macro.commodity_eod":
        eods = await adapter.get_eods(
            market=market_code,
            symbols=dataset.symbols,
            expected_currencies=dict(dataset.symbol_units),
        )
        histories = await asyncio.gather(
            *(
                adapter.get_daily_bars(
                    market=market_code,
                    symbol=symbol,
                    expected_currency=dataset.symbol_units[symbol],
                    expected_asset_type=dataset.expected_asset_types[symbol],
                    symbol_type=dataset.symbol_types[symbol],
                    # The daily close is compared exactly with /eod, so both
                    # endpoints must use the same reviewed provider precision.
                    dp=11,
                    outputsize=dataset.minimum_history,
                )
                for symbol in dataset.symbols
            )
        )
        completed_histories = tuple(
            _completed_history_for_eod(result.items, eod)
            for result, eod in zip(histories, eods.items, strict=True)
        )
        macro_block = MetricBlock(
            id="macro.commodities",
            status="ok",
            source_as_of=min(item.as_of for item in eods.items),
            metrics=tuple(
                _commodity_metric_item(identifier, eod, history[-2].close)
                for identifier, eod, history in zip(
                    ("wti", "brent", "gold", "silver", "copper"),
                    eods.items,
                    completed_histories,
                    strict=True,
                )
            ),
        )
        window_dates = _commodity_ratio_window_dates(
            (completed_histories[0], completed_histories[2], completed_histories[4])
        )
        ratios = SeriesBlock(
            id="macro.commodity_ratios",
            status="ok",
            source_as_of=window_dates[-1],
            unit_code="ratio",
            series=tuple(
                ChartSeries(
                    id=identifier,
                    points=_ratio_common_date_points(
                        numerator,
                        completed_histories[2],
                        window_dates,
                        precision=block_precision("macro.commodity_ratios"),
                        rounding=block_rounding("macro.commodity_ratios"),
                    ),
                )
                for identifier, numerator in zip(
                    ("oil_gold_ratio", "copper_gold_ratio"),
                    (completed_histories[0], completed_histories[4]),
                    strict=True,
                )
            ),
        )
        return (
            (macro_block, ratios),
            _aggregate_provenance(
                (eods.provenance, *(result.provenance for result in histories)),
                as_of=min(item.as_of for item in eods.items),
            ),
        )
    if dataset.key == "macro.rates_fx_quotes":
        quotes = await _dataset_quotes(adapter, market_code, dataset)
        rates_block = MetricBlock(
            id="macro.rates_fx",
            status="ok",
            source_as_of=min(item.as_of for item in quotes.items),
            metrics=tuple(
                _metric_item(_metric_id(item.symbol), item, "macro.rates_fx")
                for item in quotes.items
            ),
        )
        return (rates_block,), _aggregate_provenance(quotes.provenances)
    if dataset.key == "us.mega_cap_quotes":
        quotes = await _dataset_quotes(adapter, market_code, dataset)
        # The basket is fixed, so ranking by move only orders the rows; it
        # cannot pull low-priced names in the way provider movers did.
        ranked = sorted(
            quotes.items,
            key=lambda item: _previous_close_change(item.close, item.previous_close),
            reverse=True,
        )
        mega_caps_block = TableBlock(
            id="us.mega_caps",
            status="ok",
            source_as_of=min(item.as_of for item in quotes.items),
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
                            block_precision("us.mega_caps"),
                            block_rounding("us.mega_caps"),
                        )
                    ),
                    TableCell(
                        value=_quantize(
                            _previous_close_change(item.close, item.previous_close),
                            block_precision("us.mega_caps"),
                            block_rounding("us.mega_caps"),
                        )
                    ),
                )
                for item in ranked
            ),
        )
        return (mega_caps_block,), _aggregate_provenance(quotes.provenances)
    if dataset.key == "crypto.daily_bars":
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
        return (overview, normalized), _aggregate_provenance(
            tuple(result.provenance for result in results)
        )
    raise DataSourceContractError(f"unsupported report dataset {dataset.key}")


async def _dataset_quotes(
    adapter: TwelveDataAdapter,
    market_code: LaunchMarketCode,
    dataset: DatasetManifest,
) -> QuotesResult:
    return await adapter.get_quotes(
        market=market_code,
        symbols=dataset.symbols,
        expected_currencies=dict(dataset.symbol_units),
        symbol_types=dict(dataset.symbol_types),
    )


def _metric_id(symbol: str) -> str:
    """Contract-safe metric id: ``USD/TWD`` becomes ``usd_twd``."""
    return symbol.lower().replace("/", "_")


def _metric_item(identifier: str, item: QuoteResult, block_id: str) -> MetricItem:
    return MetricItem(
        id=identifier,
        value=_quantize(item.close, block_precision(block_id), block_rounding(block_id)),
        change=_quantize(
            _previous_close_change(item.close, item.previous_close),
            block_precision(block_id),
            block_rounding(block_id),
        ),
        unit_code=item.currency.lower(),
    )


def _commodity_metric_item(
    identifier: str, item: EodResult, previous_close: Decimal | None
) -> MetricItem:
    return MetricItem(
        id=identifier,
        value=_quantize(
            item.close,
            block_precision("macro.commodities"),
            block_rounding("macro.commodities"),
        ),
        change=_quantize(
            _previous_close_change(item.close, previous_close),
            block_precision("macro.commodities"),
            block_rounding("macro.commodities"),
        ),
        unit_code=item.currency.lower(),
    )


def _completed_history_for_eod(bars: tuple[DailyBar, ...], eod: EodResult) -> tuple[DailyBar, ...]:
    """Cut mutable daily bars at the EOD date and reconcile the EOD close."""
    if any(bar.symbol != eod.symbol for bar in bars):
        raise DataSourceContractError("commodity history symbol did not match EOD symbol")
    completed = tuple(bar for bar in bars if bar.trade_date <= eod.as_of)
    latest = completed[-1] if completed else None
    if latest is None or latest.trade_date != eod.as_of or latest.close != eod.close:
        raise DataSourceContractError("commodity EOD date or close did not match daily history")
    if len(completed) < 2:
        raise DataSourceContractError("commodity history omitted the previous completed EOD date")
    previous = completed[-2]
    if previous.close is None or previous.close <= 0:
        raise DataSourceContractError("commodity history omitted a usable previous completed close")
    return completed


def _previous_close_change(close: Decimal, previous_close: Decimal | None) -> Decimal:
    """Percent move against the previous close; our own definition rather than
    the provider's undocumented percent_change."""
    if previous_close is None or previous_close <= 0:
        raise DataSourceContractError("quote has no usable previous close for the change")
    return (close - previous_close) / previous_close * Decimal(100)


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


def _normalized_common_date_points(
    bars: tuple[DailyBar, ...],
    dates: tuple[date, ...],
    *,
    precision: int = 4,
    rounding: str = "ROUND_HALF_EVEN",
) -> tuple[ChartPoint, ...]:
    closes = {bar.trade_date: bar.close for bar in bars}
    base = closes[dates[0]]
    if base is None or base == 0:
        return tuple(ChartPoint(x=str(item), value=None) for item in dates)
    return tuple(
        ChartPoint(
            x=str(item),
            value=_quantize(_normalize_close(closes[item], base), precision, rounding),
        )
        for item in dates
    )


def _latest_common_provider_dates(
    histories: tuple[tuple[DailyBar, ...], ...],
) -> tuple[date, ...]:
    common_dates = sorted(
        set.intersection(*(set(bar.trade_date for bar in history) for history in histories))
    )
    if len(common_dates) < 30:
        raise DataSourceContractError(
            "Twelve Data commodity histories have fewer than 30 common provider calendar dates"
        )
    return tuple(common_dates[-30:])


def _two_calendar_years_before(item: date) -> date:
    """Subtract calendar years while retaining February 29 when possible."""
    try:
        return item.replace(year=item.year - 2)
    except ValueError:
        return item.replace(year=item.year - 2, day=28)


def _commodity_ratio_window_dates(
    histories: tuple[tuple[DailyBar, ...], tuple[DailyBar, ...], tuple[DailyBar, ...]],
) -> tuple[date, ...]:
    common_dates = tuple(
        sorted(set.intersection(*(set(bar.trade_date for bar in history) for history in histories)))
    )
    if not common_dates:
        raise DataSourceContractError("commodity ratio histories have no common completed dates")
    latest = common_dates[-1]
    start = _two_calendar_years_before(latest)
    if any(history[0].trade_date > start for history in histories):
        raise DataSourceContractError(
            "commodity ratio histories do not reach the two-calendar-year start boundary"
        )
    window = tuple(item for item in common_dates if start <= item <= latest)
    if not window:
        raise DataSourceContractError("commodity ratio window has no common completed dates")
    if window[0] > start + timedelta(days=7):
        raise DataSourceContractError(
            "commodity ratio common completed dates begin more than seven days after the "
            "two-calendar-year start boundary"
        )
    return window


def _ratio_common_date_points(
    numerator: tuple[DailyBar, ...],
    denominator: tuple[DailyBar, ...],
    dates: tuple[date, ...],
    *,
    precision: int,
    rounding: str,
) -> tuple[ChartPoint, ...]:
    numerators = {bar.trade_date: bar.close for bar in numerator}
    denominators = {bar.trade_date: bar.close for bar in denominator}
    points: list[ChartPoint] = []
    for item in dates:
        top, bottom = numerators[item], denominators[item]
        if top is None or top <= 0 or bottom is None or bottom <= 0:
            raise DataSourceContractError("commodity ratio has no usable completed close")
        points.append(ChartPoint(x=str(item), value=_quantize(top / bottom, precision, rounding)))
    return tuple(points)


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
    return _error_blocks_for_keys(
        market_code,
        frozenset(key for dataset in _market_datasets(market_code) for key in (dataset.key,)),
    )


def _error_blocks_for_dataset(
    market_code: LaunchMarketCode, dataset_key: str
) -> tuple[ReportBlock, ...]:
    return _error_blocks_for_keys(market_code, frozenset((dataset_key,)))


def _error_blocks_for_keys(
    market_code: LaunchMarketCode, dataset_keys: frozenset[str]
) -> tuple[ReportBlock, ...]:
    market = next(
        market for market in ACTIVE_LAUNCH_MANIFEST.markets if market.market_code == market_code
    )
    blocks: list[ReportBlock] = []
    for block in market.blocks:
        if not set(block.datasets) & dataset_keys:
            continue
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
                    unit_code=block.unit_code,
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
            labels={block.id: _localized_block_text(block, locale) for block in blocks},
        )
    return PublicationBundle(content=content, presentations=presentations)


def _localized_block_text(block: ReportBlock, locale: Locale) -> LocalizedElementText:
    manifest_block = next(
        manifest_block
        for market in ACTIVE_LAUNCH_MANIFEST.markets
        for manifest_block in market.blocks
        if manifest_block.id == block.id
    )
    series_labels = (
        {
            series.id: manifest_block.series_labels.get(locale, {}).get(series.id, series.id)
            for series in block.series
        }
        if isinstance(block, SeriesBlock)
        else {}
    )
    ratio_unit_labels = {"zh-hant": "比率", "zh-hans": "比率", "en": "Ratio"}
    return LocalizedElementText(
        title=manifest_block.labels[locale],
        unit_label=(
            ratio_unit_labels[locale]
            if isinstance(block, SeriesBlock) and block.unit_code == "ratio"
            else None
        ),
        series_labels=series_labels,
    )


def _aggregate_provenance(
    values: tuple[Provenance, ...], *, as_of: date | None = None
) -> Provenance:
    material = "|".join(sorted(value.response_digest for value in values))
    return Provenance(
        provider="twelve_data",
        contract_version=values[0].contract_version,
        contract_hash=values[0].contract_hash,
        endpoint=values[0].endpoint,
        query_fingerprint=hashlib.sha256(material.encode()).hexdigest(),
        fetched_at=max(value.fetched_at for value in values),
        as_of=(
            as_of
            if as_of is not None
            else min(value.as_of for value in values if value.as_of is not None)
        ),
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
            "previous_close",
            "change",
            "percent_change",
        }
    ),
    "/time_series": frozenset({"datetime", "open", "high", "low", "close", "volume"}),
    "/eod": frozenset({"symbol", "exchange", "datetime", "close"}),
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


def _provider_lock_key(report_key: str, market_code: str, edition_date: date) -> int:
    """A namespace distinct from revision publishing's advisory lock."""
    material = f"scheduled-edition|{report_key}|{market_code}|{edition_date.isoformat()}".encode()
    value = int(hashlib.sha256(material).hexdigest()[:16], 16)
    return value - 2**64 if value >= 2**63 else value


def _scheduled_edition_lock_key(report_key: str, market_code: str, edition_date: date) -> int:
    """Compatibility name for the shared pre-provider lock.

    The scheduler used to own this namespace. Manual and queued reruns now
    deliberately use it too, so all Twelve Data callers serialize before a
    provider request.
    """
    return _provider_lock_key(report_key, market_code, edition_date)


def _has_active_lease(run: ReportPipelineRun, now: datetime) -> bool:
    if run.status != "running" or run.lease_expires_at is None:
        return False
    expires_at = run.lease_expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at > now
