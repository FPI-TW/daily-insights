"""Deploy-time prompt text for podcast analysis."""

from functools import lru_cache
from importlib.resources import files


@lru_cache(maxsize=1)
def load_analysis_rules() -> tuple[str, ...]:
    """Editorial rules for titling a transcript, one rule per non-empty line."""
    raw = files(__package__).joinpath("analysis_rules.txt").read_text(encoding="utf-8")
    return tuple(line.strip() for line in raw.splitlines() if line.strip())
