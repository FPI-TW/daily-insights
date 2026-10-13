from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Literal

ProviderKey = Literal[
    "twelve_data",
    "yahoo_finance",
    "twse",
    "us_treasury",
    "new_york_fed",
    "internal_services",
]
JobKind = Literal["function", "projection"]
TriggerKind = Literal["automatic", "manual"]
DependencyPolicy = Literal["success", "terminal"]

REGISTRY_VERSION = "orchestration.v1"
DAILY_ROUTINE_KEY = "daily_market_update_v1"


@dataclass(frozen=True, slots=True)
class ProviderDefinition:
    key: ProviderKey
    display_name: str


@dataclass(frozen=True, slots=True)
class FunctionDefinition:
    key: str
    provider_key: ProviderKey
    freshness_days: int
    retryable: bool = True
    resources: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FunctionStep:
    function_key: str
    depends_on: tuple[str, ...] = ()
    dependency_policy: DependencyPolicy = "success"


@dataclass(frozen=True, slots=True)
class JobDefinition:
    key: str
    kind: JobKind
    triggers: tuple[TriggerKind, ...]
    functions: tuple[FunctionStep, ...] = ()
    projection_handler: str | None = None
    automatic_key: str | None = None
    deadline_policy: Literal["routine", "none"] = "none"


@dataclass(frozen=True, slots=True)
class JobDependencyDefinition:
    upstream_job_key: str
    downstream_job_key: str
    policy: DependencyPolicy = "success"


@dataclass(frozen=True, slots=True)
class RoutineDefinition:
    key: str
    job_keys: tuple[str, ...]
    dependencies: tuple[JobDependencyDefinition, ...]
    scheduled_hour: int
    deadline_hour: int
    timezone: str


PROVIDERS = (
    ProviderDefinition("twelve_data", "Twelve Data"),
    ProviderDefinition("yahoo_finance", "Yahoo Finance"),
    ProviderDefinition("twse", "TWSE"),
    ProviderDefinition("us_treasury", "U.S. Treasury"),
    ProviderDefinition("new_york_fed", "New York Fed"),
    ProviderDefinition("internal_services", "Internal Services"),
)

FUNCTIONS = (
    FunctionDefinition("commodity_daily_bars", "twelve_data", 3),
    FunctionDefinition("fx_daily_bars", "twelve_data", 3),
    FunctionDefinition("rates_proxy_daily_bars", "twelve_data", 3),
    FunctionDefinition("crypto_daily_bars", "twelve_data", 2),
    FunctionDefinition("us_mega_cap_daily_bars", "twelve_data", 4),
    FunctionDefinition("us_index_daily_bars", "yahoo_finance", 4),
    FunctionDefinition("hk_index_daily_bars", "yahoo_finance", 4),
    FunctionDefinition("cn_index_daily_bars", "yahoo_finance", 4),
    FunctionDefinition("dxy_daily_bars", "yahoo_finance", 4),
    FunctionDefinition("taiex_daily_bars", "twse", 4),
    FunctionDefinition("institutional_stock_flows", "twse", 4),
    FunctionDefinition("institutional_market_flows", "twse", 4),
    FunctionDefinition("treasury_yield_curve", "us_treasury", 4),
    FunctionDefinition("sofr_daily_rates", "new_york_fed", 4),
    FunctionDefinition(
        "news_global_refresh",
        "internal_services",
        1,
        resources=("news_feeds", "third_party_llm"),
    ),
    FunctionDefinition(
        "news_tw_equity_refresh",
        "internal_services",
        1,
        resources=("news_feeds", "third_party_llm"),
    ),
    FunctionDefinition(
        "news_us_equity_refresh",
        "internal_services",
        1,
        resources=("news_feeds", "third_party_llm"),
    ),
    FunctionDefinition(
        "news_publish",
        "internal_services",
        1,
        resources=("third_party_llm",),
    ),
    FunctionDefinition("analyst_viewpoints_sync", "internal_services", 1),
)


def _steps(*keys: str) -> tuple[FunctionStep, ...]:
    return tuple(FunctionStep(key) for key in keys)


