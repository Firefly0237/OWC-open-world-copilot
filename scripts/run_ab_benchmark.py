"""Run baseline-vs-optimized A/B benchmark and write Markdown/JSON reports.

Offline default is deterministic and costs $0. Use --real only when provider keys are configured;
that is a real-provider offline benchmark, not production online A/B.
"""

from __future__ import annotations

import argparse

from owcopilot.ab import ABConfig, render_ab_markdown, run_ab_benchmark, write_ab_report
from owcopilot.util import use_utf8_stdout


def main() -> None:
    use_utf8_stdout()
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", action="store_true", help="Call the configured real provider")
    parser.add_argument("--name", default="offline_ab", help="Report name")
    parser.add_argument("--out-dir", default="reports", help="Output directory")
    args = parser.parse_args()

    if args.real:
        print("!! --real: this calls the configured provider and costs money.\n")
    report = run_ab_benchmark(ABConfig(name=args.name, use_real_model=args.real))
    md_path, json_path = write_ab_report(report, args.out_dir)
    print(render_ab_markdown(report))
    print(f"Wrote: {md_path}")
    print(f"Wrote: {json_path}")


if __name__ == "__main__":
    main()
