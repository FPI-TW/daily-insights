"""A catalog's missing rows never establish ownership of another catalog's files."""

import base64
import uuid
from datetime import date

import pytest
from fastapi import HTTPException
from pydantic import SecretStr, ValidationError

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.podcasts.direct_upload import UploadRequest
from daily_insights_api.modules.podcasts.staging_paths import staging_prefix, staging_scope
from daily_insights_api.modules.podcasts.synchronous_upload import (
    UploadTicket,
    _signature,
    decode_ticket,
    encode_ticket,
)


def settings(**overrides: object) -> Settings:
    return Settings.model_validate(
        {
            "environment": "test",
            "database_url": "postgresql+psycopg://app:password@localhost/catalog_a",
            "session_secret": SecretStr("scope-test-secret"),
            "r2_endpoint_url": "https://account.r2.cloudflarestorage.com",
            "r2_bucket_name": "shared-audio",
            **overrides,
        }
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"environment": "development"},
        {"database_url": "postgresql+psycopg://app:password@localhost/catalog_b"},
        {"database_url": "postgresql+psycopg://app:password@other-host/catalog_a"},
        {"database_url": "postgresql+psycopg://app:password@localhost/catalog_a?options=x"},
        {"r2_endpoint_url": "https://other.r2.cloudflarestorage.com"},
        {"r2_bucket_name": "other-audio"},
        {"session_secret": SecretStr("other-scope-test-secret")},
    ],
)
def test_staging_namespace_isolates_deployment_identity(overrides: dict[str, object]) -> None:
    assert staging_scope(settings()) != staging_scope(settings(**overrides))


def test_database_password_rotation_preserves_scope_without_exposing_identity() -> None:
    original = settings()
    rotated = settings(database_url="postgresql+psycopg://app:rotated@localhost/catalog_a")
    assert staging_scope(original) == staging_scope(rotated)
    assert staging_prefix(original) == f"podcasts/staging/{staging_scope(original)}/"
    assert len(staging_scope(original)) == 64


def ticket_values() -> dict[str, object]:
    return {
        "actor_id": uuid.uuid4(),
        "asset_id": uuid.uuid4(),
        "trading_date": date(2026, 10, 23),
        "reason": "initial_upload",
        "file": UploadRequest(
            locale="en",
            filename="podcast.mp3",
            size_bytes=5,
            mime_type="audio/mpeg",
            sha256="0" * 64,
        ),
        "expires": 1792800000,
        "episode_version": None,
        "began_published": False,
    }


def test_v3_ticket_cannot_be_replayed_with_another_database_even_with_same_secret() -> None:
    owner = settings()
    ticket = UploadTicket.model_validate(
        {**ticket_values(), "version": 3, "staging_scope": staging_scope(owner)}
    )
    token = encode_ticket(ticket, owner)
    assert decode_ticket(token, owner) == ticket
    assert ticket.object_key.startswith(staging_prefix(owner))
    assert ticket.registered_key == "podcasts/2026/10/23/en/podcast_1.mp3"
    foreign = settings(database_url="postgresql+psycopg://app:password@localhost/catalog_b")
    with pytest.raises(HTTPException) as error:
        decode_ticket(token, foreign)
    assert error.value.status_code == 403


@pytest.mark.parametrize("version", [1, 2])
def test_old_wire_ticket_without_scope_still_decodes(version: int) -> None:
    owner = settings()
    ticket = UploadTicket.model_validate({**ticket_values(), "version": version})
    payload = base64.urlsafe_b64encode(
        ticket.model_dump_json(exclude={"staging_scope"}).encode()
    ).decode()
    decoded = decode_ticket(f"{payload}.{_signature(owner, payload)}", owner)
    assert decoded == ticket
    assert decoded.object_key.startswith("podcasts/direct/")


@pytest.mark.parametrize(
    "version,scope",
    [(3, None), (3, "invalid"), (1, "a" * 64), (2, "a" * 64)],
)
def test_ticket_requires_scope_only_for_v3(version: int, scope: str | None) -> None:
    with pytest.raises(ValidationError):
        UploadTicket.model_validate({**ticket_values(), "version": version, "staging_scope": scope})
