"""Validated shapes the model must return at each LLM stage (spec §6).

The model's JSON is untrusted: every stage parses it with ``model_validate`` and
treats a ``ValidationError`` as a retryable ``*_schema_invalid`` failure.
"""

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints

MarketCode = Literal["global", "tw_equity", "us_equity"]
Topic = Literal["markets", "economy", "companies", "policy", "technology", "commodities"]
StoryType = Literal[
    "official",
    "data",
    "catalyst",
    "price_action",
    "preview",
    "opinion",
    "press_release",
    "other",
]
Impact = Annotated[int, Field(ge=1, le=5)]
Directness = Annotated[int, Field(ge=0, le=4)]
Stars = Annotated[int, Field(ge=1, le=5)]
# 0 not a running story or no change, 1 a continuation, 2 a clear escalation.
Escalation = Annotated[int, Field(ge=0, le=2)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MarketDirectness(_Strict):
    """How directly the story bears on each market edition (0 unrelated, 4 its own subject)."""

    global_: Directness = Field(alias="global")
    tw_equity: Directness
    us_equity: Directness

    def as_dict(self) -> dict[str, int]:
        return {"global": self.global_, "tw_equity": self.tw_equity, "us_equity": self.us_equity}


class EventMatch(_Strict):
    """Attach the article to an existing candidate event (id from the prompt)."""

    match: str


class EventNew(_Strict):
    """Open a new event; the working title is admin-facing only."""

    new: ShortText


class EmbeddingTranslation(_Strict):
    """A non-Chinese headline and summary in Traditional Chinese, used only for embedding."""

    title: ShortText
    summary: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2_000)] = ""


def _lenient_date(value: object) -> object:
    """A ``YYYY-MM-DD`` string becomes a date; anything else reads as unknown."""
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


class TriageResult(_Strict):
    """Discrete judgements only; ``scoring.py`` turns them into market scores.

    The model no longer writes a 0-100 score or ``relevant``: asked for one
    number it bunched on two values and ignored the caps for price-action and
    opinion pieces, so both are now computed from these fields.
    """

    topic: Topic
    story_type: StoryType
    new_facts: bool
    impact: Impact
    directness: MarketDirectness
    # Required when the computed scores make the article relevant; ignored otherwise.
    event: EventMatch | EventNew | None = None
    # Answered from triage v3 on; earlier prompts and stored signals have none.
    escalation: Escalation = 0
    # The day the core fact happened or was announced (oldnews variant only);
    # unknown, unparsable or absent reads as None.
    event_date: Annotated[date | None, BeforeValidator(_lenient_date)] = None


REASON_MAX_CHARS = 200


def _lenient_reason(value: object) -> object:
    """A missing or overlong reason never fails the editor call: null reads as
    empty and the text is cut to ``REASON_MAX_CHARS``. Other types still fail."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()[:REASON_MAX_CHARS]
    return value


class EditorRating(_Strict):
    event_id: str
    stars: Stars
    # One short sentence; the extended editor input asks for it when the stars
    # and the triage impact differ by two or more.
    reason: Annotated[str, BeforeValidator(_lenient_reason)] = ""


class EditorResult(_Strict):
    ratings: list[EditorRating]


class DuplicateGroups(_Strict):
    """Candidate event ids that report the same core event, one list per story."""

    groups: list[list[str]] = Field(default_factory=list)


class PublishedMatch(_Strict):
    """A candidate that reports an event an earlier edition already carried."""

    event_id: str
    published_id: str
    # Whether the candidate adds facts the earlier edition did not have.
    new_facts: bool


class PublishedMatches(_Strict):
    matches: list[PublishedMatch] = Field(default_factory=list)


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
