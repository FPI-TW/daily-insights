from dataclasses import replace
from pathlib import Path

import pytest

from daily_insights_api.modules.news.contracts import Candidate
from daily_insights_api.modules.news.editions import US_EQUITY_SPEC
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
    # Market relevance is a gate ahead of ranking, and the market tag must be
    # honest so the edition's own filter can drop off-market picks.
    assert "市場相關性是硬性門檻" in criteria.text
    assert "market_rule" in criteria.text
    # Five-star stories come first; lower ratings only fill what is left.
    assert "重要性是排序的第一鍵" in criteria.text
    assert len(criteria.digest) == 64
    assert "原始催化劑" in criteria.text
    assert "單一公司產品發表" in criteria.text
    assert "純即時價格走勢稿" in criteria.text
    assert "企業交易、支付科技" in criteria.text
    assert "互相獨立的全球宏觀主線" in criteria.text
    assert criteria.version == f"selection-v11:{criteria.digest[:12]}"


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


def test_market_policy_changes_edition_input_digest() -> None:
    candidate = Candidate(
        id="a" * 64,
        url="https://www.reuters.com/article",
        hostname="www.reuters.com",
        source_name="Reuters",
        headline="Intel shares surge after a material pricing change",
    )
    fetched = FetchedCandidate(candidate, str(candidate.url), "Body", "b" * 64)
    policy = US_EQUITY_SPEC.selection
    changed = replace(policy, market_focus=f"{policy.market_focus} Updated rule.")

    assert _digest([fetched], "deepseek-chat", "c" * 64, "us_equity", policy) != _digest(
        [fetched], "deepseek-chat", "c" * 64, "us_equity", changed
    )
