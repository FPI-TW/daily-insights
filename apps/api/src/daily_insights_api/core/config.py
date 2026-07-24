from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "staging", "production"]
LOCAL_DATABASE_URL = (
    "postgresql+psycopg://daily_insights:daily_insights@localhost:5432/daily_insights"
)
LOCAL_SESSION_SECRET = "development-only-session-secret-change-me"
LOCAL_PASSWORD_PEPPER = "development-only-password-pepper-change-me"


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
            insecure_markers = ("change_me", "change-me", "development-only")
            if any(
                marker in value.lower()
                for marker in insecure_markers
                for value in (session_secret, password_pepper)
            ):
                raise ValueError("deployment secrets must not use placeholders")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
