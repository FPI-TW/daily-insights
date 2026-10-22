import json
import re

import pytest

from daily_insights_api.modules.newsroom.assembly import editor_prompt_version
from daily_insights_api.modules.newsroom.contracts import EditorResult, TriageResult
from daily_insights_api.modules.newsroom.translation import load_prompt
from daily_insights_api.modules.newsroom.triage import load_triage_prompt
from daily_insights_api.modules.newsroom.variants import BASELINE, P0, P0B

# Stories of the replay windows and the golden set; anchors and rules describe
# kinds of stories, so none of these may appear, or the replay would grade the
# prompts on the stories they were written from.
REPLAY_STORY_DETAILS = (
    "荷姆茲",
    "Hormuz",
    "曼德",
    "Mandeb",
    "利雅德",
    "Riyadh",
    "15 艘",
    "15艘",
    "108",
    "110",
    "布蘭特",
    "Meta",
    "特斯拉",
    "Tesla",
    "聯茂",
)


@pytest.mark.parametrize("name", ["triage_p0b", "editor_p0b"])
def test_p0b_prompts_name_no_replay_story(name: str) -> None:
    text = load_prompt(name)
    for detail in REPLAY_STORY_DETAILS:
        assert detail not in text


def test_p0b_prompts_are_versioned_apart_from_p0() -> None:
    p0, p0b = load_triage_prompt(variant=P0), load_triage_prompt(variant=P0B)
    assert re.fullmatch(r"triage-v4:[0-9a-f]{12}", p0b.version)
    assert p0.version.startswith("triage-v3:")
    assert p0b.text != p0.text
    assert editor_prompt_version(variant=P0B) == "newsroom.editor.v5"
    assert editor_prompt_version(variant=P0) == "newsroom.editor.v4"
    assert load_prompt(P0B.editor_prompt) != load_prompt(P0.editor_prompt)
    # Baseline stays on its own frozen prompts.
    assert load_triage_prompt(variant=BASELINE).version.startswith("triage-v2:")
    assert editor_prompt_version(variant=BASELINE) == "newsroom.editor.v3"


def test_triage_v4_judges_escalation_by_effect_and_energy_by_supply() -> None:
    p0, p0b = load_triage_prompt(variant=P0).text, load_triage_prompt(variant=P0B).text
    # The two upgrade clauses of v3 are gone, replaced by one rule.
    for removed in ("「已經在新聞中」而降級", "若符合上述 5 的條件就給 5"):
        assert removed in p0
        assert removed not in p0b
    assert "升級依已發生的實際效果判斷" in p0b
    assert "不依措辭或可能性判斷" in p0b
    energy = p0b.split("### 能源與航運", 1)[1].split("## 延續中的局勢", 1)[0]
    for rule in (
        "暫停通行",
        "受影響的產量或出口量",
        "通行中止",
        "最後通牒",
        "軍事部署",
        "可能擴及",
    ):
        assert rule in energy
    levels = [line[:3] for line in energy.splitlines() if re.match(r"- [345]", line)]
    assert levels == ["- 5", "- 4", "- 3"]


def test_triage_v4_keeps_the_other_five_point_definitions() -> None:
    p0, p0b = load_triage_prompt(variant=P0).text, load_triage_prompt(variant=P0B).text
    for kept in (
        "會改變整體市場對利率、成長、通膨或匯率的預期",
        "或改變大盤最大權值股的基本面",
        "央行利率決策、關鍵通膨或就業數據、最大權值股財報大幅偏離預期、重大軍事升級或全面性貿易限制",
    ):
        assert kept in p0
        assert kept in p0b
    # Everything above the impact scale and from the market directness on is v3's.
    impact, directness = "## 影響程度", "## 與各市場的直接程度"
    assert p0.split(impact)[0] == p0b.split(impact)[0]
    assert p0.split(directness)[1] == p0b.split(directness)[1]


def test_editor_v5_caps_energy_stars_without_supply_and_price() -> None:
    p0b = load_prompt(P0B.editor_prompt)
    assert "max_impact 為 5 不代表自動給 5 星" in p0b
    assert "應視為重要證據" not in p0b
    for rule in ("必須同時具備兩項", "只具備其中一項者", "最多 3 星", "單日漲跌約 5%"):
        assert rule in p0b
    for needle in ("JSON", "ratings", "event_id", "stars", "reason", "global", "tw_equity"):
        assert needle in p0b


def test_triage_v4_examples_parse_with_the_triage_contract() -> None:
    text = load_triage_prompt(variant=P0B).text
    examples = [line for line in text.splitlines() if line.startswith('{"story_type"')]
    assert len(examples) == 5
    for line in examples:
        assert TriageResult.model_validate(json.loads(line)).event_date is None


def test_p0b_answers_read_like_p0_answers() -> None:
    answer = {
        "story_type": "catalyst",
        "new_facts": True,
        "impact": 4,
        "escalation": 1,
        "directness": {"global": 4, "tw_equity": 3, "us_equity": 2},
        "topic": "commodities",
        "event": {"new": "油輪遇襲"},
    }
    triage = TriageResult.model_validate(answer)
    assert (triage.impact, triage.escalation) == (4, 1)
    rated = EditorResult.model_validate(
        {"ratings": [{"event_id": "e1", "stars": 3, "reason": "威脅性談話最多三星"}]}
    )
    assert rated.ratings[0].stars == 3
