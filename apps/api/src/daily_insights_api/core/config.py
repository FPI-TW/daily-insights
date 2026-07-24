from functools import lru_cache
from typing import Literal, Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "staging", "production"]
LOCAL_DATABASE_URL = (
    "postgresql+psycopg://daily_insights:daily_insights@localhost:5432/daily_insights"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DAILY_INSIGHTS_",
        env_file=".env",
        extra="ignore",
    )

    environment: Environment = "development"
    database_url: str | None = None
    app_name: str = "Daily Insights API"

    @model_validator(mode="after")
    def require_external_database_configuration(self) -> Self:
        if self.database_url is None:
            if self.environment not in {"development", "test"}:
                raise ValueError("database_url is required outside development and test")
            self.database_url = LOCAL_DATABASE_URL
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
