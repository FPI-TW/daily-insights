"""Versioned, deploy-time news selection criteria resources."""

import hashlib
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

SELECTION_PROMPT_BASE_VERSION = "selection-v7"
MAX_SELECTION_CRITERIA_CHARS = 12_000


class SelectionCriteriaError(ValueError):
    """Raised when the deploy-time selection criteria cannot be used safely."""


@dataclass(frozen=True)
class SelectionCriteria:
    text: str
    digest: str
    version: str


def load_selection_criteria(path: Path | None = None) -> SelectionCriteria:
    try:
        raw = (
            path.read_text(encoding="utf-8")
            if path is not None
            else files(__package__).joinpath("selection_criteria.txt").read_text(encoding="utf-8")
        )
    except (FileNotFoundError, OSError) as error:
        raise SelectionCriteriaError("news selection criteria resource is unavailable") from error
    text = raw.strip()
    if not text:
        raise SelectionCriteriaError("news selection criteria must not be empty")
    if len(text) > MAX_SELECTION_CRITERIA_CHARS:
        raise SelectionCriteriaError(
            f"news selection criteria must not exceed {MAX_SELECTION_CRITERIA_CHARS} characters"
        )
    digest = hashlib.sha256(text.encode()).hexdigest()
    return SelectionCriteria(
        text=text,
        digest=digest,
        version=f"{SELECTION_PROMPT_BASE_VERSION}:{digest[:12]}",
    )
