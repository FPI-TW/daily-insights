"""Versioned, deploy-time news selection and headline-screen criteria resources."""

import hashlib
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

SELECTION_PROMPT_BASE_VERSION = "selection-v12"
MAX_SELECTION_CRITERIA_CHARS = 12_000
SCREEN_PROMPT_BASE_VERSION = "screen-v1"
MAX_SCREEN_CRITERIA_CHARS = 4_000


class SelectionCriteriaError(ValueError):
    """Raised when the deploy-time selection criteria cannot be used safely."""


@dataclass(frozen=True)
class SelectionCriteria:
    text: str
    digest: str
    version: str


# The screen prompt is versioned exactly like the selection prompt.
ScreenCriteria = SelectionCriteria


def _load_criteria(
    *, resource: str, path: Path | None, base_version: str, max_chars: int, label: str
) -> SelectionCriteria:
    try:
        raw = (
            path.read_text(encoding="utf-8")
            if path is not None
            else files(__package__).joinpath(resource).read_text(encoding="utf-8")
        )
    except (FileNotFoundError, OSError) as error:
        raise SelectionCriteriaError(f"news {label} criteria resource is unavailable") from error
    text = raw.strip()
    if not text:
        raise SelectionCriteriaError(f"news {label} criteria must not be empty")
    if len(text) > max_chars:
        raise SelectionCriteriaError(
            f"news {label} criteria must not exceed {max_chars} characters"
        )
    digest = hashlib.sha256(text.encode()).hexdigest()
    return SelectionCriteria(text=text, digest=digest, version=f"{base_version}:{digest[:12]}")


def load_selection_criteria(path: Path | None = None) -> SelectionCriteria:
    return _load_criteria(
        resource="selection_criteria.txt",
        path=path,
        base_version=SELECTION_PROMPT_BASE_VERSION,
        max_chars=MAX_SELECTION_CRITERIA_CHARS,
        label="selection",
    )


def load_screen_criteria(path: Path | None = None) -> ScreenCriteria:
    """Load the headline-screen criteria; its digest joins the edition input digest."""
    return _load_criteria(
        resource="screen_criteria.txt",
        path=path,
        base_version=SCREEN_PROMPT_BASE_VERSION,
        max_chars=MAX_SCREEN_CRITERIA_CHARS,
        label="screen",
    )
