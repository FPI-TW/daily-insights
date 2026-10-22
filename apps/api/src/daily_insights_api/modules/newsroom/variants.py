"""Selection variants: the scoring constants and prompts one edition is built with.

Every tunable of triage scoring, the 08:00 assembly and the editor pass lives in
one ``SelectionVariant`` so a replay can build the same window under two
settings side by side, and production can roll back by changing one setting
(``DAILY_INSIGHTS_NEWSROOM_SELECTION_VARIANT``) instead of reverting code.

``BASELINE`` freezes the behaviour of commit eb6ad34 (triage v2, editor v3,
linear multi-source bonus). ``P0`` is the 2026-10-22 recalibration: in the
10/18-10/22 editions five stars nearly vanished, a running crisis (the Strait of
Hormuz attacks) was rated as one-source fragments, and small Taiwanese company
stories outranked it on the multi-source bonus that Taiwan's many outlets earn
easily.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

VariantName = Literal["baseline", "p0"]
SourceBonusMode = Literal["linear", "log2"]


@dataclass(frozen=True, slots=True)
class SelectionVariant:
    name: VariantName
    # Base score of a triage impact (1-5) before market directness scales it.
    impact_base: Mapping[int, int]
    # ``linear``: ``min(cap, step * (n - 1))``; ``log2``: ``min(cap, step * log2(n))``,
    # n being the event's distinct outlets (one outlet scores 0 either way).
    source_bonus_mode: SourceBonusMode
    source_bonus_step: Mapping[str, float]
    source_bonus_cap: Mapping[str, float]
    # Source weight is clamped into this range before it multiplies a score; None keeps it as set.
    weight_clamp: tuple[float, float] | None
    # Triage ``escalation == 2`` (a clear escalation) waives the no-new-facts penalty.
    escalation_waives_no_new_facts: bool
    # ``prompts/<name>.txt`` used by triage and by the editor pass.
    triage_prompt: str
    editor_prompt: str
    # The editor also receives each event's top impact, main story type, a feed
    # summary and top escalation, and answers a ``reason`` per rating.
    editor_extended_input: bool
    # More five-star picks than this in one market flag the edition for review
    # (all are still kept); None never flags.
    five_star_soft_cap: int | None


def _per_market(global_: float, tw_equity: float, us_equity: float) -> Mapping[str, float]:
    return MappingProxyType({"global": global_, "tw_equity": tw_equity, "us_equity": us_equity})


BASELINE = SelectionVariant(
    name="baseline",
    impact_base=MappingProxyType({1: 40, 2: 54, 3: 68, 4: 82, 5: 96}),
    source_bonus_mode="linear",
    source_bonus_step=_per_market(5.0, 5.0, 5.0),
    source_bonus_cap=_per_market(20.0, 20.0, 20.0),
    weight_clamp=None,
    escalation_waives_no_new_facts=False,
    triage_prompt="triage_v2",
    editor_prompt="editor_v3",
    editor_extended_input=False,
    five_star_soft_cap=None,
)

P0 = SelectionVariant(
    name="p0",
    # A wider gap between impact 4 and 5 (20 instead of 14) so a market-wide
    # story is not overtaken by a narrower one that merely has more outlets.
    impact_base=MappingProxyType({1: 30, 2: 45, 3: 62, 4: 80, 5: 100}),
    # Diminishing returns per outlet; Taiwan has a dozen outlets rewriting one
    # another, so its bonus is both smaller and capped lower.
    source_bonus_mode="log2",
    source_bonus_step=_per_market(6.0, 4.0, 6.0),
    source_bonus_cap=_per_market(15.0, 10.0, 15.0),
    weight_clamp=(0.9, 1.1),
    escalation_waives_no_new_facts=True,
    triage_prompt="triage",
    editor_prompt="editor",
    editor_extended_input=True,
    five_star_soft_cap=3,
)

VARIANTS: Mapping[VariantName, SelectionVariant] = MappingProxyType(
    {variant.name: variant for variant in (BASELINE, P0)}
)


def get_variant(name: VariantName) -> SelectionVariant:
    return VARIANTS[name]
