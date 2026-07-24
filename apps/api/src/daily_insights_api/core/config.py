import re
from functools import lru_cache
from typing import Literal, Self
from urllib.parse import urlparse

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "staging", "production"]
LOCAL_DATABASE_URL = (
    "postgresql+psycopg://daily_insights:daily_insights@localhost:5432/daily_insights"
)
LOCAL_SESSION_SECRET = "development-only-session-secret-change-me"
LOCAL_PASSWORD_PEPPER = "development-only-password-pepper-change-me"
PLACEHOLDER_MARKERS = ("change_me", "change-me", "development-only")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DAILY_INSIGHTS_",
        env_file=".env",
        extra="ignore",
    )

    environment: Environment = "development"
    database_url: str | None = None
    app_name: str = "Daily Insights API"
    session_secret: SecretStr | None = None
    password_pepper: SecretStr | None = None
    session_cookie_name: str = "daily_insights_session"
    session_ttl_seconds: int = Field(default=60 * 60 * 12, gt=0)
    login_rate_limit_attempts: int = Field(default=5, gt=0)
    login_rate_limit_window_seconds: int = Field(default=5 * 60, gt=0)
    trusted_proxy_cidrs: str = "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
    findb_base_url: str = "https://findb.tingfong.com"
    findb_api_key: SecretStr | None = None
    findb_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    findb_retry_attempts: int = Field(default=3, ge=1, le=10)
    report_freshness_max_age_days: int = Field(default=3, ge=1, le=30)
    r2_endpoint_url: str | None = None
    r2_bucket_name: str | None = None
    r2_access_key_id: SecretStr | None = None
    r2_secret_access_key: SecretStr | None = None
    r2_signed_url_ttl_seconds: int = Field(default=900, ge=60, le=3600)

    @model_validator(mode="after")
    def require_external_database_configuration(self) -> Self:
        if self.database_url is None:
            if self.environment not in {"development", "test"}:
                raise ValueError("database_url is required outside development and test")
            self.database_url = LOCAL_DATABASE_URL
        if self.session_secret is None:
            if self.environment not in {"development", "test"}:
                raise ValueError("session_secret is required outside development and test")
            self.session_secret = SecretStr(LOCAL_SESSION_SECRET)
        if self.password_pepper is None:
            if self.environment not in {"development", "test"}:
                raise ValueError("password_pepper is required outside development and test")
            self.password_pepper = SecretStr(LOCAL_PASSWORD_PEPPER)
        if self.environment in {"staging", "production"}:
            assert self.session_secret is not None
            assert self.password_pepper is not None
            session_secret = self.session_secret.get_secret_value()
            password_pepper = self.password_pepper.get_secret_value()
            if len(session_secret) < 32 or len(password_pepper) < 32:
                raise ValueError(
                    "session_secret and password_pepper must contain at least 32 characters"
                )
            if session_secret == password_pepper:
                raise ValueError("session_secret and password_pepper must be different")
            if any(
                marker in value.lower()
                for marker in PLACEHOLDER_MARKERS
                for value in (session_secret, password_pepper)
            ):
                raise ValueError("deployment secrets must not use placeholders")
            self._validate_production_external_services()
        return self

    def _validate_production_external_services(self) -> None:
        findb_url = urlparse(self.findb_base_url)
        if findb_url.scheme != "https" or not findb_url.netloc:
            raise ValueError("findb_base_url must be an absolute HTTPS URL")
        if self.findb_api_key is None or _is_placeholder(self.findb_api_key.get_secret_value()):
            raise ValueError("findb_api_key is required and cannot be a placeholder")

        required_r2_values = {
            "r2_endpoint_url": self.r2_endpoint_url,
            "r2_bucket_name": self.r2_bucket_name,
            "r2_access_key_id": (
                self.r2_access_key_id.get_secret_value()
                if self.r2_access_key_id is not None
                else None
            ),
            "r2_secret_access_key": (
                self.r2_secret_access_key.get_secret_value()
                if self.r2_secret_access_key is not None
                else None
            ),
        }
        missing = [name for name, value in required_r2_values.items() if not value]
        if missing:
            raise ValueError(f"missing mandatory R2 configuration: {', '.join(sorted(missing))}")
        endpoint = urlparse(self.r2_endpoint_url or "")
        if endpoint.scheme != "https" or not endpoint.netloc:
            raise ValueError("r2_endpoint_url must be an absolute HTTPS URL")
        for name, value in required_r2_values.items():
            if value is not None and _is_placeholder(value):
                raise ValueError(f"{name} cannot contain a placeholder")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.r2_bucket_name or ""):
            raise ValueError("r2_bucket_name must be a valid 3-63 character bucket name")


def _is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


@lru_cache
def get_settings() -> Settings:
    return Settings()
