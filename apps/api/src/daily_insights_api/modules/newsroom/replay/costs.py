"""What the calls a replay sends to a provider cost, and the round's running total.

Every call that misses the cache goes to the provider; its token usage (with
DeepSeek's cache-hit and cache-miss split of the input) is priced here and
appended to the round's ledger, ``<root>/ledger/<round>.jsonl``, so a round's
spending is the sum of its ledger whatever runs it took.

Prices are USD per 1M tokens as published by the providers, read on 2026-10-22:

- DeepSeek, https://api-docs.deepseek.com/quick_start/pricing: ``deepseek-flash``
  (DeepSeek-V4.1-Flash-1017; legacy model names are billed at the Flash price)
  input cache hit 0.006, cache miss 0.3, output 1.2 at peak; ``deepseek-v4-pro``
  0.044 / 1.32 / 3.96 at peak. Off-peak is half the peak price; peak hours are
  01:00-04:00 and 06:00-10:00 UTC Monday to Friday (Chinese public holidays,
  which are off-peak, are priced at peak here, so totals err high).
- OpenAI ``text-embedding-3-large``: input 0.13.

OpenCode Go bills a subscription allowance instead; each reply carries the
pay-as-you-go price of the call (``usage.opencode.list_cost``), and that list
cost is what the ledger records for it.
"""

import json
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PRICES_READ_ON = "2026-10-22"
OPENAI_PRICES_READ_ON = "2026-10-30"
OPENAI_PRICING_URL = "https://developers.openai.com/api/docs/models/gpt-6-luna"
DEEPSEEK_PRICING_URL = "https://api-docs.deepseek.com/quick_start/pricing"


@dataclass(frozen=True, slots=True)
class ChatPrice:
    cache_hit: float
    cache_miss: float
    output: float


# Peak prices, USD per 1M tokens.
OPENAI_LUNA = ChatPrice(cache_hit=0.01, cache_miss=0.1, output=0.5)
DEEPSEEK_FLASH = ChatPrice(cache_hit=0.006, cache_miss=0.3, output=1.2)
DEEPSEEK_PRO = ChatPrice(cache_hit=0.044, cache_miss=1.32, output=3.96)
EMBEDDING_PRICES = {"text-embedding-3-large": 0.13, "text-embedding-3-small": 0.02}
EMBEDDING_FALLBACK_PRICE = 0.13
OFF_PEAK_FACTOR = 0.5
# UTC hours [start, end) that are peak on weekdays.
PEAK_HOURS = ((1, 4), (6, 10))
MILLION = 1_000_000


def chat_price(model: str) -> ChatPrice:
    """The price list of the model that answered (``deepseek-chat`` bills as Flash)."""
    if model == "gpt-6-luna" or model.startswith("gpt-6-luna-"):
        return OPENAI_LUNA
    return DEEPSEEK_PRO if "pro" in model.lower() else DEEPSEEK_FLASH


def is_peak(moment: datetime) -> bool:
    utc = moment.astimezone(UTC)
    if utc.weekday() >= 5:
        return False
    return any(start <= utc.hour < end for start, end in PEAK_HOURS)


def chat_cost(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_hit_tokens: int | None,
    at: datetime,
    cache_write_tokens: int = 0,
) -> float:
    """USD of one chat call; an unknown cache split is priced as all misses."""
    price = chat_price(model)
    if model == "gpt-6-luna" or model.startswith("gpt-6-luna-"):
        hit = min(max(cache_hit_tokens or 0, 0), input_tokens)
        writes = min(max(cache_write_tokens, 0), input_tokens - hit)
        input_usd = (
            hit * price.cache_hit
            + writes * price.cache_miss * 1.25
            + (input_tokens - hit - writes) * price.cache_miss
        )
        output_usd = output_tokens * price.output
        if input_tokens > 272_000:
            input_usd *= 2
            output_usd *= 1.5
        return (input_usd + output_usd) / MILLION
    hit = min(cache_hit_tokens or 0, input_tokens)
    usd = (
        hit * price.cache_hit + (input_tokens - hit) * price.cache_miss
    ) / MILLION + output_tokens * price.output / MILLION
    return usd if is_peak(at) else usd * OFF_PEAK_FACTOR


