import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest
from pydantic import ValidationError
from sqlalchemy import Table

from daily_insights_api.modules.operations.models import ReportPipelineRun, SourceRun
from daily_insights_api.modules.operations.service import (
    PipelineSpec,
    complete_source_run,
    evaluate_freshness,
    fail_source_run,
    pipeline_idempotency_key,
    sanitize_error_code,
    sanitize_error_detail,
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
from daily_insights_api.modules.reports.models import ReportPublication


def content() -> PublicationContent:
    return PublicationContent(
        schema_version="daily-report.v1",
        market_code="us_equity",
        as_of=date(2026, 7, 23),
        metrics=(MetricValue(id="market.close", value=Decimal("123.4500"), unit_code="usd"),),
        charts=(
            ChartData(
                id="market.history",
                unit_code="usd",
                series=(
                    ChartSeries(
                        id="close",
                        points=(ChartPoint(x="2026-07-23", value=Decimal("123.4500")),),
                    ),
                ),
            ),
        ),
    )


def presentation(locale: Locale) -> PresentationContract:
    return PresentationContract(
        schema_version="daily-presentation.v1",
        locale=locale,
        title=f"Report {locale}",
        labels={
            "market.close": LocalizedElementText(title="Close", unit_label="USD"),
            "market.history": LocalizedElementText(
                title="History",
                unit_label="USD",
                series_labels={"close": "Close"},
            ),
        },
    )


def bundle() -> PublicationBundle:
    return PublicationBundle(
        content=content(),
        presentations={
            "zh-hant": presentation("zh-hant"),
            "zh-hans": presentation("zh-hans"),
            "en": presentation("en"),
        },
    )


def test_publication_bundle_requires_exactly_three_locales() -> None:
    with pytest.raises(ValidationError, match="exactly zh-hant"):
        PublicationBundle(
            content=content(),
            presentations={
                "zh-hant": presentation("zh-hant"),
                "en": presentation("en"),
            },
        )


def test_presentation_references_every_content_and_series_id() -> None:
    invalid = presentation("en").model_copy(
        update={
            "labels": {
                "market.close": LocalizedElementText(title="Close", unit_label="USD"),
            }
        }
    )
    with pytest.raises(ValidationError, match="every metric and chart"):
        PublicationBundle(
            content=content(),
            presentations={
                "zh-hant": presentation("zh-hant"),
                "zh-hans": presentation("zh-hans"),
                "en": invalid,
            },
        )


def test_decimal_values_are_serialized_losslessly_and_locale_neutrally() -> None:
    publication = bundle()
    stored = publication.content_for_storage()
    metrics = cast(list[dict[str, object]], stored["metrics"])
    charts = cast(list[dict[str, object]], stored["charts"])
    series = cast(list[dict[str, object]], charts[0]["series"])
    points = cast(list[dict[str, object]], series[0]["points"])
    assert metrics[0]["value"] == "123.4500"
    assert points[0]["value"] == "123.4500"
    assert "title" not in metrics[0]


def test_domain_models_never_store_provider_raw_payloads() -> None:
    source_columns = set(SourceRun.__table__.columns.keys())
    forbidden = {"raw_payload", "raw_body", "response_body", "rows", "payload"}
    assert forbidden.isdisjoint(source_columns)
    assert {
        "request_fingerprint",
        "contract_hash",
        "endpoint",
        "pipeline_attempt",
        "payload_sha256",
        "source_as_of",
        "record_count",
        "contract_version",
    } <= source_columns
    assert "updated_at" not in ReportPublication.__table__.columns


def test_pipeline_idempotency_is_deterministic_and_version_scoped() -> None:
    spec = PipelineSpec(
        report_key="daily-market",
        market_code="us_equity",
        edition_date=date(2026, 7, 24),
        revision=1,
        derivation_version="unresolved.v1",
        content_schema_version="daily-report.v1",
    )
    assert pipeline_idempotency_key(spec) == pipeline_idempotency_key(spec)
    assert pipeline_idempotency_key(spec) != pipeline_idempotency_key(
        PipelineSpec(
            report_key=spec.report_key,
            market_code=spec.market_code,
            edition_date=spec.edition_date,
            revision=2,
            derivation_version=spec.derivation_version,
            content_schema_version=spec.content_schema_version,
        )
    )


def test_source_run_success_retains_only_metadata() -> None:
    source = SourceRun(
        pipeline_run_id=uuid.uuid4(),
        provider="findb",
        dataset_key="daily-observations",
        pipeline_attempt=1,
        attempt=1,
        contract_version="normalized.v1",
        contract_hash="c" * 64,
        endpoint="/api/v1/serve/eod",
        request_fingerprint="a" * 64,
        started_at=datetime(2026, 7, 24, tzinfo=UTC),
        status="running",
    )
    complete_source_run(
        source,
        source_as_of=date(2026, 7, 23),
        fetched_at=datetime(2026, 7, 24, 1, tzinfo=UTC),
        record_count=10,
        payload_sha256="b" * 64,
        provider_request_id="provider-123",
        finished_at=datetime(2026, 7, 24, 1, 0, 1, tzinfo=UTC),
    )
    assert source.status == "succeeded"
    assert source.record_count == 10
    assert source.payload_sha256 == "b" * 64


def test_source_failure_redacts_secrets_and_bounds_storage() -> None:
    source = SourceRun(
        pipeline_run_id=uuid.uuid4(),
        provider="findb",
        dataset_key="daily-observations",
        pipeline_attempt=1,
        attempt=1,
        contract_version="normalized.v1",
        contract_hash="c" * 64,
        endpoint="/api/v1/serve/eod",
        request_fingerprint="a" * 64,
        started_at=datetime(2026, 7, 24, tzinfo=UTC),
        status="running",
    )
    fail_source_run(
        source,
        error_code="UPSTREAM TIMEOUT!!!",
        error_detail="Authorization: Bearer secret-token\n" + "x" * 2_000,
        finished_at=datetime(2026, 7, 24, 1, tzinfo=UTC),
    )
    assert source.status == "failed"
    assert source.error_code == "upstream_timeout"
    assert source.error_detail is not None
    assert "secret-token" not in source.error_detail
    assert len(source.error_detail) == 1_000
    assert sanitize_error_code("***") == "unknown_error"
    assert sanitize_error_detail(None) is None


@pytest.mark.parametrize(
    "detail",
    [
        '{"api_key":"supersecret"}',
        'headers={"Authorization": "Bearer supersecret"}',
        "password='two words must disappear'",
        "request?token=supersecret&market=US",
        # Providers write the key name as prose in their own error text.
        "API key: supersecret",
        "API Key: supersecret",
        "Api-Key: supersecret",
    ],
)
def test_source_failure_redacts_structured_secrets(detail: str) -> None:
    sanitized = sanitize_error_detail(detail)
    assert sanitized is not None
    assert "supersecret" not in sanitized
    assert "two words must disappear" not in sanitized
    assert "[REDACTED]" in sanitized


@pytest.mark.parametrize(
    "detail",
    [
        "connection to postgresql://dbuser:hunter2@db.internal:5432/app failed",
        "postgresql+psycopg://dbuser:hunter2@host/db",
        "amqps://dbuser:hunter2@broker:5671",
    ],
)
def test_source_failure_redacts_a_connection_string_password(detail: str) -> None:
    """A DSN carries its password with no key name to match on.

    Driver errors quote the whole connection string back, and these details are
    read by an operator in the back office.
    """
    sanitized = sanitize_error_detail(detail)
    assert sanitized is not None
    assert "hunter2" not in sanitized
    assert "[REDACTED]" in sanitized
    # The user survives: it says which credential to rotate.
    assert "dbuser" in sanitized


@pytest.mark.parametrize(
    "detail",
    [
        "GET https://example.com/path returned 500",
        "GET https://docs.example.com:8443/guide returned 404",
        "mailto user@example.com bounced",
        "retry after 3:04@2026-09-11",
    ],
)
def test_source_failure_keeps_details_that_hold_no_secret(detail: str) -> None:
    # Over-redaction costs diagnosis: a port or an address is not a credential.
    assert sanitize_error_detail(detail) == detail


def publication(source_as_of: date, published_at: datetime) -> ReportPublication:
    return ReportPublication(
        id=uuid.uuid4(),
        pipeline_run_id=uuid.uuid4(),
        report_key="daily-market",
        market_code="us_equity",
        edition_date=source_as_of,
        revision=1,
        derivation_version="unresolved.v1",
        content_schema_version="daily-report.v1",
        input_digest="c" * 64,
        source_as_of=source_as_of,
        content={},
        presentations={"zh-hant": {}, "zh-hans": {}, "en": {}},
        published_at=published_at,
        created_at=published_at,
    )


def test_last_known_good_becomes_stale_by_age_or_failed_refresh() -> None:
    published_at = datetime(2026, 7, 24, 2, tzinfo=UTC)
    report = publication(date(2026, 7, 24), published_at)
    fresh = evaluate_freshness(
        report,
        now=datetime(2026, 7, 24, 12, tzinfo=UTC),
        maximum_age=timedelta(days=1),
    )
    assert fresh.status == "fresh"
    assert fresh.stale_reason is None

    failed = evaluate_freshness(
        report,
        now=datetime(2026, 7, 24, 12, tzinfo=UTC),
        maximum_age=timedelta(days=1),
        latest_failed_at=datetime(2026, 7, 24, 3, tzinfo=UTC),
    )
    assert failed.status == "stale"
    assert failed.stale_reason == "latest_refresh_failed"

    old = evaluate_freshness(
        report,
        now=datetime(2026, 7, 26, tzinfo=UTC),
        maximum_age=timedelta(days=1),
    )
    assert old.status == "stale"
    assert old.stale_reason == "source_too_old"


def test_pipeline_and_source_status_constraints_are_registered() -> None:
    pipeline_table = cast(Table, ReportPipelineRun.__table__)
    source_table = cast(Table, SourceRun.__table__)
    pipeline_checks = {
        str(constraint.sqltext)
        for constraint in pipeline_table.constraints
        if hasattr(constraint, "sqltext")
    }
    source_checks = {
        str(constraint.sqltext)
        for constraint in source_table.constraints
        if hasattr(constraint, "sqltext")
    }
    assert any("published" in check and "failed" in check for check in pipeline_checks)
    assert any("succeeded" in check and "failed" in check for check in source_checks)