JOBS = (
    JobDefinition(
        "twelve_data_daily_update",
        "function",
        ("automatic",),
        _steps(
            "commodity_daily_bars",
            "fx_daily_bars",
            "rates_proxy_daily_bars",
            "crypto_daily_bars",
            "us_mega_cap_daily_bars",
        ),
        automatic_key="twelve_data",
        deadline_policy="routine",
    ),
    JobDefinition(
        "yahoo_finance_daily_update",
        "function",
        ("automatic",),
        _steps(
            "us_index_daily_bars",
            "hk_index_daily_bars",
            "cn_index_daily_bars",
            "dxy_daily_bars",
        ),
        automatic_key="yahoo_finance",
        deadline_policy="routine",
    ),
    JobDefinition(
        "twse_daily_update",
        "function",
        ("automatic",),
        _steps(
            "taiex_daily_bars",
            "institutional_stock_flows",
            "institutional_market_flows",
        ),
        automatic_key="twse",
        deadline_policy="routine",
    ),
    JobDefinition(
        "us_treasury_daily_update",
        "function",
        ("automatic",),
        _steps("treasury_yield_curve"),
        automatic_key="us_treasury",
        deadline_policy="routine",
    ),
    JobDefinition(
        "new_york_fed_daily_update",
        "function",
        ("automatic",),
        _steps("sofr_daily_rates"),
        automatic_key="new_york_fed",
        deadline_policy="routine",
    ),
    JobDefinition(
        "internal_services_daily_update",
        "function",
        ("automatic",),
        (
            FunctionStep("news_global_refresh"),
            FunctionStep("news_tw_equity_refresh"),
            FunctionStep("news_us_equity_refresh"),
            FunctionStep(
                "news_publish",
                (
                    "news_global_refresh",
                    "news_tw_equity_refresh",
                    "news_us_equity_refresh",
                ),
                "terminal",
            ),
            FunctionStep("analyst_viewpoints_sync"),
        ),
        automatic_key="internal_services",
        deadline_policy="routine",
    ),
    JobDefinition(
        "news_daily_update",
        "function",
        ("manual",),
        (
            FunctionStep("news_global_refresh"),
            FunctionStep("news_tw_equity_refresh"),
            FunctionStep("news_us_equity_refresh"),
            FunctionStep(
                "news_publish",
                (
                    "news_global_refresh",
                    "news_tw_equity_refresh",
                    "news_us_equity_refresh",
                ),
                "terminal",
            ),
        ),
    ),
    JobDefinition(
        "market_reports_publish",
        "projection",
        ("automatic", "manual"),
        projection_handler="market_reports_publish",
        automatic_key="market_reports_publish",
        deadline_policy="routine",
    ),
    JobDefinition(
        "macro_dashboard_publish",
        "projection",
        ("automatic", "manual"),
        projection_handler="macro_dashboard_publish",
        automatic_key="macro_dashboard_publish",
        deadline_policy="routine",
    ),
    JobDefinition(
        "global_macro_refresh",
        "function",
        ("manual",),
        _steps(
            "commodity_daily_bars",
            "fx_daily_bars",
            "rates_proxy_daily_bars",
            "dxy_daily_bars",
            "treasury_yield_curve",
            "sofr_daily_rates",
        ),
    ),
    JobDefinition(
        "us_equity_refresh",
        "function",
        ("manual",),
        _steps("us_mega_cap_daily_bars", "us_index_daily_bars"),
    ),
    JobDefinition(
        "tw_equity_refresh",
        "function",
        ("manual",),
        _steps(
            "taiex_daily_bars",
            "institutional_stock_flows",
            "institutional_market_flows",
        ),
    ),
    JobDefinition(
        "news_global_refresh_job",
        "function",
        ("manual",),
        (
            FunctionStep("news_global_refresh"),
            FunctionStep("news_publish", ("news_global_refresh",), "terminal"),
        ),
    ),
    JobDefinition(
        "news_tw_equity_refresh_job",
        "function",
        ("manual",),
        (
            FunctionStep("news_tw_equity_refresh"),
            FunctionStep("news_publish", ("news_tw_equity_refresh",), "terminal"),
        ),
    ),
    JobDefinition(
        "news_us_equity_refresh_job",
        "function",
        ("manual",),
        (
            FunctionStep("news_us_equity_refresh"),
            FunctionStep("news_publish", ("news_us_equity_refresh",), "terminal"),
        ),
    ),
    JobDefinition(
        "news_publish_job",
        "function",
        ("manual",),
        _steps("news_publish"),
    ),
    JobDefinition(
        "analyst_viewpoints_refresh",
        "function",
        ("manual",),
        _steps("analyst_viewpoints_sync"),
    ),
)

