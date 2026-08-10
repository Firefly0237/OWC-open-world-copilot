"""Run all four frozen formal evaluator controls and verify their emitted artifacts.

Example:
    python scripts/run_formal_evals.py --output-dir .tmp/formal-runs \
        --run-id local-conformance-v1 --generated-at 2026-08-03T00:00:00Z
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

# The CLI reserves stderr for its machine-readable error document.  Transformers
# otherwise emits an import-time advisory when only tokenizer dependencies are
# installed, before the CLI can report its own structured result.
os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")

from owcopilot.evaluation.formal_artifacts import default_formal_root  # noqa: E402
from owcopilot.evaluation.formal_runner import (  # noqa: E402
    DEFAULT_FORMAL_GENERATED_AT,
    DEFAULT_FORMAL_RUN_ID,
    FormalRunnerError,
    run_formal_evals,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the four deterministic harness-only formal controls; no model or quality "
            "evaluation is invoked."
        )
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Dedicated output root; each suite gets report.json and traces/*.json.",
    )
    parser.add_argument(
        "--formal-root",
        default=str(default_formal_root()),
        help="Frozen benchmarks/formal governance root.",
    )
    parser.add_argument(
        "--run-id",
        default=DEFAULT_FORMAL_RUN_ID,
        help="Deterministic base run id; suite ids are appended.",
    )
    parser.add_argument(
        "--generated-at",
        default=DEFAULT_FORMAL_GENERATED_AT,
        help="Explicit timezone-aware ISO-8601 timestamp used by reports and traces.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = _parser().parse_args(argv)
    try:
        result = run_formal_evals(
            args.output_dir,
            formal_root=args.formal_root,
            run_id=args.run_id,
            generated_at=args.generated_at,
        )
    except FormalRunnerError as exc:
        print(
            json.dumps(
                {"ok": False, "exit_code": 2, "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
