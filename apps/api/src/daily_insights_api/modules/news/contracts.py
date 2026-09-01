from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

Locale = Literal["zh-hant", "zh-hans", "en"]
NewsStatus = Literal["complete", "partial", "unavailable"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Candidate(StrictModel):
    id: str = Field(pattern=r"^[a-f0-9]{64}$")
    url: HttpUrl
    hostname: str
    source_name: str = Field(min_length=1, max_length=100)
    headline: str = Field(min_length=1, max_length=1000)
    seen_at: datetime | None = None


class SelectedCandidate(StrictModel):
    id: str = Field(pattern=r"^[a-f0-9]{64}$")
    topic: Literal["markets", "economy", "companies", "policy", "technology", "commodities"]
    event_key: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,79}$")
    market: Literal["global", "us", "asia", "china", "europe", "commodities", "crypto"]
    importance: int = Field(ge=1, le=5)


class Selection(StrictModel):
    selections: tuple[SelectedCandidate, ...] = Field(max_length=5)

    @model_validator(mode="after")
    def unique_event_and_diversity_checked(self) -> "Selection":
        ids = [item.id for item in self.selections]
        if len(ids) != len(set(ids)):
            raise ValueError("selected candidates must be unique")
        event_keys = [item.event_key for item in self.selections]
        if len(event_keys) != len(set(event_keys)):
            raise ValueError("selected candidates must have unique event keys")
        if len(self.selections) >= 3:
            topics = {item.topic for item in self.selections}
            markets = {item.market for item in self.selections}
            if len(topics) < 2:
                raise ValueError("three or more selections must cover at least two topics")
            if len(markets) < 2:
                raise ValueError("three or more selections must cover at least two markets")
        return self


class LocalizedSummary(StrictModel):
    headline: str = Field(min_length=1, max_length=1000)
    summary: str = Field(min_length=1, max_length=3000)
    numeric_facts: tuple[str, ...] = Field(default=(), max_length=20)
