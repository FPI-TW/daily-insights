#!/usr/bin/env python3
"""Exercise both actual proxy configurations against a local synthetic upstream.

Requires Docker and openssl; creates/removes its own container. No application
credentials or external requests. --revision <pre-fix-commit> reproduces the defect.
"""

import argparse
import concurrent.futures
import pathlib
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]


def command(*args: str) -> str:
    return subprocess.check_output(args, text=True).strip()


def check(production: bool, revision: str | None) -> None:
    def source(path: str) -> str:
        return (
            command("git", "-C", str(ROOT), "show", f"{revision}:{path}")
            if revision
            else (ROOT / path).read_text()
        )

    with tempfile.TemporaryDirectory(prefix="nginx-security-") as temporary:
        directory = pathlib.Path(temporary)
        prefix = "infra/production/nginx" if production else "infra/nginx"
        config = source(
            f"{prefix}/default.conf.template"
            if production
            else f"{prefix}/conf.d/default.conf"
        )
        config = (
            config.replace("${PUBLIC_HOSTNAME}", "localhost")
            .replace("api:8000", "127.0.0.1:18080")
            .replace("web:3000", "127.0.0.1:18081")
        )
        config = config.replace("/etc/nginx/tls/", "/tmp/security-nginx/")
        config += '\nserver { listen 18080; location / { return 200 "api"; } }\nserver { listen 18081; location / { return 200 "web"; } }\n'
        (directory / "default.conf").write_text(config)
        main = source(f"{prefix}/nginx.conf").replace(
            "/etc/nginx/conf.d/*.conf", "/tmp/security-nginx/default.conf"
        )
        main = main.replace(
            "include /etc/nginx/cloudflare-realip.conf;",
            "# No external proxy in this isolated test.",
        )
        (directory / "nginx.conf").write_text(main)
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-keyout",
                str(directory / "origin.key"),
                "-out",
                str(directory / "origin.crt"),
            ],
            check=True,
            capture_output=True,
        )
        port = "443" if production else "80"
        container = command(
            "docker",
            "run",
            "-d",
            "--rm",
            "-p",
            f"127.0.0.1::{port}",
            "--entrypoint",
            "sh",
            "nginx:1.27-alpine",
            "-c",
            "sleep 300",
        )
        try:
            command(
                "docker",
                "cp",
                str(directory) + "/.",
                f"{container}:/tmp/security-nginx",
            )
            command(
                "docker",
                "exec",
                container,
                "nginx",
                "-c",
                "/tmp/security-nginx/nginx.conf",
            )
            address = command("docker", "port", container, f"{port}/tcp").splitlines()[
                0
            ]
            base = ("https://" if production else "http://") + address
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            tls.check_hostname = False
            tls.verify_mode = (
                ssl.CERT_NONE
            )  # Only the disposable loopback test certificate.
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=tls),
            )

            def request(path: str) -> int:
                try:
                    with opener.open(base + path, timeout=5) as response:
                        return response.status
                except urllib.error.HTTPError as error:
                    return error.code

            for _ in range(30):
                try:
                    if request("/api/health") == 200:
                        break
                except OSError:
                    time.sleep(0.1)
            # Preserve the more specific streaming/upload locations and web fallback.
            for path in (
                "/api/health",
                "/api/v1/chat/stream",
                "/api/admin/podcasts/uploads",
                "/zh-hant/login",
            ):
                assert request(path) == 200, path
            path = "/api/podcasts/123e4567-e89b-12d3-a456-426614174000/audio-url"
            with concurrent.futures.ThreadPoolExecutor(max_workers=40) as pool:
                statuses = list(pool.map(request, [path] * 100))
            assert 200 in statuses and 503 in statuses, (
                f"limiter not selected: {statuses}"
            )
            assert set(statuses) <= {200, 503}, statuses
            # Pydantic's UUID accepts brace and URN forms as well as hex text.
            # Every representation that reaches the signer must share the zone.
            for identifier in (
                "%7B123e4567-e89b-12d3-a456-426614174000%7D",
                "urn:uuid:123e4567-e89b-12d3-a456-426614174000",
                "123E4567E89B12D3A456426614174000",
            ):
                alternate = f"/api/podcasts/{identifier}/audio-url"
                with concurrent.futures.ThreadPoolExecutor(max_workers=40) as pool:
                    alternate_statuses = list(pool.map(request, [alternate] * 100))
                assert 503 in alternate_statuses, (alternate, alternate_statuses)
                assert set(alternate_statuses) <= {200, 503}, alternate_statuses
            print(
                f"{'production' if production else 'development'}: {statuses.count(503)} limited; normal routes passed"
            )
        finally:
            command("docker", "rm", "-f", container)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision")
    options = parser.parse_args()
    for production_config in (False, True):
        check(production_config, options.revision)
