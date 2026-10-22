import uuid
from datetime import UTC, datetime

import pytest

from daily_insights_api.modules.newsroom.assembly import (
    _ArticleRow,
    dedupe_prompt,
    earnings_angles,
    is_earnings_title,
    same_earnings_release,
    title_companies,
)
from daily_insights_api.modules.newsroom.translation import load_prompt
from daily_insights_api.modules.newsroom.triage import load_triage_prompt
from daily_insights_api.modules.newsroom.variants import BASELINE, P0, P0B, VARIANTS


@pytest.mark.parametrize(
    ("title", "companies"),
    [
        ("台積電第三季獲利優於預期", {"tsmc"}),
        ("TSMC raises full-year guidance", {"tsmc"}),
        ("聯發科法說會 明年營收成長", {"mediatek"}),
        ("美超微上調財測", {"supermicro"}),
        ("超微第三季營收創高", {"amd"}),
        ("Super Micro beats on earnings", {"supermicro"}),
        ("Netflix第三季訂戶數優於預期", {"netflix"}),
        ("台達電上調全年財測", {"台達電"}),
        ("Acme Corp quarterly results top estimates", {"acme"}),
        # Two companies, or an unknown one leading a known one.
        ("輝達與台積電公布第三季財報", {"nvidia", "tsmc"}),
        ("Pilot與微軟合作建置資料中心", {"pilot", "microsoft"}),
        # No company: a country, a market or a theme.
        ("美國第三季GDP成長放緩", set()),
        ("美股財報季進入高峰", set()),
        ("AI伺服器需求強勁", set()),
    ],
)
def test_title_companies(title: str, companies: set[str]) -> None:
    assert title_companies(title) == companies


@pytest.mark.parametrize(
    ("title", "earnings"),
    [
        ("台積電第三季獲利優於預期", True),
        ("Acme Q3 revenue tops estimates", True),
        ("甲公司上調全年財測", True),
        ("甲公司法說會 資本支出上看300億美元", True),
        ("Acme earnings call: CEO sees demand into next year", True),
        # Monthly sales, previews and the company's other news are not the release.
        ("台積電9月營收創新高", False),
        ("台積電十月份合併營收年增", False),
        ("甲公司財報前瞻 市場關注毛利率", False),
        ("甲公司第三季財報前夕 市場聚焦資本支出", False),
        ("甲公司財報後遭股東訴訟", False),
        ("甲公司發表新款晶片", False),
        ("甲公司執行長離職", False),
        ("甲公司宣布收購乙公司", False),
    ],
)
def test_is_earnings_title(title: str, earnings: bool) -> None:
    assert is_earnings_title(title) is earnings


@pytest.mark.parametrize(
    ("title", "other", "types", "same"),
    [
        ("台積電第三季獲利優於預期", "台積電上調全年財測", ("data", "data"), True),
        ("Nvidia Q3 revenue beats", "輝達法說會 資本支出指引上修", ("data", "data"), True),
        ("台達電第三季毛利率創高", "台達電上調全年財測", ("data", "data"), True),
        # Another company's earnings.
        ("台積電第三季獲利優於預期", "聯發科第三季獲利優於預期", ("data", "data"), False),
        # The same company's other news.
        ("台積電第三季獲利優於預期", "台積電發表新製程", ("data", "data"), False),
        ("台積電第三季獲利優於預期", "台積電財報前瞻", ("data", "preview"), False),
        # Not mainly data.
        ("台積電第三季獲利優於預期", "台積電法說會 董事長談AI需求", ("data", "opinion"), False),
        ("台積電第三季獲利優於預期", "台積電上調全年財測", ("data", None), False),
        # A combined story naming two companies.
        ("輝達與台積電公布第三季財報", "台積電第三季獲利優於預期", ("data", "data"), False),
        # No company to compare.
        ("美股財報季第三季獲利成長", "美股財報季營收優於預期", ("data", "data"), False),
    ],
)
def test_same_earnings_release(
    title: str, other: str, types: tuple[str | None, str | None], same: bool
) -> None:
    assert same_earnings_release(title, other, *types) is same
    assert same_earnings_release(other, title, *reversed(types)) is same


def _rows(event_id: uuid.UUID, *story_types: str) -> list[_ArticleRow]:
    return [
        _ArticleRow(
            event_id=event_id,
            title=f"report {index}",
            source_id=uuid.uuid4(),
            source_name=f"source {index}",
            weight=1.0,
            trust_tier=2,
            market_scores={"us_equity": 80},
            has_body=True,
            story_type=story_type,
            first_seen_at=datetime(2026, 9, 29, tzinfo=UTC),
        )
        for index, story_type in enumerate(story_types)
    ]


def test_earnings_angles_read_each_event_s_main_story_type() -> None:
    results, guidance, chief, product = (uuid.uuid4() for _ in range(4))
    titles = {
        results: "台積電第三季獲利優於預期",
        guidance: "台積電上調全年財測",
        chief: "台積電法說會 董事長談AI需求",
        product: "台積電發表新製程",
    }
    articles = {
        # Price-action pieces do not count toward the main story type.
        results: _rows(results, "data", "price_action", "price_action"),
        guidance: _rows(guidance, "data", "opinion", "data"),
        chief: _rows(chief, "opinion", "opinion", "data"),
        product: _rows(product, "data"),
    }
    assert earnings_angles(titles, articles, results, [guidance, chief, product]) == [guidance]
    # A target that is not mainly data takes no angles.
    assert earnings_angles(titles, articles, chief, [results, guidance]) == []


def test_only_earnings_merge_appends_the_earnings_prompts() -> None:
    earnings = VARIANTS["earnings_merge"]
    triage = load_triage_prompt(variant=earnings)
    assert triage.version.startswith("triage-v4+triage_addendum_earnings:")
    assert triage.text.startswith(load_triage_prompt(variant=P0B).text)
    assert triage.text.endswith(load_prompt("triage_addendum_earnings"))
    text, version = dedupe_prompt(variant=earnings)
    assert version == "newsroom.dedupe.v2+dedupe_addendum_earnings"
    assert text == load_prompt("dedupe") + "\n\n" + load_prompt("dedupe_addendum_earnings")
    for variant in (BASELINE, P0, P0B):
        assert dedupe_prompt(variant=variant) == (load_prompt("dedupe"), "newsroom.dedupe.v2")
        assert not variant.earnings_merge_path


@pytest.mark.parametrize("name", ["triage_addendum_earnings", "dedupe_addendum_earnings"])
def test_earnings_addenda_state_the_rule_with_generic_examples(name: str) -> None:
    text = load_prompt(name)
    assert "earnings_merge" not in text  # the placeholder is gone
    for needle in ("同一家公司同一次財報", "財測", "主管", "新產品", "訴訟", "人事"):
        assert needle in text
    # Examples are generic; the replay window's own earnings must not leak in.
    for detail in ("Meta", "特斯拉", "Tesla", "臉書"):
        assert detail not in text
