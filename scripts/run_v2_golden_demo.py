"""Run the v2 Golden World evaluation demo.

Example:
    python scripts/run_v2_golden_demo.py --workspace .tmp/golden_demo
"""

from __future__ import annotations

import argparse
import json
import tempfile
from collections.abc import Sequence
from pathlib import Path

from owcopilot.evaluation import run_golden_evaluation


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", help="Directory for generated content/export artifacts.")
    args = parser.parse_args(argv)

    if args.workspace:
        workspace = Path(args.workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        report = run_golden_evaluation(workspace)
    else:
        with tempfile.TemporaryDirectory(prefix="owcopilot-golden-") as tmp:
            report = run_golden_evaluation(tmp)

    print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, sort_keys=True))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
