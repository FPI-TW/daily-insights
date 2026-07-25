import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import Settings
from daily_insights_api.core.enums import SystemRole, UserStatus
from daily_insights_api.core.models import Base
from daily_insights_api.core.security import hash_password
from daily_insights_api.modules.identity.models import User
from daily_insights_api.modules.markets.catalog import MARKETS
from daily_insights_api.modules.markets.models import Market, OrganizationMarketPolicy
from daily_insights_api.modules.operations.models import ReportPipelineRun
from daily_insights_api.modules.operations.service import (
    PipelineBusyError,
    PipelineLeaseLostError,
    PipelineSpec,
    claim_pipeline_run,
    complete_source_run,
    publish_completed_run,
    start_source_run,
)
from daily_insights_api.modules.reports.contracts import (
    ChartData,
    ChartPoint,
    ChartSeries,
    Locale,
    LocalizedElementText,
    MetricValue,
    PresentationContract,
    PublicationBundle,
    PublicationContent,
)
from daily_insights_api.modules.tenancy.models import Membership, Organization
from daily_insights_api.web.app import create_app

pytestmark = pytest.mark.integration


@dataclass
class Phase2Harness:
    client: AsyncClient
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings
    organization_id: uuid.UUID
    member_id: uuid.UUID


async def ready() -> bool:
    return True


@pytest_asyncio.fixture
async def phase2_harness() -> AsyncIterator[Phase2Harness]:
    database_url = os.getenv("DAILY_INSIGHTS_TEST_DATABASE_URL") or os.getenv(
        "DAILY_INSIGHTS_DATABASE_URL"
    )
    if database_url is None:
        pytest.skip("DAILY_INSIGHTS_TEST_DATABASE_URL is required for integration tests")
    settings = Settings(
        environment="test",
        database_url=database_url,
        session_secret=SecretStr("phase2-test-session-secret"),
        password_pepper=SecretStr("phase2-test-password-pepper"),
        report_freshness_max_age_days=3,
    )
    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        await connection.run_sync(Base.metadata.create_all)

    organization_id = uuid.uuid4()
    member_id = uuid.uuid4()
    admin_id = uuid.uuid4()
    assert settings.password_pepper is not None
    pepper = settings.password_pepper.get_secret_value()
    async with session_factory.begin() as database:
        database.add_all(
            [
                Market(
                    code=market.code,
                    name_en=market.name_en,
                    name_zh_hant=market.name_zh_hant,
                    name_zh_hans=market.name_zh_hans,
                )
                for market in MARKETS
            ]
        )
        database.add(
            Organization(
                id=organization_id,
                name="Phase 2 Customer",
                slug="phase-2-customer",
                seat_limit=1,
            )
        )
        database.add_all(
            [
                User(
                    id=admin_id,
                    email="phase2-admin@example.com",
                    display_name="Phase 2 Admin",
                    password_hash=hash_password("AdminPassword123!", pepper),
                    must_change_password=False,
                    system_role=SystemRole.ADMIN,
                    status=UserStatus.ACTIVE,
                ),
                User(
                    id=member_id,
                    email="phase2-member@example.com",
                    display_name="Phase 2 Member",
                    password_hash=hash_password("MemberPassword123!", pepper),
                    must_change_password=False,
                    system_role=SystemRole.ORG_MEMBER,
                    status=UserStatus.ACTIVE,
                ),
            ]
        )
        await database.flush()
        database.add(
            Membership(
                organization_id=organization_id,
                user_id=member_id,
                joined_at=datetime.now(UTC),
            )
        )
        database.add(
            OrganizationMarketPolicy(
                organization_id=organization_id,
                market_code="crypto",
                is_visible=False,
                contract_reference="phase2-hidden-market",
                reason="Phase 2 tenant isolation test",
                changed_by_user_id=admin_id,
                changed_at=datetime.now(UTC),
            )
        )

    app = create_app(settings, ready, session_factory)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        login = await client.post(
            "/api/auth/login",
            json={
                "email": "phase2-member@example.com",
                "password": "MemberPassword123!",
            },
        )
        assert login.status_code == 200, login.text
        yield Phase2Harness(
            client=client,
            session_factory=session_factory,
            settings=settings,
            organization_id=organization_id,
            member_id=member_id,
        )

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    await engine.dispose()


