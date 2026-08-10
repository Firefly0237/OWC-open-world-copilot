"""Per-construct losslessness ledger for the ink importer — the evidence behind "无损往返".

Parses inkle's *The Intercept* (a real, MIT-licensed narrative game, vendored under
``tests/fixtures/ink/the_intercept/``) with the production importer and accounts for every ink
construct: what is materialized into the ImportPlan, and where every non-captured occurrence goes
(skipped / deferred / flagged). The point is honesty — nothing is dropped in silence.

Deterministic and ``$0`` (a pure parse of the committed fixture — no model, no network):

    python scripts/run_ink_losslessness.py                       # the committed report
    python scripts/run_ink_losslessness.py --ink path/to.ink     # any other ink file

Writes a Markdown report to ``benchmarks/ink_losslessness.md`` and prints it.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from owcopilot.recognize.ink_losslessness import (
    THE_INTERCEPT_FIXTURE,
    build_ledger,
    render_markdown,
)
from owcopilot.util import use_utf8_stdout


def main() -> None:
    use_utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--ink",
        type=Path,
        default=THE_INTERCEPT_FIXTURE,
        help="ink script to analyze (default: the vendored The Intercept fixture)",
    )
    args = ap.parse_args()

    text = args.ink.read_text(encoding="utf-8")
    ledger = build_ledger(text, source_file=args.ink.name)
    report = render_markdown(ledger, corpus=args.ink.name)

    out = Path(__file__).resolve().parents[1] / "benchmarks" / "ink_losslessness.md"
    out.write_text(report, encoding="utf-8")
    print("\n" + report)
    print(f"\n[written] {out}")


if __name__ == "__main__":
    main()
