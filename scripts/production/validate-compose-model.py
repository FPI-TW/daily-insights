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
    "morning-report-scheduler",
    "daily-news-scheduler",
    "analyst-viewpoints-scheduler",
    "index-daily-bars-scheduler",
    "institutional-flows-scheduler",
    "data-management-worker",
    "macro-dashboard-scheduler",
)
API_ENVIRONMENT_KEYS = {
    "DAILY_INSIGHTS_DAILY_NEWS_ENABLED",
    "DAILY_INSIGHTS_ANALYST_VIEWPOINTS_API_KEY",
    "DAILY_INSIGHTS_ANALYST_VIEWPOINTS_BASE_URL",
    "DAILY_INSIGHTS_ANALYST_VIEWPOINTS_ENABLED",
    "DAILY_INSIGHTS_ANALYST_VIEWPOINTS_TIMEOUT_SECONDS",
    "DAILY_INSIGHTS_DATABASE_URL",
    "DAILY_INSIGHTS_ENVIRONMENT",
    "DAILY_INSIGHTS_MODEL_API_BASE_URL",
    "DAILY_INSIGHTS_MODEL_API_KEY",
    "DAILY_INSIGHTS_MODEL_NAME",
    "DAILY_INSIGHTS_MODEL_PROVIDER",
    "DAILY_INSIGHTS_MORNING_REPORTS_ENABLED",
    "DAILY_INSIGHTS_NEWS_EXTRA_HOSTNAMES",
    "DAILY_INSIGHTS_NEWS_BLOCKED_HOSTNAMES",
    "DAILY_INSIGHTS_GUARDIAN_API_KEY",
    "DAILY_INSIGHTS_SEC_CONTACT_EMAIL",
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
    "DAILY_INSIGHTS_TRUSTED_PROXY_CIDRS",
    "DAILY_INSIGHTS_TWSE_ENABLED",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {sys.argv[0]} COMPOSE_MODEL_JSON")

    model: dict[str, Any] = json.loads(Path(sys.argv[1]).read_text())
    services = model.get("services", {})
    require(
        set(services) == set(SERVICES),
        "production Compose must contain api/web/nginx and all gated schedulers",
    )

    for name in SERVICES:
        service = services[name]
        require(
            service.get("container_name") == f"daily-insights-{name}",
            f"{name} must use a stable container name for reboot diagnostics",
        )
        require("@sha256:" in service.get("image", ""), f"{name} image must use a digest")
        require("build" not in service, f"{name} must not build on the host")
        require(service.get("restart") == "unless-stopped", f"{name} restart policy is invalid")
        require(service.get("read_only") is True, f"{name} root filesystem must be read-only")
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
    for scheduler, flag, secret in (
        (
            "morning-report-scheduler",
            "DAILY_INSIGHTS_MORNING_REPORTS_ENABLED",
            "DAILY_INSIGHTS_TWELVE_DATA_API_KEY",
        ),
        (
            "daily-news-scheduler",
            "DAILY_INSIGHTS_DAILY_NEWS_ENABLED",
            None,
        ),
        (
            "analyst-viewpoints-scheduler",
            "DAILY_INSIGHTS_ANALYST_VIEWPOINTS_ENABLED",
            "DAILY_INSIGHTS_ANALYST_VIEWPOINTS_API_KEY",
        ),
        # Yahoo publishes no API, so this scheduler has no provider credential.
        (
            "index-daily-bars-scheduler",
            "DAILY_INSIGHTS_YFINANCE_ENABLED",
            None,
        ),
        # TWSE publishes no API either; the scheduler only queues the run
        # that the worker below executes.
        (
            "institutional-flows-scheduler",
            "DAILY_INSIGHTS_TWSE_ENABLED",
            None,
        ),
        (
            "data-management-worker",
            "DAILY_INSIGHTS_YFINANCE_ENABLED",
            None,
        ),
    ):
        scheduler_environment = services[scheduler].get("environment", {})
        require(
            scheduler_environment.get("DAILY_INSIGHTS_ENVIRONMENT") == "production",
            f"{scheduler} environment must be production",
        )
        required = {flag, "DAILY_INSIGHTS_DATABASE_URL"}
        if secret is not None:
            required.add(secret)
        require(
            required.issubset(scheduler_environment),
            f"{scheduler} must receive its feature flag, database URL, and provider credential",
        )
        require(not services[scheduler].get("ports"), f"{scheduler} must not publish a host port")
    analyst_scheduler_environment = services["analyst-viewpoints-scheduler"].get("environment", {})
    require(
        {
            "DAILY_INSIGHTS_ANALYST_VIEWPOINTS_BASE_URL",
            "DAILY_INSIGHTS_ANALYST_VIEWPOINTS_TIMEOUT_SECONDS",
        }.issubset(analyst_scheduler_environment),
        "analyst-viewpoints-scheduler must receive its upstream URL and timeout",
    )
    macro_scheduler_environment = services["macro-dashboard-scheduler"].get("environment", {})
    require(
        {
            "DAILY_INSIGHTS_ENVIRONMENT",
            "DAILY_INSIGHTS_DATABASE_URL",
        }.issubset(macro_scheduler_environment),
        "macro-dashboard-scheduler must receive production database settings",
    )
    require(
        not {
            "DAILY_INSIGHTS_SESSION_SECRET",
            "DAILY_INSIGHTS_PASSWORD_PEPPER",
            "DAILY_INSIGHTS_R2_ENDPOINT_URL",
            "DAILY_INSIGHTS_R2_BUCKET_NAME",
            "DAILY_INSIGHTS_R2_ACCESS_KEY_ID",
            "DAILY_INSIGHTS_R2_SECRET_ACCESS_KEY",
            "DAILY_INSIGHTS_R2_SIGNED_URL_TTL_SECONDS",
        }.intersection(macro_scheduler_environment),
        "macro-dashboard-scheduler must not receive unrelated credentials",
    )
    require(
        macro_scheduler_environment.get("DAILY_INSIGHTS_ENVIRONMENT") == "production",
        "macro-dashboard-scheduler environment must be production",
    )
    require(
        not services["macro-dashboard-scheduler"].get("ports"),
        "macro-dashboard-scheduler must not publish a host port",
    )
    news_scheduler_environment = services["daily-news-scheduler"].get("environment", {})
    require(
        not {
            "DAILY_INSIGHTS_SESSION_SECRET",
            "DAILY_INSIGHTS_PASSWORD_PEPPER",
            "DAILY_INSIGHTS_MODEL_API_KEY",
            "DAILY_INSIGHTS_R2_ENDPOINT_URL",
            "DAILY_INSIGHTS_R2_BUCKET_NAME",
            "DAILY_INSIGHTS_R2_ACCESS_KEY_ID",
            "DAILY_INSIGHTS_R2_SECRET_ACCESS_KEY",
        }.intersection(news_scheduler_environment),
        "daily-news-scheduler must only receive queueing configuration",
    )
    web_environment = services["web"].get("environment", {})
    require(web_environment.get("APP_ENV") == "production", "Web environment must be production")
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
    require(subnets == [EXPECTED_PROXY_NETWORK], "production app network must use its pinned CIDR")
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