DAILY_ROUTINE = RoutineDefinition(
    key=DAILY_ROUTINE_KEY,
    job_keys=(
        "twelve_data_daily_update",
        "yahoo_finance_daily_update",
        "twse_daily_update",
        "us_treasury_daily_update",
        "new_york_fed_daily_update",
        "internal_services_daily_update",
        "market_reports_publish",
        "macro_dashboard_publish",
    ),
    dependencies=(
        JobDependencyDefinition("twelve_data_daily_update", "market_reports_publish", "terminal"),
        JobDependencyDefinition("yahoo_finance_daily_update", "market_reports_publish", "terminal"),
        JobDependencyDefinition("us_treasury_daily_update", "market_reports_publish", "terminal"),
        JobDependencyDefinition("new_york_fed_daily_update", "market_reports_publish", "terminal"),
        JobDependencyDefinition("twelve_data_daily_update", "macro_dashboard_publish", "terminal"),
        JobDependencyDefinition(
            "yahoo_finance_daily_update", "macro_dashboard_publish", "terminal"
        ),
        JobDependencyDefinition("us_treasury_daily_update", "macro_dashboard_publish", "terminal"),
        JobDependencyDefinition("new_york_fed_daily_update", "macro_dashboard_publish", "terminal"),
    ),
    scheduled_hour=8,
    deadline_hour=10,
    timezone="Asia/Taipei",
)

PROVIDER_BY_KEY = {item.key: item for item in PROVIDERS}
FUNCTION_BY_KEY = {item.key: item for item in FUNCTIONS}
JOB_BY_KEY = {item.key: item for item in JOBS}

PROJECTION_HANDLERS = frozenset(("market_reports_publish", "macro_dashboard_publish"))
MANUAL_MARKET_JOB_KEYS = (
    "global_macro_refresh",
    "us_equity_refresh",
    "tw_equity_refresh",
)
ADMIN_TRIGGER_JOB_KEYS = (
    *MANUAL_MARKET_JOB_KEYS,
    "news_daily_update",
    "news_global_refresh_job",
    "news_tw_equity_refresh_job",
    "news_us_equity_refresh_job",
)


def _assert_acyclic(nodes: set[str], edges: list[tuple[str, str]]) -> None:
    incoming = {node: 0 for node in nodes}
    outgoing: dict[str, list[str]] = {node: [] for node in nodes}
    for upstream, downstream in edges:
        if upstream not in nodes or downstream not in nodes:
            raise ValueError("dependency references an unknown registry key")
        outgoing[upstream].append(downstream)
        incoming[downstream] += 1
    ready = [node for node, count in incoming.items() if count == 0]
    visited = 0
    while ready:
        node = ready.pop()
        visited += 1
        for downstream in outgoing[node]:
            incoming[downstream] -= 1
            if incoming[downstream] == 0:
                ready.append(downstream)
    if visited != len(nodes):
        raise ValueError("registry dependency graph contains a cycle")


def validate_registry() -> None:
    if len(PROVIDER_BY_KEY) != len(PROVIDERS):
        raise ValueError("provider keys must be unique")
    if len(FUNCTION_BY_KEY) != len(FUNCTIONS):
        raise ValueError("function keys must be unique")
    if len(JOB_BY_KEY) != len(JOBS):
        raise ValueError("job keys must be unique")
    for function in FUNCTIONS:
        if function.provider_key not in PROVIDER_BY_KEY:
            raise ValueError(f"unknown provider for {function.key}")
    for job in JOBS:
        if job.kind == "function":
            if not job.functions or job.projection_handler is not None:
                raise ValueError(f"function job {job.key} is not executable")
            keys = {step.function_key for step in job.functions}
            if len(keys) != len(job.functions) or not keys <= FUNCTION_BY_KEY.keys():
                raise ValueError(f"function job {job.key} has invalid functions")
            _assert_acyclic(
                keys,
                [
                    (dependency, step.function_key)
                    for step in job.functions
                    for dependency in step.depends_on
                ],
            )
        elif job.functions or job.projection_handler not in PROJECTION_HANDLERS:
            raise ValueError(f"projection job {job.key} is not executable")
        if "automatic" in job.triggers and job.automatic_key is None:
            raise ValueError(f"automatic job {job.key} needs an automatic key")
    automatic_provider_keys = [
        job.automatic_key for job in JOBS if job.kind == "function" and "automatic" in job.triggers
    ]
    if len(automatic_provider_keys) != len(set(automatic_provider_keys)):
        raise ValueError("automatic provider jobs must be unique by provider key")
    routine_jobs = set(DAILY_ROUTINE.job_keys)
    if not routine_jobs <= JOB_BY_KEY.keys():
        raise ValueError("routine references an unknown job")
    _assert_acyclic(
        routine_jobs,
        [
            (dependency.upstream_job_key, dependency.downstream_job_key)
            for dependency in DAILY_ROUTINE.dependencies
        ],
    )


def registry_snapshot() -> dict[str, object]:
    validate_registry()
    return {
        "version": REGISTRY_VERSION,
        "providers": [asdict(item) for item in PROVIDERS],
        "functions": [asdict(item) for item in FUNCTIONS],
        "jobs": [asdict(item) for item in JOBS],
        "routine": asdict(DAILY_ROUTINE),
    }


def registry_digest() -> str:
    payload = json.dumps(registry_snapshot(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


validate_registry()
