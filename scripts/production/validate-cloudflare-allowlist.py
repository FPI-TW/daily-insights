#!/usr/bin/env python3
from __future__ import annotations

import ipaddress
import re
import sys
from pathlib import Path

LINE = re.compile(r"^set_real_ip_from ([^;\s]+);$")
DOCUMENTATION_NETWORKS = (
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("2001:db8::/32"),
)


def fail(message: str) -> None:
    raise SystemExit(message)


def main() -> None:
    if len(sys.argv) != 2:
        fail(f"usage: {sys.argv[0]} CLOUDFLARE_REALIP_CONF")

    path = Path(sys.argv[1])
    if not path.is_file() or path.is_symlink():
        fail("Cloudflare real-IP allowlist must be a regular non-symlink file")

    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for line_number, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = LINE.fullmatch(line)
        if match is None:
            fail(f"invalid Cloudflare allowlist directive on line {line_number}")
        try:
            network = ipaddress.ip_network(match.group(1), strict=True)
        except ValueError as error:
            fail(f"invalid CIDR on line {line_number}: {error}")
        if network.prefixlen == 0:
            fail("Cloudflare allowlist must not contain a default route")
        if any(
            network.version == documentation.version and network.subnet_of(documentation)
            for documentation in DOCUMENTATION_NETWORKS
        ):
            fail("Cloudflare allowlist must not contain documentation/test networks")
        if network.is_private or network.is_loopback or network.is_link_local:
            fail("Cloudflare allowlist must contain only public network ranges")
        networks.append(network)

    if not networks:
        fail("Cloudflare real-IP allowlist must contain at least one CIDR")


if __name__ == "__main__":
    main()