def embedding_cost(model: str, tokens: int) -> float:
    return tokens * EMBEDDING_PRICES.get(model, EMBEDDING_FALLBACK_PRICE) / MILLION


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    at: str
    run: str
    kind: str
    stage: str
    model: str
    served_model: str | None
    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int | None
    usd: float
    failed: bool = False
    # OpenCode Go: the list cost the replies reported (then ``usd`` is that cost)
    # and the reasoning tokens inside ``output_tokens``.
    list_cost: float | None = None
    reasoning_tokens: int | None = None
    # Wall time of the call with its retries, the HTTP replies it took (two after a
    # repair turn) and the error that ended a failed call.
    latency_ms: int | None = None
    replies: int | None = None
    error: str | None = None


class Ledger:
    """Append-only JSON lines of every call that reached a provider in one round."""

    def __init__(self, path: Path, run: str) -> None:
        self.path = path
        self.run = run

    def add(self, entry: LedgerEntry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")

    def chat(
        self,
        *,
        stage: str,
        model: str,
        usages: Iterable[dict[str, Any]],
        failed: bool = False,
        latency_ms: int | None = None,
        error: str | None = None,
    ) -> LedgerEntry | None:
        """Price and record one chat call from the usage of each of its HTTP replies."""
        rows = list(usages)
        if not rows:
            return None
        at = datetime.now(UTC)
        input_tokens = sum(_int(row.get("prompt_tokens")) for row in rows)
        output_tokens = sum(_int(row.get("completion_tokens")) for row in rows)
        hits = [cache_hit_tokens(row) for row in rows]
        cache_hit = sum(_int(value) for value in hits) if any(v is not None for v in hits) else None
        served = next((str(row["model"]) for row in rows if row.get("model")), None)
        list_costs = [cost for cost in map(_list_cost, rows) if cost is not None]
        reasoning = [value for value in map(_reasoning_tokens, rows) if value is not None]
        list_cost = sum(list_costs) if list_costs else None
        entry = LedgerEntry(
            at=at.isoformat(timespec="seconds"),
            run=self.run,
            kind="llm",
            stage=stage,
            model=model,
            served_model=served,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_hit_tokens=cache_hit,
            usd=list_cost
            if list_cost is not None
            else sum(
                chat_cost(
                    str(row.get("model") or served or model),
                    input_tokens=_int(row.get("prompt_tokens")),
                    output_tokens=_int(row.get("completion_tokens")),
                    cache_hit_tokens=cache_hit_tokens(row),
                    cache_write_tokens=_int(
                        row.get("prompt_tokens_details", {}).get("cache_write_tokens")
                    )
                    if isinstance(row.get("prompt_tokens_details"), dict)
                    else 0,
                    at=at,
                )
                for row in rows
            ),
            failed=failed,
            list_cost=list_cost,
            reasoning_tokens=sum(reasoning) if reasoning else None,
            latency_ms=latency_ms,
            replies=len(rows),
            error=error,
        )
        self.add(entry)
        return entry

    def embedding(self, *, model: str, tokens: int) -> LedgerEntry:
        entry = LedgerEntry(
            at=datetime.now(UTC).isoformat(timespec="seconds"),
            run=self.run,
            kind="embed",
            stage="embed",
            model=model,
            served_model=None,
            input_tokens=tokens,
            output_tokens=0,
            cache_hit_tokens=None,
            usd=embedding_cost(model, tokens),
        )
        self.add(entry)
        return entry


def _int(value: Any) -> int:
    return value if isinstance(value, int) else 0


def cache_hit_tokens(usage: dict[str, Any]) -> int | None:
    """Cached input tokens of one reply: DeepSeek's own field, or the OpenAI one Go sends."""
    value = usage.get("prompt_cache_hit_tokens")
    if isinstance(value, int):
        return value
    details = usage.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    return cached if isinstance(cached, int) else None


def _list_cost(usage: dict[str, Any]) -> float | None:
    opencode = usage.get("opencode")
    cost = opencode.get("list_cost") if isinstance(opencode, dict) else None
    return float(cost) if isinstance(cost, int | float) and not isinstance(cost, bool) else None


def _reasoning_tokens(usage: dict[str, Any]) -> int | None:
    details = usage.get("completion_tokens_details")
    value = details.get("reasoning_tokens") if isinstance(details, dict) else None
    return value if isinstance(value, int) else None


def read_ledger(path: Path) -> list[LedgerEntry]:
    if not path.exists():
        return []
    return [
        LedgerEntry(**json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def ledger_total(entries: Iterable[LedgerEntry]) -> dict[str, Any]:
    rows = list(entries)
    return {
        "calls": len(rows),
        "input_tokens": sum(row.input_tokens for row in rows),
        "cache_hit_tokens": sum(row.cache_hit_tokens or 0 for row in rows),
        "output_tokens": sum(row.output_tokens for row in rows),
        "reasoning_tokens": sum(row.reasoning_tokens or 0 for row in rows),
        "usd": sum(row.usd for row in rows),
    }


def _percentile(values: list[int], share: float) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(share * (len(ordered) - 1)))]


def calls_markdown(
    entries: Iterable[LedgerEntry],
    groups: Mapping[str, str],
    *,
    timeout_seconds: float = 90.0,
) -> str:
    """Per group (runs whose name contains its pattern): latency, repairs, failures, cost."""
    rows = list(entries)
    lines = [
        "| 組 | 呼叫 | 延遲 p50 | p95 | 最長 | 超過逾時 | repair 回合 | 失敗 | 推理 tokens 平均"
        " | 輸出 tokens 平均 | list_cost |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name, pattern in groups.items():
        members = [row for row in rows if pattern in row.run and row.kind == "llm"]
        timed = [row.latency_ms for row in members if row.latency_ms is not None]
        if not members:
            lines.append(f"| {name} | 0 | — | — | — | — | — | — | — | — | — |")
            continue
        late = sum(value > timeout_seconds * 1000 for value in timed)
        repairs = sum((row.replies or 1) > 1 for row in members)
        failed = sum(row.failed for row in members)
        lines.append(
            f"| {name} | {len(members)}"
            + (
                f" | {_percentile(timed, 0.5) / 1000:.1f} s"
                f" | {_percentile(timed, 0.95) / 1000:.1f} s"
                f" | {max(timed) / 1000:.1f} s"
                if timed
                else " | — | — | —"
            )
            + f" | {late} | {repairs} | {failed}"
            f" | {statistics.fmean(row.reasoning_tokens or 0 for row in members):.0f}"
            f" | {statistics.fmean(row.output_tokens for row in members):.0f}"
            f" | {sum(row.usd for row in members):.4f} |"
        )
    return "\n".join(lines) + "\n"


def ledger_markdown(entries: Iterable[LedgerEntry]) -> str:
    """Spending per run and stage, and the round's total."""
    rows = list(entries)
    groups: dict[tuple[str, str], list[LedgerEntry]] = {}
    for row in rows:
        groups.setdefault((row.run, row.stage), []).append(row)
    lines = [
        "| run | 呼叫 | 次數 | 輸入 tokens | 其中快取命中 | 輸出 tokens | 美元 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for (run, stage), members in groups.items():
        total = ledger_total(members)
        lines.append(
            f"| {run} | {stage} | {total['calls']} | {total['input_tokens']}"
            f" | {total['cache_hit_tokens']} | {total['output_tokens']} | {total['usd']:.4f} |"
        )
    total = ledger_total(rows)
    lines.append(
        f"| 合計 | | {total['calls']} | {total['input_tokens']} | {total['cache_hit_tokens']}"
        f" | {total['output_tokens']} | {total['usd']:.4f} |"
    )
    return "\n".join(lines) + "\n"
