"""Check model-host DNS/TCP/TLS without credentials or model API requests."""

from __future__ import annotations

import argparse
import json
import socket
import ssl
import sys
import time
from pathlib import Path


def diagnose() -> dict:
    report = {
        "host": "api.deepseek.com",
        "python": sys.executable,
        "model_api_requests": 0,
        "credentials_sent": False,
    }
    stage = "dns"
    started = time.monotonic()
    try:
        addresses = socket.getaddrinfo("api.deepseek.com", 443, type=socket.SOCK_STREAM)
        report["dns"] = "resolved"
        report["address_count"] = len(addresses)
        stage = "tcp"
        with socket.create_connection(("api.deepseek.com", 443), timeout=8) as connection:
            report["tcp"] = "connected"
            stage = "tls"
            with ssl.create_default_context().wrap_socket(
                connection, server_hostname="api.deepseek.com"
            ) as secured:
                report["tls"] = "connected"
                report["tls_version"] = secured.version()
        report["status"] = "connected"
    except Exception as exc:
        report.update(
            status="failed",
            failed_stage=stage,
            error_type=type(exc).__name__,
            error=str(exc),
            errno=getattr(exc, "errno", None),
            winerror=getattr(exc, "winerror", None),
        )
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = diagnose()
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized)
    return 0 if result["status"] == "connected" else 1


if __name__ == "__main__":
    raise SystemExit(main())