def _presentation(locale: Locale, market_code: str) -> PresentationContract:
    return PresentationContract(
        schema_version="daily-presentation.v1",
        locale=locale,
        title=f"{market_code} {locale}",
        labels={
            "market.close": LocalizedElementText(title=f"Close {locale}", unit_label="USD"),
            "market.history": LocalizedElementText(
                title=f"History {locale}",
                unit_label="USD",
                series_labels={"close": f"Close {locale}"},
            ),
        },
    )


def _bundle(market_code: str, source_as_of: date) -> PublicationBundle:
    return PublicationBundle(
        content=PublicationContent(
            schema_version="daily-report.v1",
            market_code=market_code,
            as_of=source_as_of,
            metrics=(
                MetricValue(
                    id="market.close",
                    value=Decimal("123.4500"),
                    unit_code="usd",
                ),
            ),
            charts=(
                ChartData(
                    id="market.history",
                    unit_code="usd",
                    series=(
                        ChartSeries(
                            id="close",
                            points=(
                                ChartPoint(
                                    x=source_as_of.isoformat(),
                                    value=Decimal("123.4500"),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        presentations={
            "zh-hant": _presentation("zh-hant", market_code),
            "zh-hans": _presentation("zh-hans", market_code),
            "en": _presentation("en", market_code),
        },
    )


async def _publish(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    market_code: str,
    edition_date: date,
    source_as_of: date,
    revision: int = 1,
) -> uuid.UUID:
    spec = PipelineSpec(
        report_key="daily-market",
        market_code=market_code,
        edition_date=edition_date,
        revision=revision,
        derivation_version="synthetic-test.v1",
        content_schema_version="daily-report.v1",
    )
    now = datetime.now(UTC)
    async with session_factory.begin() as database:
        pipeline = await claim_pipeline_run(
            database,
            spec,
            lease_owner="phase2-test",
            now=now,
            lease_for=timedelta(minutes=5),
        )
        source = await start_source_run(
            database,
            pipeline_run_id=pipeline.id,
            lease_owner="phase2-test",
            lease_attempt=pipeline.attempt_count,
            provider="findb",
            dataset_key="daily-bars",
            attempt=1,
            contract_version="0.1.0",
            contract_hash="c" * 64,
            endpoint="/api/v1/serve/eod",
            request_fingerprint="a" * 64,
            now=now,
        )
        complete_source_run(
            source,
            source_as_of=source_as_of,
            fetched_at=now,
            record_count=1,
            payload_sha256="b" * 64,
            provider_request_id="synthetic-request",
            finished_at=now,
        )
        result = await publish_completed_run(
            database,
            pipeline_run_id=pipeline.id,
            lease_owner="phase2-test",
            lease_attempt=pipeline.attempt_count,
            bundle=_bundle(market_code, source_as_of),
            source_run_ids=(source.id,),
            required_dataset_keys=frozenset({"daily-bars"}),
            now=now,
        )
        assert result.publication is not None
        return result.publication.id


async def test_pipeline_claim_is_idempotent_across_concurrent_workers(
    phase2_harness: Phase2Harness,
) -> None:
    spec = PipelineSpec(
        report_key="daily-market",
        market_code="us_equity",
        edition_date=date(2026, 7, 24),
        revision=1,
        derivation_version="synthetic-test.v1",
        content_schema_version="daily-report.v1",
    )

    async def claim(worker: str) -> str:
        try:
            async with phase2_harness.session_factory.begin() as database:
                await claim_pipeline_run(
                    database,
                    spec,
                    lease_owner=worker,
                    now=datetime.now(UTC),
                    lease_for=timedelta(minutes=5),
                )
            return "claimed"
        except PipelineBusyError:
            return "busy"

    outcomes = await asyncio.gather(claim("worker-a"), claim("worker-b"))
    assert sorted(outcomes) == ["busy", "claimed"]
    async with phase2_harness.session_factory() as database:
        count = await database.scalar(select(func.count()).select_from(ReportPipelineRun))
    assert count == 1


async def test_expired_worker_is_fenced_after_another_worker_reclaims(
    phase2_harness: Phase2Harness,
) -> None:
    spec = PipelineSpec(
        report_key="daily-market",
        market_code="us_equity",
        edition_date=date(2026, 7, 25),
        revision=1,
        derivation_version="synthetic-test.v1",
        content_schema_version="daily-report.v1",
    )
    claimed_at = datetime(2026, 7, 25, tzinfo=UTC)
    async with phase2_harness.session_factory.begin() as database:
        first = await claim_pipeline_run(
            database,
            spec,
            lease_owner="worker-a",
            now=claimed_at,
            lease_for=timedelta(minutes=1),
        )
        first_attempt = first.attempt_count
        stale_source = await start_source_run(
            database,
            pipeline_run_id=first.id,
            lease_owner="worker-a",
            lease_attempt=first_attempt,
            provider="findb",
            dataset_key="daily-bars",
            attempt=1,
            contract_version="0.1.0",
            contract_hash="c" * 64,
            endpoint="/api/v1/serve/eod",
            request_fingerprint="a" * 64,
            now=claimed_at,
        )
        complete_source_run(
            stale_source,
            source_as_of=date(2026, 7, 24),
            fetched_at=claimed_at,
            record_count=1,
            payload_sha256="b" * 64,
            provider_request_id="stale-worker-request",
            finished_at=claimed_at,
        )
        pipeline_id = first.id
        stale_source_id = stale_source.id

    reclaimed_at = claimed_at + timedelta(minutes=2)
    async with phase2_harness.session_factory.begin() as database:
        second = await claim_pipeline_run(
            database,
            spec,
            lease_owner="worker-b",
            now=reclaimed_at,
            lease_for=timedelta(minutes=5),
        )
        assert second.attempt_count == first_attempt + 1

    async with phase2_harness.session_factory.begin() as database:
        with pytest.raises(PipelineLeaseLostError):
            await publish_completed_run(
                database,
                pipeline_run_id=pipeline_id,
                lease_owner="worker-a",
                lease_attempt=first_attempt,
                bundle=_bundle("us_equity", date(2026, 7, 24)),
                source_run_ids=(stale_source_id,),
                required_dataset_keys=frozenset({"daily-bars"}),
                now=reclaimed_at,
            )

    async with phase2_harness.session_factory() as database:
        current = await database.get(ReportPipelineRun, pipeline_id)
        assert current is not None
        assert current.status == "running"
        assert current.lease_owner == "worker-b"
        assert current.attempt_count == first_attempt + 1


async def test_publication_is_idempotent_and_incomplete_input_fails_closed(
    phase2_harness: Phase2Harness,
) -> None:
    source_as_of = date(2026, 7, 23)
    publication_id = await _publish(
        phase2_harness.session_factory,
        market_code="us_equity",
        edition_date=date(2026, 7, 24),
        source_as_of=source_as_of,
    )

    async with phase2_harness.session_factory.begin() as database:
        pipeline = await database.scalar(
            select(ReportPipelineRun).where(
                ReportPipelineRun.market_code == "us_equity",
                ReportPipelineRun.edition_date == date(2026, 7, 24),
            )
        )
        assert pipeline is not None
        repeated = await publish_completed_run(
            database,
            pipeline_run_id=pipeline.id,
            lease_owner="phase2-test",
            lease_attempt=pipeline.attempt_count,
            bundle=_bundle("us_equity", source_as_of),
            source_run_ids=(),
            required_dataset_keys=frozenset({"daily-bars"}),
            now=datetime.now(UTC),
        )
        assert repeated.publication is not None
        assert repeated.publication.id == publication_id
        assert repeated.created is False

    failed_spec = PipelineSpec(
        report_key="daily-market",
        market_code="hk_equity",
        edition_date=date(2026, 7, 24),
        revision=1,
        derivation_version="synthetic-test.v1",
        content_schema_version="daily-report.v1",
    )
    async with phase2_harness.session_factory.begin() as database:
        failed_pipeline = await claim_pipeline_run(
            database,
            failed_spec,
            lease_owner="phase2-test",
            now=datetime.now(UTC),
            lease_for=timedelta(minutes=5),
        )
        failed = await publish_completed_run(
            database,
            pipeline_run_id=failed_pipeline.id,
            lease_owner="phase2-test",
            lease_attempt=failed_pipeline.attempt_count,
            bundle=_bundle("hk_equity", source_as_of),
            source_run_ids=(),
            required_dataset_keys=frozenset({"daily-bars"}),
            now=datetime.now(UTC),
        )
        assert failed.publication is None
        assert failed.error_code == "incomplete_source_data"
    async with phase2_harness.session_factory() as database:
        stored_failed_pipeline = await database.scalar(
            select(ReportPipelineRun).where(
                ReportPipelineRun.market_code == "hk_equity",
            )
        )
        assert stored_failed_pipeline is not None
        assert stored_failed_pipeline.status == "failed"


async def test_correction_revision_can_reuse_the_same_source_inputs(
    phase2_harness: Phase2Harness,
) -> None:
    source_as_of = date(2026, 7, 23)
    await _publish(
        phase2_harness.session_factory,
        market_code="us_equity",
        edition_date=date(2026, 7, 24),
        source_as_of=source_as_of,
        revision=1,
    )
    await _publish(
        phase2_harness.session_factory,
        market_code="us_equity",
        edition_date=date(2026, 7, 24),
        source_as_of=source_as_of,
        revision=2,
    )
    async with phase2_harness.session_factory() as database:
        revisions = (
            await database.scalars(
                select(ReportPipelineRun.revision)
                .where(ReportPipelineRun.market_code == "us_equity")
                .order_by(ReportPipelineRun.revision)
            )
        ).all()
    assert revisions == [1, 2]


async def test_older_backfill_failure_does_not_stale_latest_publication(
    phase2_harness: Phase2Harness,
) -> None:
    today = date.today()
    await _publish(
        phase2_harness.session_factory,
        market_code="us_equity",
        edition_date=today,
        source_as_of=today,
    )
    failed_spec = PipelineSpec(
        report_key="daily-market",
        market_code="us_equity",
        edition_date=today - timedelta(days=1),
        revision=1,
        derivation_version="synthetic-test.v1",
        content_schema_version="daily-report.v1",
    )
    async with phase2_harness.session_factory.begin() as database:
        failed_pipeline = await claim_pipeline_run(
            database,
            failed_spec,
            lease_owner="backfill-worker",
            now=datetime.now(UTC),
            lease_for=timedelta(minutes=5),
        )
        await publish_completed_run(
            database,
            pipeline_run_id=failed_pipeline.id,
            lease_owner="backfill-worker",
            lease_attempt=failed_pipeline.attempt_count,
            bundle=_bundle("us_equity", today - timedelta(days=1)),
            source_run_ids=(),
            required_dataset_keys=frozenset({"daily-bars"}),
            now=datetime.now(UTC),
        )

    response = await phase2_harness.client.get("/api/reports/us_equity/latest")
    assert response.status_code == 200, response.text
    assert response.json()["stale"] is False
    assert response.json()["stale_reason"] is None


async def test_report_api_filters_hidden_market_localizes_and_marks_stale(
    phase2_harness: Phase2Harness,
) -> None:
    stale_as_of = date.today() - timedelta(days=4)
    visible_publication_id = await _publish(
        phase2_harness.session_factory,
        market_code="us_equity",
        edition_date=date.today(),
        source_as_of=stale_as_of,
    )
    await _publish(
        phase2_harness.session_factory,
        market_code="crypto",
        edition_date=date.today(),
        source_as_of=stale_as_of,
    )

    listed = await phase2_harness.client.get(
        "/api/reports",
        params={"locale": "en"},
        headers={"X-Organization-ID": str(uuid.uuid4())},
    )
    assert listed.status_code == 200, listed.text
    assert [report["market_code"] for report in listed.json()] == ["us_equity"]
    report = listed.json()[0]
    assert report["publication_id"] == str(visible_publication_id)
    assert report["locale"] == "en"
    assert report["presentation"]["locale"] == "en"
    assert report["content"]["metrics"][0]["value"] == "123.4500"
    assert report["stale"] is True
    assert report["stale_reason"] == "source_too_old"

    hidden = await phase2_harness.client.get("/api/reports/crypto/latest")
    assert hidden.status_code == 404
    unsupported_locale = await phase2_harness.client.get(
        "/api/reports/us_equity/latest",
        params={"locale": "fr"},
    )
    assert unsupported_locale.status_code == 422
