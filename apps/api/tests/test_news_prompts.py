from pathlib import Path

import pytest

from daily_insights_api.modules.news.contracts import Candidate
from daily_insights_api.modules.news.extraction import FetchedCandidate
from daily_insights_api.modules.news.prompts import (
    MAX_SELECTION_CRITERIA_CHARS,
    SelectionCriteriaError,
    load_selection_criteria,
)
from daily_insights_api.modules.news.service import _digest


def test_packaged_selection_criteria_loads_with_version_and_digest() -> None:
    criteria = load_selection_criteria()
    assert "不得因新聞使用的語言" in criteria.text
    # The packaged criteria carry the dedupe and source-credibility rules but
    # defer every count to OUTPUT_CONTRACT so one file serves all editions.
    assert "event_key" in criteria.text
    assert "來源可信度" in criteria.text
    assert "OUTPUT_CONTRACT" in criteria.text
    assert "5 則" not in criteria.text
    assert len(criteria.digest) == 64
    assert criteria.version == f"selection-v5:{criteria.digest[:12]}"


@pytest.mark.parametrize("content", ["", "   \n\t"])
def test_selection_criteria_rejects_empty_content(tmp_path: Path, content: str) -> None:
    path = tmp_path / "selection_criteria.txt"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(SelectionCriteriaError, match="must not be empty"):
        load_selection_criteria(path)


def test_selection_criteria_rejects_missing_and_oversized_resources(tmp_path: Path) -> None:
    with pytest.raises(SelectionCriteriaError, match="unavailable"):
        load_selection_criteria(tmp_path / "missing.txt")
    oversized = tmp_path / "selection_criteria.txt"
    oversized.write_text("x" * (MAX_SELECTION_CRITERIA_CHARS + 1), encoding="utf-8")
    with pytest.raises(SelectionCriteriaError, match="must not exceed"):
        load_selection_criteria(oversized)


def test_prompt_content_changes_edition_input_digest(tmp_path: Path) -> None:
    first_path = tmp_path / "first.txt"
    second_path = tmp_path / "second.txt"
    first_path.write_text("Prefer cross-market impact.", encoding="utf-8")
    second_path.write_text("Prefer monetary policy impact.", encoding="utf-8")
    first = load_selection_criteria(first_path)
    same = load_selection_criteria(first_path)
    second = load_selection_criteria(second_path)
    candidate = Candidate(
        id="a" * 64,
        url="https://www.reuters.com/article",
        hostname="www.reuters.com",
        source_name="Reuters",
        headline="A market headline",
    )
    fetched = FetchedCandidate(
        candidate,
        str(candidate.url),
        "Original article body",
        "b" * 64,
    )
    assert _digest([fetched], "deepseek-chat", first.digest) == _digest(
        [fetched], "deepseek-chat", same.digest
    )
    assert _digest([fetched], "deepseek-chat", first.digest) != _digest(
        [fetched], "deepseek-chat", second.digest
    )
