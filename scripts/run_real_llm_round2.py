"""Round-2 real-LLM validation: the assist/repair paths that round 1 (QA) did not cover.

Round 1 (see project_docs/真实LLM测试与本轮修复报告.md) proved grounded QA against
deepseek-v4-flash on the 雾隐山河 pack. This script validates the NEW model-facing paths on the
self-contained acceptance world:

  1. patch suggest  — real model proposes JSON-Patch candidates for 4 representative audit
                      issues; every candidate passes shadow re-audit before being counted.
  2. quest draft    — one Chinese brief drafted into a pending-review quest.
  3. barks batch    — 2 speakers x 3 variants under a 40-char budget with deterministic lint.
  4. ask regression — 3 questions (2 answerable, 1 must-refuse) on the same world.

Usage:
  .venv\\Scripts\\python.exe scripts\\run_real_llm_round2.py
      [--model deepseek-v4-flash] [--workspace .tmp\\real_llm_round2]
      [--output project_docs/reports/real_llm_round2.json]

Requires OPENAI_BASE_URL / OPENAI_API_KEY (read from .env via load_dotenv; shell env wins).
Exit 0 when every section meets its gate; the JSON report records everything either way.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from owcopilot.assist.barks import BarkBatchService  # noqa: E402
from owcopilot.assist.drafts import QuestDraftService  # noqa: E402
from owcopilot.assist.review_queue import ReviewQueue  # noqa: E402
from owcopilot.content.store import ContentStore  # noqa: E402
from owcopilot.evaluation.acceptance import build_acceptance_world, seed_errors  # noqa: E402
from owcopilot.llm.cache import NoOpCache  # noqa: E402
from owcopilot.llm.gateway import LLMGateway, OpenAICompatProvider  # noqa: E402
from owcopilot.llm.router import StaticRouter  # noqa: E402
from owcopilot.llm.telemetry import TelemetryCollector  # noqa: E402
from owcopilot.pipeline.audit import run_full_audit  # noqa: E402
from owcopilot.pipeline.patches import suggest_for_issue  # noqa: E402
from owcopilot.pipeline.project import ProjectContext  # noqa: E402
from owcopilot.qa.service import LoreQAService  # noqa: E402
from owcopilot.util import load_dotenv, use_utf8_stdout  # noqa: E402

SUGGEST_RULES = [
    "UNKNOWN_ENTITY_REF",
    "TERM_INCONSISTENT",
    "MISSING_LOCALIZATION_KEY",
    "FACTION_CONFLICT",
    "TIMELINE_VIOLATION",
    "POI_LEVEL_OUT_OF_BOUNDS",
]


def _gateway(model: str, task: str) -> tuple[LLMGateway, TelemetryCollector]:
    telemetry = TelemetryCollector()
    gateway = LLMGateway(
        providers={"cheap": OpenAICompatProvider(model=model)},
        router=StaticRouter(mapping={task: "cheap"}),
        cache=NoOpCache(),
        telemetry=telemetry,
        max_retries=1,
        retry_backoff_seconds=1.0,
    )
    return gateway, telemetry


def run(model: str, workspace: Path) -> dict[str, Any]:
    corrupted_root = workspace / "world_seeded"
    clean_root = workspace / "world_clean"
    corrupted, _seeded = seed_errors(build_acceptance_world())
    ContentStore(corrupted_root).save(corrupted)
    ContentStore(clean_root).save(build_acceptance_world())

    report: dict[str, Any] = {"model": model, "sections": {}}
    totals = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    def absorb(telemetry: TelemetryCollector) -> dict[str, Any]:
        summary = telemetry.summary()
        totals["calls"] += int(summary.get("calls", 0))
        totals["input_tokens"] += int(summary.get("input_tokens", 0))
        totals["output_tokens"] += int(summary.get("output_tokens", 0))
        totals["cost_usd"] += float(summary.get("total_cost_usd", 0.0))
        return summary

    # ---- section 1: patch suggest on representative seeded issues -------------------
    project = ProjectContext.open(corrupted_root, sqlite_path=workspace / "rt_seeded.sqlite")
    try:
        audit = run_full_audit(project, persist=True)
        issues_by_rule = {}
        for issue in audit.issues:
            if issue.status.value == "open" and issue.rule_code not in issues_by_rule:
                issues_by_rule[issue.rule_code] = issue
        cases = []
        for rule in SUGGEST_RULES:
            issue = issues_by_rule.get(rule)
            if issue is None:
                cases.append({"rule": rule, "skipped": "rule not present in audit"})
                continue
            gateway, telemetry = _gateway(model, "patch_suggest")
            started = time.perf_counter()
            try:
                result = suggest_for_issue(project, issue, gateway=gateway, max_candidates=3)
                cases.append(
                    {
                        "rule": rule,
                        "issue_target": issue.target_ref,
                        "parse_failed": result.parse_failed,
                        "candidates": len(result.candidates),
                        "llm_candidates": sum(
                            1 for item in result.candidates if item.source == "llm"
                        ),
                        "target_resolved_any": any(
                            item.target_resolved for item in result.candidates
                        ),
                        "rejected_by_shadow_audit": result.rejected_count,
                        "top_ops": [
                            op.model_dump(mode="json")
                            for op in (
                                result.candidates[0].candidate.ops if result.candidates else []
                            )
                        ],
                        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                        "telemetry": absorb(telemetry),
                    }
                )
            except Exception as e:  # provider failures recorded, not fatal
                cases.append({"rule": rule, "error": f"{e.__class__.__name__}: {e}"})
        attempted = [case for case in cases if "skipped" not in case]
        passed_cases = [
            case
            for case in attempted
            if case.get("candidates", 0) >= 1 and case.get("target_resolved_any")
        ]
        required = max(1, round(0.75 * len(attempted)))
        report["sections"]["patch_suggest"] = {
            "cases": cases,
            "gate": (
                f">=75% of attempted issues ({required}/{len(attempted)}) get a "
                "shadow-validated, target-resolving candidate"
            ),
            "passed": len(passed_cases) >= required,
        }
    finally:
        project.close()

    # ---- section 2: quest draft (Chinese brief, pending review) ---------------------
    project = ProjectContext.open(clean_root, sqlite_path=workspace / "rt_clean.sqlite")
    try:
        gateway, telemetry = _gateway(model, "quest_draft")
        section: dict[str, Any]
        try:
            draft = QuestDraftService(
                gateway=gateway,
                context_builder=project.context_builder,
                audit_runner=project.audit_runner,
                bundle=project.bundle,
            ).draft_quest(
                "为雾脊山道写一个护送盐车去烽燧的支线任务，委托人用已有NPC，难度适合低等级。",
                budget_tokens=900,
            )
            queue = ReviewQueue(project.sqlite_store)
            item = queue.add_quest_draft(draft.quest.model_dump(mode="json", exclude_none=True))
            section = {
                "quest_id": draft.quest.id,
                "title": draft.quest.title,
                "origin": draft.quest.origin.value,
                "review_status": draft.quest.review_status.value,
                "new_issues": [issue.rule_code for issue in draft.issues],
                "review_item_id": item.id,
                "telemetry": absorb(telemetry),
                "passed": draft.quest.origin.value == "ai_draft"
                and draft.quest.review_status.value == "pending_review",
            }
        except Exception as e:
            section = {"error": f"{e.__class__.__name__}: {e}", "passed": False}
        report["sections"]["quest_draft"] = section

        # ---- section 3: barks batch --------------------------------------------------
        gateway, telemetry = _gateway(model, "barks_batch")
        try:
            barks = BarkBatchService(
                gateway=gateway,
                bundle=project.bundle,
                review_queue=ReviewQueue(project.sqlite_store),
            ).generate(
                speaker_ids=["npc_r1_a", "npc_r2_b"],
                topic="发现可疑商队靠近烽燧",
                variants_per_speaker=3,
                max_chars=40,
            )
            speakers_with_accepts = {variant.speaker_id for variant in barks.accepted}
            report["sections"]["barks_batch"] = {
                "accepted": [
                    {"speaker": variant.speaker_id, "text": variant.text}
                    for variant in barks.accepted
                ],
                "rejected": [
                    {
                        "speaker": rejected.speaker_id,
                        "text": rejected.text,
                        "issues": [issue.code for issue in rejected.issues],
                    }
                    for rejected in barks.rejected
                ],
                "review_items": len(barks.review_items),
                "telemetry": absorb(telemetry),
                "gate": "each speaker gets >=1 lint-passing variant",
                "passed": speakers_with_accepts == {"npc_r1_a", "npc_r2_b"},
            }
        except Exception as e:
            report["sections"]["barks_batch"] = {
                "error": f"{e.__class__.__name__}: {e}",
                "passed": False,
            }

        # ---- section 4: ask regression ----------------------------------------------
        gateway, telemetry = _gateway(model, "qa_answer")
        qa = LoreQAService(
            gateway=gateway,
            context_builder=project.context_builder,
            bundle=project.bundle,
        )
        questions = [
            ("沈清河是谁？", "grounded"),
            ("玄武之约是什么事件？", "grounded"),
            ("龙王是谁？", "refused"),
        ]
        qa_cases = []
        for question, expectation in questions:
            try:
                answer = qa.ask(question, budget_tokens=900)
                ok = (
                    (not answer.refused and bool(answer.citations))
                    if expectation == "grounded"
                    else answer.refused
                )
                qa_cases.append(
                    {
                        "question": question,
                        "expectation": expectation,
                        "refused": answer.refused,
                        "citations": [citation.ref for citation in answer.citations],
                        "answer_excerpt": (answer.answer or "")[:160],
                        "passed": ok,
                    }
                )
            except Exception as e:
                qa_cases.append(
                    {
                        "question": question,
                        "error": f"{e.__class__.__name__}: {e}",
                        "passed": False,
                    }
                )
        report["sections"]["ask_regression"] = {
            "cases": qa_cases,
            "telemetry": absorb(telemetry),
            "passed": all(case["passed"] for case in qa_cases),
        }
    finally:
        project.close()

    totals["cost_usd"] = round(totals["cost_usd"], 6)
    report["totals"] = totals
    report["passed"] = all(section.get("passed", False) for section in report["sections"].values())
    return report


def main() -> int:
    use_utf8_stdout()
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--workspace", default=".tmp/real_llm_round2")
    parser.add_argument("--output", default="project_docs/reports/real_llm_round2.json")
    args = parser.parse_args()

    workspace = Path(args.workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    report = run(args.model, workspace)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
