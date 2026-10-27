#!/usr/bin/env python3
from __future__ import annotations

import ipaddress
import json
import sys
from pathlib import Path
from typing import Any

EXPECTED_PROXY_NETWORK = "172.30.0.0/24"
SERVICES = (
    "api",
    "web",
    "nginx",
    "orchestration-worker",
    "orchestration-dispatcher",
    "newsroom-worker",
    "browser-egress",
    "cloakbrowser",
)
API_ENVIRONMENT_KEYS = {
    "DAILY_INSIGHTS_ANALYST_VIEWPOINTS_API_KEY",
    "DAILY_INSIGHTS_ANALYST_VIEWPOINTS_BASE_URL",
    "DAILY_INSIGHTS_ANALYST_VIEWPOINTS_ENABLED",
    "DAILY_INSIGHTS_ANALYST_VIEWPOINTS_TIMEOUT_SECONDS",
    "DAILY_INSIGHTS_DATABASE_URL",
    "DAILY_INSIGHTS_ENVIRONMENT",
    "DAILY_INSIGHTS_MORNING_REPORTS_ENABLED",
    "DAILY_INSIGHTS_NEWSROOM_ENABLED",
    "DAILY_INSIGHTS_NEWS_BLOCKED_HOSTNAMES",
    "DAILY_INSIGHTS_NEWS_IGNORE_ROBOTS",
    "DAILY_INSIGHTS_TWELVE_DATA_API_KEY",
    "DAILY_INSIGHTS_TWELVE_DATA_BASE_URL",
    "DAILY_INSIGHTS_YFINANCE_ENABLED",
    "DAILY_INSIGHTS_PASSWORD_PEPPER",
    "DAILY_INSIGHTS_R2_ACCESS_KEY_ID",
    "DAILY_INSIGHTS_R2_BUCKET_NAME",
    "DAILY_INSIGHTS_R2_ENDPOINT_URL",
    "DAILY_INSIGHTS_R2_SECRET_ACCESS_KEY",
    "DAILY_INSIGHTS_R2_SIGNED_URL_TTL_SECONDS",
    "DAILY_INSIGHTS_SESSION_SECRET",
    "DAILY_INSIGHTS_TRADINGVIEW_ENABLED",
    "DAILY_INSIGHTS_TRUSTED_PROXY_CIDRS",
    "DAILY_INSIGHTS_TWSE_ENABLED",
}

