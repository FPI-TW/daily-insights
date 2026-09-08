import pytest
from pydantic import SecretStr, ValidationError

from daily_insights_api.core.config import MacroDashboardSchedulerSettings, Settings
from daily_insights_api.modules.assets.r2.store import R2ObjectStore
from daily_insights_api.web.app import create_app


def production_settings(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "environment": "production",
        "database_url": "postgresql+psycopg://app:secret@example.invalid/app",
        "session_secret": SecretStr("s" * 32),
        "password_pepper": SecretStr("p" * 32),
        "findb_base_url": "https://findb.example.invalid",
        "findb_api_key": SecretStr("findb-production-key"),
        "morning_reports_enabled": False,
        "r2_endpoint_url": "https://account.r2.cloudflarestorage.com",
        "r2_bucket_name": "daily-insights-production",
        "r2_access_key_id": SecretStr("r2-access-key"),
        "r2_secret_access_key": SecretStr("r2-secret-key"),
        "r2_signed_url_ttl_seconds": 900,
    }
    values.update(overrides)
    return values


def test_production_accepts_complete_external_configuration() -> None:
    settings = Settings.model_validate(production_settings())
    assert settings.environment == "production"
    assert settings.r2_signed_url_ttl_seconds == 900


def test_macro_scheduler_requires_only_a_production_database_url() -> None:
    settings = MacroDashboardSchedulerSettings.model_validate(
        {
            "environment": "production",
            "database_url": "postgresql+psycopg://app:secret@example.invalid/app",
        }
    )
    assert settings.database_url == "postgresql+psycopg://app:secret@example.invalid/app"

    with pytest.raises(ValidationError, match="database_url is required"):
        MacroDashboardSchedulerSettings(environment="production", database_url=None)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("r2_endpoint_url", "http://account.r2.cloudflarestorage.com"),
        ("r2_bucket_name", "CHANGE_ME_BUCKET"),
        ("r2_bucket_name", "Invalid_Bucket"),
        ("r2_access_key_id", None),
        ("r2_secret_access_key", SecretStr("development-only-secret")),
    ],
)
def test_production_rejects_insecure_or_partial_external_configuration(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(production_settings(**{field: value}))


def test_settings_repr_redacts_r2_and_provider_credentials() -> None:
    rendered = repr(Settings.model_validate(production_settings()))
    assert "findb-production-key" not in rendered
    assert "r2-access-key" not in rendered
    assert "r2-secret-key" not in rendered


def test_findb_is_not_a_three_market_production_requirement() -> None:
    settings = Settings.model_validate(
        production_settings(findb_base_url="http://unused.invalid", findb_api_key=None)
    )
    assert settings.findb_api_key is None


def test_morning_reports_accept_valid_twelve_data_configuration() -> None:
    settings = Settings.model_validate(
        production_settings(
            morning_reports_enabled=True,
            twelve_data_api_key=SecretStr("twelve-data-production-key"),
        )
    )
    assert settings.morning_reports_enabled is True


def test_analyst_viewpoints_require_a_valid_https_endpoint_and_key() -> None:
    settings = Settings.model_validate(
        production_settings(
            analyst_viewpoints_enabled=True,
            analyst_viewpoints_base_url="https://analyst.example.invalid",
            analyst_viewpoints_api_key=SecretStr("analyst-viewpoints-production-key"),
        )
    )
    assert settings.analyst_viewpoints_enabled is True

    with pytest.raises(ValidationError, match="analyst_viewpoints_api_key"):
        Settings.model_validate(
            production_settings(
                analyst_viewpoints_enabled=True,
                analyst_viewpoints_api_key=SecretStr(""),
            )
        )


@pytest.mark.parametrize("key", ["", "   \t"])
def test_enabled_morning_reports_rejects_an_unusable_provider_key(key: str) -> None:
    with pytest.raises(ValidationError, match="twelve_data_api_key is required"):
        Settings.model_validate(
            production_settings(
                morning_reports_enabled=True,
                twelve_data_api_key=SecretStr(key),
            )
        )


def test_production_runtime_composes_r2_adapter_without_exposing_credentials() -> None:
    settings = Settings.model_validate(production_settings())

    async def ready() -> bool:
        return True

    app = create_app(settings=settings, readiness_checker=ready)
    assert isinstance(app.state.object_store, R2ObjectStore)
    assert "r2-access-key" not in repr(app.state.object_store)
    assert "r2-secret-key" not in repr(app.state.object_store)
