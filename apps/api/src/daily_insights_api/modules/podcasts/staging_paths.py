"""Bind automatic staging cleanup to one deployment's storage and catalog."""

import hashlib
import hmac
import json

from sqlalchemy.engine import URL, make_url

from daily_insights_api.core.config import Settings


def staging_scope(settings: Settings) -> str:
    # Password rotation must not change catalog identity. Distinct
    # databases must never infer ownership from each other's missing rows.
    assert settings.database_url is not None
    assert settings.session_secret is not None
    configured_database = make_url(settings.database_url)
    database = URL.create(
        configured_database.drivername,
        username=configured_database.username,
        host=configured_database.host,
        port=configured_database.port,
        database=configured_database.database,
        query=configured_database.query,
    )
    identity = json.dumps(
        [
            settings.environment,
            database.render_as_string(hide_password=True),
            settings.r2_endpoint_url,
            settings.r2_bucket_name,
        ],
        separators=(",", ":"),
    )
    return hmac.new(
        settings.session_secret.get_secret_value().encode(),
        b"podcast-staging-v3:" + identity.encode(),
        hashlib.sha256,
    ).hexdigest()


def staging_prefix(settings: Settings) -> str:
    return f"podcasts/staging/{staging_scope(settings)}/"
