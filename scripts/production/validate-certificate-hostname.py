#!/usr/bin/env python3
from __future__ import annotations

import ssl
import sys


def dns_name_matches(pattern: str, hostname: str) -> bool:
    pattern = pattern.rstrip(".").casefold()
    hostname = hostname.rstrip(".").casefold()
    if "*" not in pattern:
        return pattern == hostname
    if not pattern.startswith("*.") or pattern.count("*") != 1:
        return False

    suffix = pattern[2:]
    hostname_labels = hostname.split(".")
    suffix_labels = suffix.split(".")
    return (
        len(hostname_labels) == len(suffix_labels) + 1
        and hostname_labels[1:] == suffix_labels
    )


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(f"usage: {sys.argv[0]} CERTIFICATE HOSTNAME")
    certificate = ssl._ssl._test_decode_cert(sys.argv[1])
    hostname = sys.argv[2]
    san_names = [
        value
        for name_type, value in certificate.get("subjectAltName", ())
        if name_type == "DNS"
    ]
    names = san_names
    if not names:
        names = [
            value
            for relative_name in certificate.get("subject", ())
            for attribute, value in relative_name
            if attribute == "commonName"
        ]
    if not names or not any(dns_name_matches(name, hostname) for name in names):
        raise SystemExit(
            f"origin certificate hostname mismatch: {hostname} is not covered"
        )


if __name__ == "__main__":
    main()
