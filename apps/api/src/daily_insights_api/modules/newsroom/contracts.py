"""Validated shapes the model must return at each LLM stage (spec §6).

The model's JSON is untrusted: every stage parses it with ``model_validate`` and
treats a ``ValidationError`` as a retryable ``*_schema_invalid`` failure.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

MarketCode = Literal["global", "tw_equity", "us_equity"]
Topic = Literal["markets", "economy", "companies", "policy", "technology", "commodities"]
Score = Annotated[int, Field(ge=0, le=100)]
Stars = Annotated[int, Field(ge=1, le=5)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MarketScores(_Strict):
    global_: Score = Field(alias="global")
    tw_equity: Score
    us_equity: Score

    def as_dict(self) -> dict[str, int]:
        return {"global": self.global_, "tw_equity": self.tw_equity, "us_equity": self.us_equity}


class EventMatch(_Strict):
    """Attach the article to an existing candidate event (id from the prompt)."""

    match: str


class EventNew(_Strict):
    """Open a new event; the working title is admin-facing only."""

    new: ShortText


class TriageResult(_Strict):
    relevant: bool
    topic: Topic
    market_scores: MarketScores
    # Required when relevant; ignored (and may be null) otherwise.
    event: EventMatch | EventNew | None = None


class EditorRating(_Strict):
    event_id: str
    stars: Stars


class EditorResult(_Strict):
    ratings: list[EditorRating]


class DuplicateGroups(_Strict):
    """Candidate event ids that report the same core event, one list per story."""

    groups: list[list[str]] = Field(default_factory=list)


class RelatedSymbol(_Strict):
    symbol: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
    kind: Literal["index", "equity", "fx", "commodity", "rate", "crypto"]
    label: ShortText


class MarketWhy(_Strict):
    market: MarketCode
    why: LongText


class AnalysisResult(_Strict):
    """zh-hant event analysis shared across markets, plus one "why" per market."""

    headline: ShortText
    summary: LongText
    related_symbols: list[RelatedSymbol] = Field(default_factory=list, max_length=8)
    why: list[MarketWhy] = Field(min_length=1)


class WhyResult(_Strict):
    """A single market's "why it matters" for an event added to another edition."""

    why: LongText


class TranslationResult(_Strict):
    headline: ShortText
    summary: LongText
    # Keyed by edition item id, so each market's "why" maps back to its row.
    why: dict[str, LongText] = Field(default_factory=dict)