# Removed with the legacy daily-news pipeline; no service may still receive them.
REMOVED_ENVIRONMENT_KEYS = {
    "DAILY_INSIGHTS_DAILY_NEWS_ENABLED",
    "DAILY_INSIGHTS_MODEL_PROVIDER",
    "DAILY_INSIGHTS_MODEL_NAME",
    "DAILY_INSIGHTS_MODEL_API_BASE_URL",
    "DAILY_INSIGHTS_MODEL_TIMEOUT_SECONDS",
}
# The newsroom's DeepSeek key still lives in the legacy news GitHub Secret. The
# release workflow maps it to DAILY_INSIGHTS_NEWSROOM_LLM_API_KEY, so no service
# receives it under the Secret's own name.
LEGACY_LLM_SECRET_NAME = "DAILY_INSIGHTS_NEWS_MODEL_API_KEY"
# The orchestration worker runs the 08:00 newsroom_assemble function.
NEWSROOM_ASSEMBLY_KEYS = (
    "DAILY_INSIGHTS_NEWSROOM_ENABLED",
    "DAILY_INSIGHTS_NEWSROOM_LLM_BASE_URL",
    "DAILY_INSIGHTS_NEWSROOM_LLM_API_KEY",
    "DAILY_INSIGHTS_NEWSROOM_EDITOR_MODEL",
    "DAILY_INSIGHTS_NEWSROOM_LLM_USER_AGENT",
    "DAILY_INSIGHTS_NEWSROOM_LLM_SESSION_HEADER",
    "DAILY_INSIGHTS_NEWSROOM_LLM_EXTRA_BODY",
    "DAILY_INSIGHTS_NEWSROOM_EDITOR_EXTRA_BODY",
    "DAILY_INSIGHTS_NEWSROOM_EDITOR_TIMEOUT_SECONDS",
    "DAILY_INSIGHTS_NEWSROOM_SLACK_WEBHOOK_URL",
    "DAILY_INSIGHTS_NEWSROOM_ADMIN_BASE_URL",
)
# Ingestion settings are read only by the newsroom worker (and the blocked-host
# kill switch by the API, for admin manual URLs).
NEWSROOM_INGESTION_KEYS = {
    "DAILY_INSIGHTS_NEWS_EXTRA_HOSTNAMES",
    "DAILY_INSIGHTS_NEWS_FETCH_TIMEOUT_SECONDS",
    "DAILY_INSIGHTS_NEWS_DISCOVERY_TIMEOUT_SECONDS",
    "DAILY_INSIGHTS_GUARDIAN_API_KEY",
    "DAILY_INSIGHTS_SEC_CONTACT_EMAIL",
}
NEWSROOM_WORKER_KEYS = {
    "DAILY_INSIGHTS_ENVIRONMENT",
    "DAILY_INSIGHTS_RUNTIME_ROLE",
    "DAILY_INSIGHTS_DATABASE_URL",
    *NEWSROOM_ASSEMBLY_KEYS,
    "DAILY_INSIGHTS_NEWSROOM_TRIAGE_MODEL",
    "DAILY_INSIGHTS_NEWSROOM_ANALYSIS_MODEL",
    "DAILY_INSIGHTS_NEWSROOM_TRANSLATE_MODEL",
    "DAILY_INSIGHTS_NEWSROOM_EMBEDDING_BASE_URL",
    "DAILY_INSIGHTS_NEWSROOM_EMBEDDING_API_KEY",
    "DAILY_INSIGHTS_NEWSROOM_EMBEDDING_MODEL",
    "DAILY_INSIGHTS_NEWS_BLOCKED_HOSTNAMES",
    # Shared with the API, which applies it to admin manual URLs.
    "DAILY_INSIGHTS_NEWS_IGNORE_ROBOTS",
    "DAILY_INSIGHTS_NEWSROOM_BROWSER_CDP_URL",
    *NEWSROOM_INGESTION_KEYS,
}
BROWSER_CDP_URL = "http://cloakbrowser:9222"
CLOAKBROWSER_PROXY_ARGS = {
    "--proxy-server=http://browser-egress:3128",
    "--proxy-bypass-list=<-loopback>",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def service_networks(service: dict[str, Any]) -> set[str]:
    return set(service.get("networks") or {})


def validate_browser_isolation(model: dict[str, Any]) -> None:
    """CloakBrowser may reach the internet only through browser-egress."""
    services = model["services"]
    require(
        model.get("networks", {}).get("browser", {}).get("internal") is True,
        "the browser network must be internal so CloakBrowser has no direct route out",
    )
    cloakbrowser = services["cloakbrowser"]
    require(
        service_networks(cloakbrowser) == {"browser"},
        "cloakbrowser must sit only on the internal browser network",
    )
    command = [str(item) for item in cloakbrowser.get("command") or []]
    require(
        command[:1] == ["cloakserve"] and CLOAKBROWSER_PROXY_ARGS.issubset(command),
        "cloakbrowser must send every request, loopback included, through browser-egress",
    )
    require(
        not any(key.startswith("DAILY_INSIGHTS_") for key in cloakbrowser.get("environment", {})),
        "cloakbrowser must not receive application settings",
    )
    egress = services["browser-egress"]
    require(
        egress.get("command")
        == ["python", "-m", "daily_insights_api.scripts.run_browser_egress_proxy"],
        "browser-egress must run the public-HTTPS-only egress proxy",
    )
    require(
        not egress.get("environment"),
        "browser-egress must not receive application settings",
    )
    require(
        service_networks(egress) == {"app", "browser"},
        "browser-egress must bridge only the app and browser networks",
    )
    on_browser = {
        name for name, service in services.items() if "browser" in service_networks(service)
    }
    require(
        on_browser == {"cloakbrowser", "browser-egress", "newsroom-worker"},
        "only cloakbrowser, browser-egress and newsroom-worker may join the browser network",
    )


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {sys.argv[0]} COMPOSE_MODEL_JSON")

    model: dict[str, Any] = json.loads(Path(sys.argv[1]).read_text())
    services = model.get("services", {})
    require(
        set(services) == set(SERVICES),
        "production Compose must contain api/web/nginx and unified orchestration services",
    )

    for name in SERVICES:
        service = services[name]
        require(
            service.get("container_name") == f"daily-insights-{name}",
            f"{name} must use a stable container name for reboot diagnostics",
        )
        require(
            "@sha256:" in service.get("image", ""), f"{name} image must use a digest"
        )
        require("build" not in service, f"{name} must not build on the host")
        require(
            service.get("restart") == "unless-stopped",
            f"{name} restart policy is invalid",
        )
        require(
            service.get("read_only") is True,
            f"{name} root filesystem must be read-only",
        )
        require(bool(service.get("healthcheck")), f"{name} must define a healthcheck")
        stop_grace = service.get("stop_grace_period")
        require(
            isinstance(stop_grace, str)
            and stop_grace.endswith("s")
            and float(stop_grace[:-1]) > 0,
            f"{name} must define a positive stop grace period",
        )

    require(not services["api"].get("ports"), "API must not publish a host port")
    require(not services["web"].get("ports"), "Web must not publish a host port")
    api_environment = services["api"].get("environment", {})
    require(
        API_ENVIRONMENT_KEYS.issubset(api_environment),
        "API must receive every production setting through Compose environment",
    )
    require(
        api_environment.get("DAILY_INSIGHTS_ENVIRONMENT") == "production",
        "API environment must be production",
    )
    require(
        api_environment.get("DAILY_INSIGHTS_TRUSTED_PROXY_CIDRS")
        == EXPECTED_PROXY_NETWORK,
        "API trusted proxy setting must match the app network",
    )
    require(
        any(
            volume.get("target") == "/var/spool/podcast-media"
            and volume.get("type") == "volume"
            for volume in services["api"].get("volumes", [])
        ),
        "API must have disk-backed audio verification spool space",
    )
    worker_environment = services["orchestration-worker"].get("environment", {})
    require(
        {
            "DAILY_INSIGHTS_ENVIRONMENT",
            "DAILY_INSIGHTS_RUNTIME_ROLE",
            "DAILY_INSIGHTS_DATABASE_URL",
            "DAILY_INSIGHTS_ORCHESTRATION_ENABLED",
            "DAILY_INSIGHTS_ORCHESTRATION_ACTIVATION_DATE",
            "DAILY_INSIGHTS_TWELVE_DATA_API_KEY",
            "DAILY_INSIGHTS_YFINANCE_ENABLED",
            "DAILY_INSIGHTS_TWSE_ENABLED",
            "DAILY_INSIGHTS_TRADINGVIEW_ENABLED",
            "DAILY_INSIGHTS_ANALYST_VIEWPOINTS_ENABLED",
            "DAILY_INSIGHTS_ANALYST_VIEWPOINTS_API_KEY",
            *NEWSROOM_ASSEMBLY_KEYS,
            "DAILY_INSIGHTS_R2_ENDPOINT_URL",
            "DAILY_INSIGHTS_R2_BUCKET_NAME",
            "DAILY_INSIGHTS_R2_ACCESS_KEY_ID",
            "DAILY_INSIGHTS_R2_SECRET_ACCESS_KEY",
        }.issubset(worker_environment),
        "orchestration-worker must receive provider, feature, cutover, and R2 upload settings",
    )
    require(
        worker_environment.get("DAILY_INSIGHTS_RUNTIME_ROLE") == "orchestration-worker",
        "orchestration-worker must use its least-privilege runtime role",
    )
    require(
        not {
            "DAILY_INSIGHTS_SESSION_SECRET",
            "DAILY_INSIGHTS_PASSWORD_PEPPER",
        }.intersection(worker_environment),
        "orchestration-worker must not receive API authentication secrets",
    )
    require(
        "DAILY_INSIGHTS_NEWSROOM_EMBEDDING_API_KEY" not in worker_environment,
        "orchestration-worker assembles drafts without embedding and must not hold that key",
    )
    newsroom_environment = services["newsroom-worker"].get("environment", {})
    require(
        set(newsroom_environment) == NEWSROOM_WORKER_KEYS,
        "newsroom-worker must receive exactly its database, newsroom, and ingestion settings",
    )
    require(
        newsroom_environment.get("DAILY_INSIGHTS_RUNTIME_ROLE") == "newsroom-worker",
        "newsroom-worker must use its least-privilege runtime role",
    )
    require(
        services["newsroom-worker"].get("command")
        == ["python", "-m", "daily_insights_api.scripts.run_newsroom_worker"],
        "newsroom-worker must run the newsroom worker entry point",
    )
    require(
        newsroom_environment.get("DAILY_INSIGHTS_NEWSROOM_BROWSER_CDP_URL") == BROWSER_CDP_URL,
        "newsroom-worker must reach CloakBrowser through the internal browser network",
    )
    validate_browser_isolation(model)
    for name in SERVICES:
        environment = services[name].get("environment", {})
        require(
            not REMOVED_ENVIRONMENT_KEYS.intersection(environment),
            f"{name} must not receive removed legacy daily-news settings",
        )
        require(
            LEGACY_LLM_SECRET_NAME not in environment,
            f"{name} must receive the newsroom LLM key as DAILY_INSIGHTS_NEWSROOM_LLM_API_KEY, "
            f"not under its GitHub Secret name {LEGACY_LLM_SECRET_NAME}",
        )
        if name != "newsroom-worker":
            require(
                not NEWSROOM_INGESTION_KEYS.intersection(environment),
                f"{name} must not receive newsroom ingestion settings",
            )
        if name not in {"newsroom-worker", "orchestration-worker"}:
            require(
                "DAILY_INSIGHTS_NEWSROOM_LLM_API_KEY" not in environment,
                f"{name} must not receive the newsroom LLM key",
            )
    dispatcher_environment = services["orchestration-dispatcher"].get("environment", {})
    require(
        set(dispatcher_environment)
        == {
            "DAILY_INSIGHTS_ENVIRONMENT",
            "DAILY_INSIGHTS_DATABASE_URL",
            "DAILY_INSIGHTS_ORCHESTRATION_ENABLED",
            "DAILY_INSIGHTS_ORCHESTRATION_ACTIVATION_DATE",
        },
        "orchestration-dispatcher must only receive queueing and activation settings",
    )
    for name in (
        "orchestration-worker",
        "orchestration-dispatcher",
        "newsroom-worker",
    ):
        require(
            services[name].get("environment", {}).get("DAILY_INSIGHTS_ENVIRONMENT")
            == "production",
            f"{name} environment must be production",
        )
    for name in SERVICES:
        if name != "nginx":
            require(not services[name].get("ports"), f"{name} must not publish a host port")
    web_environment = services["web"].get("environment", {})
    require(
        web_environment.get("APP_ENV") == "production",
        "Web environment must be production",
    )
    require(
        web_environment.get("API_INTERNAL_URL") == "http://api:8000",
        "Web must use the internal API service URL",
    )
    nginx_ports = services["nginx"].get("ports", [])
    require(len(nginx_ports) == 1, "nginx must publish exactly one port")
    nginx_port = nginx_ports[0]
    require(
        nginx_port.get("published") == "443" and nginx_port.get("target") == 443,
        "nginx must publish only host port 443",
    )

    tmpfs = services["nginx"].get("tmpfs", [])
    require(
        any(str(item).startswith("/etc/nginx/conf.d") for item in tmpfs),
        "read-only nginx must provide writable tmpfs for envsubst output",
    )
    require(
        set(services["nginx"].get("cap_add", []))
        == {"CHOWN", "NET_BIND_SERVICE", "SETGID", "SETUID"},
        "nginx must receive only the capabilities required to initialize and drop worker privileges",
    )

    app_network = model.get("networks", {}).get("app", {})
    subnets = [
        entry.get("subnet")
        for entry in app_network.get("ipam", {}).get("config", [])
        if entry.get("subnet")
    ]
    require(
        subnets == [EXPECTED_PROXY_NETWORK],
        "production app network must use its pinned CIDR",
    )
    ipaddress.ip_network(subnets[0], strict=True)

    command = [str(item) for item in services["api"].get("command", [])]
    try:
        allowed_proxy = command[command.index("--forwarded-allow-ips") + 1]
    except (ValueError, IndexError):
        raise SystemExit("API command must set --forwarded-allow-ips") from None
    require(
        allowed_proxy == EXPECTED_PROXY_NETWORK,
        "uvicorn forwarded proxy CIDR must match the app network",
    )


if __name__ == "__main__":
    main()
