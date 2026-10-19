"""Compatibility of previously signed tickets with durable removal fencing."""

import base64
import uuid
from datetime import date

from pydantic import SecretStr

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.podcasts.direct_upload import UploadRequest
from daily_insights_api.modules.podcasts.synchronous_upload import (
    UploadTicket,
    _signature,
    decode_ticket,
)


def test_legacy_signed_ticket_without_generation_decodes_as_zero() -> None:
    settings = Settings(environment="test", session_secret=SecretStr("ticket-test-secret"))
    ticket = UploadTicket(
        actor_id=uuid.uuid4(),
        asset_id=uuid.uuid4(),
        trading_date=date(2026, 10, 1),
        reason="initial_upload",
        file=UploadRequest(
            locale="en",
            filename="podcast.mp3",
            size_bytes=5,
            mime_type="audio/mpeg",
            sha256="0" * 64,
        ),
        expires=9999999999,
        episode_version=None,
        began_published=False,
    )
    old_payload = base64.urlsafe_b64encode(
        ticket.model_dump_json(exclude={"generation"}).encode()
    ).decode()
    decoded = decode_ticket(f"{old_payload}.{_signature(settings, old_payload)}", settings)
    assert decoded.generation == 0
    assert decoded.asset_id == ticket.asset_id
