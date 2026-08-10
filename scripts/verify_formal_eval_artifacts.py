"""Offline verifier for frozen formal-evaluation governance and run directories."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from owcopilot.evaluation.formal_artifacts import (  # noqa: E402
    default_formal_root,
    verify_formal_eval_runs,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify frozen formal-evaluation governance and, when supplied, final run artifacts."
        )
    )
    parser.add_argument(
        "run_dirs",
        metavar="RUN_DIR",
        nargs="*",
        type=Path,
        help="run directory containing report.json and traces/*.json",
    )
    parser.add_argument(
        "--formal-root",
        type=Path,
        default=default_formal_root(),
        help="frozen benchmarks/formal root (default: repository copy)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = _parser().parse_args(argv)
    result = verify_formal_eval_runs(args.run_dirs, formal_root=args.formal_root)
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
