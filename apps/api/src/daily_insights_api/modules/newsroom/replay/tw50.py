"""Taiwan 50 constituents, to tell large-cap stories from small-company ones.

Static list of the 50 stocks held by Yuanta Taiwan Top 50 ETF (0050), which
tracks the FTSE TWSE Taiwan 50 Index one for one. Source:
https://www.yuantaetfs.com/product/detail/0050/ratio (持股比重, 交易日期
2026/10/21, retrieved 2026-10-22). The index is reviewed every March, June,
September and December; refresh this list after each review.

Matching is by company name, common aliases and the four-digit code in a title.
It cannot tell a story about the company from one that only mentions it, and a
name that is also an everyday word is matched only in its unambiguous forms
(``統一企業``, ``創意電子``), so short forms of those are missed.
"""

import re
from dataclasses import dataclass
from functools import cache

DATA_DATE = "2026-10-21"
SOURCE_URL = "https://www.yuantaetfs.com/product/detail/0050/ratio"


@dataclass(frozen=True, slots=True)
class Constituent:
    code: str
    name: str
    # Regular expressions besides the name and the code.
    aliases: tuple[str, ...] = ()
    # Whether the plain name is matched; off for names that are everyday words.
    match_name: bool = True


CONSTITUENTS: tuple[Constituent, ...] = (
    Constituent("2330", "台積電", ("台積", r"\bTSMC\b")),
    Constituent("2454", "聯發科", (r"MediaTek",)),
    Constituent("2308", "台達電", (r"Delta Electronics",)),
    Constituent("2317", "鴻海", ("鴻海精密", r"Foxconn", r"Hon Hai")),
    Constituent("3711", "日月光投控", ("日月光", r"\bASE\b")),
    # "南亞" alone is also South Asia; 南亞科 is listed separately.
    Constituent("1303", "南亞", ("南亞塑膠", r"南亞(?![科洲地國各諸])"), match_name=False),
    Constituent("2303", "聯電", (r"\bUMC\b",)),
    Constituent(
        "2383",
        "台光電",
    ),
    Constituent(
        "3037",
        "欣興",
    ),
    Constituent(
        "2881",
        "富邦金",
    ),
    Constituent(
        "2891",
        "中信金",
    ),
    Constituent(
        "2882",
        "國泰金",
    ),
    Constituent(
        "3017",
        "奇鋐",
    ),
    Constituent("2887", "台新新光金", ("台新金", "新光金")),
    Constituent("2345", "智邦", (r"Accton",)),
    Constituent("2382", "廣達", (r"Quanta",)),
    Constituent("2327", "國巨", (r"Yageo",)),
    Constituent(
        "2885",
        "元大金",
    ),
    Constituent(
        "2884",
        "玉山金",
    ),
    Constituent(
        "2360",
        "致茂",
    ),
    Constituent("2357", "華碩", (r"\bAsus\b", r"\bASUS\b")),
    # "創意" is also "creativity".
    Constituent("3443", "創意", ("創意電子",), match_name=False),
    Constituent(
        "2886",
        "兆豐金",
    ),
    Constituent(
        "2883",
        "凱基金",
    ),
    Constituent(
        "2890",
        "永豐金",
    ),
    Constituent("3231", "緯創", (r"Wistron",)),
    Constituent("2412", "中華電", ("中華電信",)),
    Constituent("3008", "大立光", (r"Largan",)),
    Constituent("6669", "緯穎", (r"Wiwynn",)),
    Constituent(
        "3653",
        "健策",
    ),
    Constituent(
        "2408",
        "南亞科",
    ),
    Constituent(
        "2059",
        "川湖",
    ),
    Constituent("2301", "光寶科", ("光寶",)),
    Constituent("2344", "華邦電", ("華邦",)),
    Constituent(
        "2880",
        "華南金",
    ),
    Constituent(
        "2892",
        "第一金",
    ),
    Constituent("4958", "臻鼎", ("臻鼎-KY",)),
    Constituent(
        "2368",
        "金像電",
    ),
    # "統一" is also "unified"; 統一超 is not a constituent.
    Constituent("1216", "統一", ("統一企業",), match_name=False),
    Constituent("6446", "藥華藥", ("藥華",)),
    Constituent("3665", "貿聯", ("貿聯-KY",)),
    Constituent(
        "8046",
        "南電",
    ),
    Constituent("2449", "京元電子", ("京元電",)),
    Constituent("2395", "研華", (r"Advantech",)),
    Constituent(
        "5880",
        "合庫金",
    ),
    Constituent(
        "7769",
        "鴻勁",
    ),
    # 長榮航 (2618) is not a constituent.
    Constituent("2603", "長榮", ("長榮海運", r"長榮(?!航)"), match_name=False),
    Constituent(
        "4904",
        "遠傳",
    ),
    # "台灣大" also begins 台灣大學 and 台灣大跌.
    Constituent(
        "3045", "台灣大", ("台灣大哥大", r"台灣大(?![跌漲學型幅量規增減])"), match_name=False
    ),
    Constituent(
        "6505",
        "台塑化",
    ),
)


@cache
def _patterns() -> tuple[tuple[str, re.Pattern[str]], ...]:
    compiled: list[tuple[str, re.Pattern[str]]] = []
    for constituent in CONSTITUENTS:
        # A code, not an amount: "台積電(2330)" but not "營收2330億".
        parts = [rf"(?<![\d.,]){constituent.code}(?![\d.,億萬元%])", *constituent.aliases]
        if constituent.match_name:
            parts.append(re.escape(constituent.name))
        compiled.append((constituent.code, re.compile("|".join(f"(?:{part})" for part in parts))))
    return tuple(compiled)


def match_tw50(*texts: str) -> list[str]:
    """Codes of the constituents named in any of ``texts``, in list order."""
    return [
        code for code, pattern in _patterns() if any(pattern.search(text) for text in texts if text)
    ]
