# CLI 入口：v2 项目工作流全部子命令。JSON 输出、可进 CI。
"""Command-line entrypoint for the v2 project workflow."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..assist.barks import BarkBatchService
from ..assist.calibration import build_calibration_report
from ..assist.drafts import QuestDraftService
from ..assist.offline import OfflineBarksProvider, OfflineQuestDraftProvider
from ..assist.review_queue import ReviewQueue
from ..audit.baseline import AuditBaseline, issue_fingerprint
from ..audit.default_rules import build_default_rule_registry
from ..audit.models import IssueStatus
from ..audit.report import render_audit_markdown
from ..audit.runner import AuditRunner
from ..content.hash import content_hash
from ..content.ingest import ingest_raw_objects, parse_paths
from ..content.mapping import FieldMapping, apply_field_mapping
from ..evaluation import run_acceptance_evaluation, run_golden_evaluation
from ..exporters import EngineTarget, export_content_bundle
from ..impact import Change, ChangeSet, ChangeType, ImpactAnalyzer, ImpactLevel
from ..llm.cache import NoOpCache
from ..llm.gateway import LLMGateway, require_offline_llm_allowed
from ..llm.resilience import build_real_provider
from ..llm.router import StaticRouter
from ..llm.telemetry import TelemetryCollector
from ..pipeline.audit import run_full_audit
from ..pipeline.export_gate import assert_export_ready
from ..pipeline.harness import run_quality_harness
from ..pipeline.ingest import run_ingest
from ..pipeline.patches import (
    apply_patch_workflow,
    find_issue,
    rollback_patch_workflow,
    suggest_for_issue,
)
from ..pipeline.project import ProjectContext
from ..pipeline.review import decide_review_item
from ..qa.community_index import CommunityIndexService
from ..qa.offline import OfflineCommunityReportProvider, OfflineQAProvider
from ..qa.service import LoreQAService
from ..readiness import assess_readiness
from ..telemetry import deterministic_step, llm_step, summarize_workflow
from ..util import load_dotenv

logger = logging.getLogger(__name__)


def _configure_output_encoding() -> None:
    """Keep CLI output alive on non-UTF-8 consoles (e.g. Windows GBK).

    Model output can contain characters outside the console encoding (emoji, variation
    selectors). With the default errors='strict', one such character kills the whole command
    with a raw UnicodeEncodeError (exit 2). backslashreplace degrades only the unencodable
    characters to \\uXXXX escapes — CJK still renders, and the emitted JSON stays valid for
    pipes/files. Streams without reconfigure (test capture shims) are left untouched.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="backslashreplace")
        except Exception:  # noqa: BLE001 — an exotic stream must not break the CLI itself
            pass


def main(argv: Sequence[str] | None = None) -> int:
    _configure_output_encoding()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except Exception as e:
        print(
            json.dumps(
                {"error": str(e), "type": e.__class__.__name__},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="owcopilot")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest", help="Import source files into a content store.")
    _add_project_args(ingest)
    ingest.add_argument("--input", action="append", required=True, help="Source file to import.")
    ingest.add_argument(
        "--field-mapping",
        help=(
            "JSON field mapping. Supports either {'type','columns'} for all inputs or "
            "a per-file mapping keyed by relative path."
        ),
    )
    ingest.add_argument("--write", action="store_true", help="Commit changes instead of dry-run.")
    ingest.add_argument(
        "--skip-conflicts",
        action="store_true",
        help="With --write, persist non-conflicting changes while leaving conflicts untouched.",
    )
    ingest.set_defaults(handler=_cmd_ingest)

    audit = subparsers.add_parser("audit", help="Run deterministic content audits.")
    _add_project_args(audit)
    audit.add_argument("--no-persist", action="store_true", help="Do not persist runs or issues.")
    audit.add_argument("--fail-on-error", action="store_true", help="Exit 1 if open errors exist.")
    audit.add_argument("--baseline", help="Audit baseline JSON file.")
    audit.add_argument(
        "--update-baseline",
        help=(
            "Write a baseline JSON to this path accepting every currently open issue "
            "(lint-ratchet mode for onboarding existing projects)."
        ),
    )
    audit.add_argument(
        "--markdown-report",
        help="Also render a human-readable Markdown report to this path.",
    )
    audit.set_defaults(handler=_cmd_audit)

    readiness = subparsers.add_parser(
        "readiness",
        help="Score content against the completeness standard (completeness, not correctness).",
    )
    _add_project_args(readiness)
    readiness.add_argument(
        "--kind",
        choices=["quest", "character", "region", "dialogue_tree"],
        help="Only assess one content kind.",
    )
    readiness.add_argument(
        "--only-incomplete",
        action="store_true",
        help="List only items that are not yet complete.",
    )
    readiness.set_defaults(handler=_cmd_readiness)

    quest_explore = subparsers.add_parser(
        "quest-explore",
        help=(
            "Bounded full-path exploration of one quest's branch graph (narrative model checking): "
            "outcome reachability with replayable witnesses, dead branches, stage/branch coverage, "
            "and dead ends. A $0 diagnostic report — never a pass/fail gate. `truncated` flags a "
            "boundary hit."
        ),
    )
    _add_project_args(quest_explore)
    quest_explore.add_argument("quest_id", help="Quest id to explore (from the content bundle).")
    quest_explore.add_argument(
        "--max-states",
        type=_nonneg_int,
        default=100_000,
        help="State-graph budget: stop after this many distinct (stage, var-state) nodes.",
    )
    quest_explore.add_argument(
        "--max-depth",
        type=_nonneg_int,
        default=10_000,
        help="Path-depth budget: stop expanding a path past this many transitions.",
    )
    quest_explore.set_defaults(handler=_cmd_quest_explore)

    quest_simulate = subparsers.add_parser(
        "quest-simulate",
        help=(
            "Deterministic single-path playtest of one quest (the $0 counterpart of "
            "quest-explore): walk stages with a fixed choice script, applying stage and branch "
            "effects, and report the path, per-step effects and final state."
        ),
    )
    _add_project_args(quest_simulate)
    quest_simulate.add_argument("quest_id", help="Quest id to simulate (from the content bundle).")
    quest_simulate.add_argument(
        "--choices",
        help=(
            "Comma-separated branch ids to prefer at decision stages (a witness from "
            "quest-explore replays verbatim here)."
        ),
    )
    quest_simulate.add_argument(
        "--initial-state",
        help=(
            "Path to a JSON object of variable overrides — e.g. the final_state of a dialogue "
            "walk, closing the dialogue-choice -> quest-path loop."
        ),
    )
    quest_simulate.add_argument(
        "--dialogue-tree",
        help=(
            "Walk this dialogue tree FIRST and feed its final_state into the simulation (the "
            "composition contract: dialogue choices set variables, quest logic reads them). "
            "--initial-state overrides walk values on key conflicts."
        ),
    )
    quest_simulate.add_argument(
        "--dialogue-choices",
        help="Comma-separated '<node_id>:<index>' selectors for the pre-walk dialogue tree.",
    )
    quest_simulate.set_defaults(handler=_cmd_quest_simulate)

    dialogue_walk = subparsers.add_parser(
        "dialogue-walk",
        help=(
            "Walk one dialogue tree deterministically (conditions gate choices, chosen effects "
            "mutate state) or, with --explore, enumerate every satisfiable path — the dialogue "
            "counterpart of quest-simulate / quest-explore."
        ),
    )
    _add_project_args(dialogue_walk)
    dialogue_walk.add_argument("tree_id", help="Dialogue tree id (from the content bundle).")
    dialogue_walk.add_argument(
        "--choices", help="Comma-separated '<node_id>:<index>' selectors to prefer."
    )
    dialogue_walk.add_argument(
        "--initial-state", help="Path to a JSON object of variable overrides."
    )
    dialogue_walk.add_argument(
        "--explore",
        action="store_true",
        help="Enumerate every condition-satisfiable path (endings + choice coverage) instead.",
    )
    dialogue_walk.set_defaults(handler=_cmd_dialogue_walk)

    prompts = subparsers.add_parser(
        "prompts",
        help=(
            "Inspect the prompt registry: every model-facing prompt with purpose, task label, "
            "output contract and source. `prompts` lists; `prompts --show <name>` prints one "
            "prompt's full text and metadata."
        ),
    )
    prompts.add_argument("--show", metavar="NAME", help="Print one prompt's metadata + text.")
    prompts.add_argument("--output", help="Write the JSON result to this file as well as stdout.")
    prompts.set_defaults(handler=_cmd_prompts)

    memory_report = subparsers.add_parser(
        "memory-report",
        help=(
            "Report the review feedback loop over a workspace: review-queue state, "
            "reviewer calibration, and the lesson feedback path."
        ),
    )
    _add_project_args(memory_report)
    memory_report.add_argument(
        "--write-lessons",
        action="store_true",
        help=(
            "Also distill lessons from the calibration report and persist them to the lesson "
            "archive (the same write path the REST calibration endpoint uses) — closes the "
            "memory loop for CLI-only workflows."
        ),
    )
    memory_report.set_defaults(handler=_cmd_memory_report)

    lessons = subparsers.add_parser(
        "lessons",
        help=(
            "Manage the cross-session lesson archive. `lessons revoke <id> --reason ...` "
            "soft-deletes a wrong or harmful lesson so it stops being injected into prompts "
            "(tombstoned, never physically deleted; idempotent)."
        ),
    )
    _add_project_args(lessons)
    lessons.add_argument(
        "action",
        choices=["revoke"],
        help="The lesson operation to perform (currently only 'revoke').",
    )
    lessons.add_argument("lesson_id", help="The id of the lesson to revoke.")
    lessons.add_argument(
        "--reason",
        default="",
        help="Why the lesson is being revoked; recorded on the tombstone.",
    )
    lessons.set_defaults(handler=_cmd_lessons)

    harness = subparsers.add_parser(
        "quality-harness",
        help="Run the deterministic MCP-style quality loop and return next tool calls.",
    )
    _add_project_args(harness)
    harness.add_argument(
        "--no-propose-fixes",
        action="store_true",
        help="Skip patch proposal persistence; report only gates and next tool calls.",
    )
    harness.add_argument("--max-issues", type=int, default=5)
    harness.add_argument("--max-candidates-per-issue", type=int, default=1)
    harness.set_defaults(handler=_cmd_quality_harness)

    agent = subparsers.add_parser(
        "agent",
        help=(
            "Run a ReAct agent that diagnoses the world via the read-only/propose tool surface "
            "and reports the safe next step (never writes canon)."
        ),
    )
    _add_project_args(agent)
    agent.add_argument("--goal", required=True, help="What the agent should achieve / investigate.")
    agent.add_argument("--max-steps", type=int, default=6, help="Tool-call budget for the loop.")
    agent.add_argument(
        "--skills",
        nargs="*",
        metavar="SKILL",
        default=None,
        help=(
            "Whitelist of skill names the agent may use (BE-9). "
            "Omit to allow all built-in skills. "
            "Example: --skills audit_project list_issues"
        ),
    )
    agent.add_argument(
        "--no-recall",
        action="store_true",
        help=(
            "Disable episodic memory recall (MemGPT recall storage + Reflexion buffer). "
            "By default the agent retrieves similar past runs and reflections and injects them "
            "as prompt context; this flag turns that read side off (writes still happen)."
        ),
    )
    agent.add_argument(
        "--early-stopping",
        choices=["force", "generate"],
        default="force",
        help=(
            "Step-budget-exhaustion behaviour (default: force = stop immediately, unchanged). "
            "'generate' spends one extra LLM call asking for a real final answer from the "
            "transcript collected so far (LangChain AgentExecutor's early_stopping_method="
            "'generate'), instead of discarding it; stop_reason becomes 'max_steps_generate'."
        ),
    )
    _add_llm_args(agent)
    agent.set_defaults(handler=_cmd_agent)

    multi_agent = subparsers.add_parser(
        "multi-agent",
        help=(
            "Run the runtime multi-agent system (Orchestrator-Worker + SQLite blackboard) on a "
            "real world: an orchestrator decomposes the goal, diag/repair workers run "
            "independently, and a verifier re-audits — all communicating through the blackboard. "
            "Never writes canon (propose-only). Offline $0 with the deterministic double."
        ),
    )
    _add_project_args(multi_agent)
    multi_agent.add_argument(
        "--goal", required=True, help="High-level goal for the orchestrator to decompose."
    )
    _add_llm_args(multi_agent)
    multi_agent.set_defaults(handler=_cmd_multi_agent)

    agent_history = subparsers.add_parser(
        "agent-history",
        help=(
            "Inspect persisted agent runs (episodic/recall memory). Lists recent runs "
            "newest-first; `--run-id` shows one run's full transcript. Read-only; never "
            "writes canon."
        ),
    )
    _add_project_args(agent_history)
    agent_history.add_argument(
        "--run-id", help="Show this run's full transcript by id (default: list recent runs)."
    )
    agent_history.add_argument(
        "--limit", type=_nonneg_int, default=50, help="Max runs to list (ignored with --run-id)."
    )
    agent_history.set_defaults(handler=_cmd_agent_history)

    issues = subparsers.add_parser("issues", help="List persisted audit issues.")
    _add_project_args(issues)
    issues.add_argument("--severity", choices=["error", "warning", "info"])
    issues.add_argument("--rule-code")
    issues.add_argument("--status", choices=["open", "suppressed", "fixed"])
    issues.set_defaults(handler=_cmd_issues)

    context = subparsers.add_parser("context-pack", help="Build a retrieval context pack.")
    _add_project_args(context)
    context.add_argument("--query", required=True)
    context.add_argument("--budget-tokens", type=_nonneg_int, default=800)
    context.set_defaults(handler=_cmd_context_pack)

    ask = subparsers.add_parser("ask", help="Answer a lore question with grounded citations.")
    _add_project_args(ask)
    ask.add_argument("--query", required=True)
    ask.add_argument("--budget-tokens", type=_nonneg_int, default=800)
    ask.add_argument(
        "--agentic",
        action="store_true",
        help=(
            "Enable agentic retrieval (CRAG/Self-RAG-style sufficiency judge + query-rewrite "
            "multi-hop) — extra LLM calls per hop, so opt-in."
        ),
    )
    ask.add_argument(
        "--faithfulness",
        choices=["off", "offline", "llm"],
        default=os.getenv("OWCOPILOT_QA_FAITHFULNESS", "off"),
        help=(
            "Opt-in RAGAS-style entailment gate on the answer (annotate, not block). off: "
            "unchanged. offline: deterministic $0 lexical judge. llm: the answering model judges "
            "each claim (requires --llm-mode real). Low faithfulness never suppresses the answer; "
            "it flags unsupported claims for human review."
        ),
    )
    _add_llm_args(ask)
    ask.set_defaults(handler=_cmd_ask)

    overview = subparsers.add_parser(
        "build-overview",
        help="Build/refresh the GraphRAG macro-overview index (community + global reports).",
    )
    _add_project_args(overview)
    _add_llm_args(overview)
    overview.set_defaults(handler=_cmd_build_overview)

    impact = subparsers.add_parser(
        "impact", help="Preview the blast radius of a planned change (pure graph, no LLM)."
    )
    _add_project_args(impact)
    impact.add_argument(
        "--change",
        action="append",
        required=True,
        help=(
            "Change spec '<change_type>:<target_ref>', e.g. "
            "entity_delete:entity:npc_aldric or relation_change:entity:fac_caobang. "
            f"Change types: {', '.join(item.value for item in ChangeType)}."
        ),
    )
    impact.add_argument("--max-depth", type=_nonneg_int, default=2)  # BUG-7: was bare int
    impact.set_defaults(handler=_cmd_impact)

    suggest = subparsers.add_parser(
        "suggest",
        help="Propose shadow-validated fix candidates for a persisted audit issue.",
    )
    _add_project_args(suggest)
    suggest.add_argument("--issue-id", required=True, help="Issue id from `owcopilot issues`.")
    suggest.add_argument("--max-candidates", type=int, default=3)
    suggest.add_argument("--budget-tokens", type=_nonneg_int, default=600)
    _add_llm_args(suggest)
    suggest.set_defaults(handler=_cmd_suggest)

    repair_plan = subparsers.add_parser(
        "repair-plan",
        help=(
            "Search (MCTS) for the best sequence of shadow-validated deterministic fixes that "
            "clears the most audit errors. $0, reproducible; returns a plan only (never writes "
            "canon — apply each move via the human write path)."
        ),
    )
    _add_project_args(repair_plan)
    repair_plan.add_argument("--max-iterations", type=int, default=200)
    repair_plan.add_argument(
        "--max-depth", type=int, default=None, help="Plan-length cap (default: scales with errors)."
    )
    repair_plan.add_argument("--seed", type=int, default=0, help="RNG seed (reproducible search).")
    repair_plan.add_argument(
        "--llm-candidates",
        action="store_true",
        help=(
            "Widen the MCTS move set with LLM-proposed fix candidates (LATS-style LM proposer, "
            "arXiv:2310.04406). Off by default: the search stays $0/deterministic. Needs "
            "--llm-mode real; each model candidate is still shadow-validated and non-worsening-"
            "pruned like the deterministic fixers."
        ),
    )
    repair_plan.add_argument(
        "--max-llm-calls",
        type=_nonneg_int,
        default=32,
        help=(
            "Hard cap on LLM proposer calls when --llm-candidates is set (LATS budget knob). "
            "Once spent, the search falls back to the deterministic fixers."
        ),
    )
    _add_llm_args(repair_plan)
    repair_plan.set_defaults(handler=_cmd_repair_plan)

    apply_cmd = subparsers.add_parser(
        "apply", help="Apply a proposed patch to the content files (human write path)."
    )
    _add_project_args(apply_cmd)
    apply_cmd.add_argument("--patch-id", required=True)
    apply_cmd.add_argument(
        "--operator", required=True, help="Who applies the patch; recorded in the audit log."
    )
    apply_cmd.set_defaults(handler=_cmd_apply)

    rollback = subparsers.add_parser(
        "rollback", help="Roll back an applied patch using its stored inverse operations."
    )
    _add_project_args(rollback)
    rollback.add_argument("--patch-id", required=True)
    rollback.add_argument(
        "--operator", required=True, help="Who rolls back; recorded in the audit log."
    )
    rollback.set_defaults(handler=_cmd_rollback)

    draft = subparsers.add_parser(
        "draft", help="Draft one quest from a brief into the review queue (pending review)."
    )
    _add_project_args(draft)
    draft.add_argument("--brief", required=True)
    draft.add_argument("--budget-tokens", type=_nonneg_int, default=800)
    _add_llm_args(draft)
    draft.set_defaults(handler=_cmd_draft)

    expand = subparsers.add_parser(
        "expand",
        help=(
            "Grow more canon-grounded content (locations / NPCs / side quests) on an EXISTING "
            "world at one focus, into the review queue."
        ),
    )
    _add_project_args(expand)
    expand.add_argument(
        "--focus",
        required=True,
        help="Focus to deepen: region:<id> / faction:<id> / quest:<id> (or a bare existing id).",
    )
    expand.add_argument("--angle", default="", help="What tension to deepen (optional).")
    expand.add_argument("--pois", type=int, default=3, help="New locations to add.")
    expand.add_argument("--npcs", type=int, default=4, help="New secondary NPCs to add.")
    expand.add_argument("--quests", type=int, default=3, help="New side quests to add.")
    expand.add_argument("--budget-tokens", type=_nonneg_int, default=1800)
    expand.add_argument(
        "--refine-rounds",
        type=int,
        default=1,
        help="Quests-stage critique→refine rounds (0 disables the loop).",
    )
    _add_llm_args(expand)
    expand.set_defaults(handler=_cmd_expand)

    extract = subparsers.add_parser(
        "extract",
        help=(
            "Distill an unstructured manuscript (txt/md/docx/json) into a reviewable "
            "draft: entities, relations, plot beats and a gap list."
        ),
    )
    _add_project_args(extract)
    extract.add_argument("--input", required=True, help="Path to the manuscript file.")
    extract.add_argument("--title", help="Source title; defaults to the file stem.")
    extract.add_argument("--source-kind", default="文稿")
    extract.add_argument(
        "--fill-gaps",
        action="store_true",
        help="Ask the model to suggest values for missing fields and apply them.",
    )
    extract.add_argument(
        "--submit",
        action="store_true",
        help="Submit the draft to the review queue (otherwise print only).",
    )
    extract.add_argument(
        "--beats-as-quests",
        action="store_true",
        help="With --submit: also turn plot beats into quest skeletons.",
    )
    _add_llm_args(extract)
    extract.set_defaults(handler=_cmd_extract)

    barks = subparsers.add_parser(
        "barks", help="Generate lint-filtered bark variants into the review queue."
    )
    _add_project_args(barks)
    barks.add_argument("--speakers", required=True, help="Comma-separated speaker entity ids.")
    barks.add_argument("--topic", required=True)
    barks.add_argument("--variants", type=int, default=4, help="Variants per speaker.")
    barks.add_argument("--max-chars", type=int, default=40)
    barks.add_argument(
        "--allowed-entities",
        help="Comma-separated extra entity ids the bark text may reference.",
    )
    _add_llm_args(barks)
    barks.set_defaults(handler=_cmd_barks)

    review = subparsers.add_parser(
        "review", help="List or decide pending review items (the only AI-content write path)."
    )
    _add_project_args(review)
    review.add_argument("--accept", help="Review item id to accept.")
    review.add_argument("--reject", help="Review item id to reject.")
    review.add_argument("--operator", help="Required with --accept/--reject.")
    review.set_defaults(handler=_cmd_review)

    export = subparsers.add_parser(
        "export", help="Export project content as a data bundle + localization files."
    )
    _add_project_args(export)
    export.add_argument("--output-dir", required=True)
    export.add_argument(
        "--target-engine",
        choices=[target.value for target in EngineTarget],
        default=EngineTarget.GENERIC.value,
        help=(
            "Export target recorded in the manifest. 'generic' (default) is the checksummed "
            "content_bundle.json plus CSV/XLIFF localization. 'unreal' and 'unity' are supersets: "
            "each also writes the generic bundle, then adds an engine-side projection under an "
            "unreal/ (UE DataTable JSON + StringTable CSVs + row header) or unity/ "
            "(unity_import_manifest.json for the UPM importer) subdir."
        ),
    )
    export.add_argument(
        "--unity-localization",
        action="store_true",
        help=(
            "UNITY target only: also write an opt-in String Table Collection CSV under "
            "unity/localization/ in Unity's official com.unity.localization CSV format "
            "(Key + one column per locale), importable via the Localization Tables window's "
            "CSV import. Off by default; the rest of the unity/ projection is unchanged."
        ),
    )
    export.set_defaults(handler=_cmd_export)

    sync_back = subparsers.add_parser(
        "sync-back",
        help=(
            "Controlled engine back-flow: engine-edited quest rows (v2-shaped JSON array, as "
            "exchanged by the engine bridges) -> three-way diff vs canon + export baseline -> "
            "review queue. Conflicts never stage. Raw DataTable JSON goes through `recognize "
            "--source-format ue` instead."
        ),
    )
    _add_project_args(sync_back)
    sync_back.add_argument(
        "--input", required=True, help="Engine-side quest rows (v2-shaped JSON array)."
    )
    sync_back.add_argument(
        "--baseline",
        help=(
            "The engine JSON OWCopilot originally exported (the export-time snapshot). "
            "Enables conflict detection: divergence on BOTH sides is reported, never staged."
        ),
    )
    sync_back.add_argument(
        "--apply",
        action="store_true",
        help="Stage new + safely-changed quests into review (otherwise just print the plan).",
    )
    sync_back.add_argument(
        "--explain",
        action="store_true",
        help=(
            "Attach guided explanations for conflicts/warnings: deterministic diagnosis + "
            "nearest-id candidates always; an LLM narrative summary on top (fail-closed)."
        ),
    )
    _add_llm_args(sync_back)
    sync_back.set_defaults(handler=_cmd_sync_back)

    recognize = subparsers.add_parser(
        "recognize",
        help=(
            "Read a foreign game-project file (spreadsheet / articy / ink / Yarn / UE / Unity) and "
            "recognize its entities + relations into an editable plan; --apply stages into review."
        ),
    )
    _add_project_args(recognize)
    recognize.add_argument(
        "--source-format",
        required=True,
        choices=["table", "articy", "ink", "yarn", "ue", "unity"],
        help=(
            "table: .csv/.xlsx/.json rows with unknown columns. articy/ue/unity: a JSON export. "
            "ink/yarn: a narrative script. ue: UE DataTable JSON. unity: ScriptableObject JSON."
        ),
    )
    recognize.add_argument("--input", required=True, help="Path to the file to recognize.")
    recognize.add_argument(
        "--mapping",
        help="Path to a ColumnMapping JSON to override the inferred table mapping (table only).",
    )
    recognize.add_argument(
        "--apply",
        action="store_true",
        help="Stage new/changed entities+relations into review (otherwise just print the plan).",
    )
    recognize.add_argument("--operator", default="import", help="Operator recorded on apply.")
    recognize.set_defaults(handler=_cmd_recognize)

    eval_golden = subparsers.add_parser("eval-golden", help="Run the offline Golden World eval.")
    eval_golden.add_argument("--workspace", required=True)
    eval_golden.add_argument(
        "--output",
        help="Write the JSON result to this file as well as stdout.",
    )
    eval_golden.set_defaults(handler=_cmd_eval_golden)

    eval_acceptance = subparsers.add_parser(
        "eval-acceptance",
        help=(
            "Run the acceptance benchmark: 65-entity bilingual world, 25 seeded errors, "
            "impact recall, 30-query retrieval and QA gates. Offline, $0."
        ),
    )
    eval_acceptance.add_argument("--workspace", required=True)
    eval_acceptance.add_argument(
        "--output",
        help="Write the JSON result to this file as well as stdout.",
    )
    eval_acceptance.set_defaults(handler=_cmd_eval_acceptance)

    eval_repair = subparsers.add_parser(
        "eval-repair",
        help=(
            "Benchmark the MCTS repair planner vs a greedy baseline over the same move space "
            "(independent-fix worlds + a controlled interacting world). Offline, $0."
        ),
    )
    eval_repair.add_argument(
        "--output", help="Write the JSON result to this file as well as stdout."
    )
    eval_repair.set_defaults(handler=_cmd_eval_repair)

    eval_agent = subparsers.add_parser(
        "eval-agent",
        help=(
            "Five-dimension agent scorecard for task completion, character consistency, "
            "narrative plausibility, tool-call effectiveness, and safety."
        ),
    )
    eval_agent.add_argument(
        "--output", help="Write the JSON result to this file as well as stdout."
    )
    eval_agent.set_defaults(handler=_cmd_eval_agent)

    return parser


def _nonneg_float(value: str) -> float:
    """argparse type= helper: reject negative floats with a friendly message (P8)."""
    try:
        f = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a valid number") from exc
    if f < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0 (got {f})")
    return f


def _nonneg_int(value: str) -> int:
    """argparse type= helper: reject negative ints with a friendly message (P9)."""
    try:
        i = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a valid integer") from exc
    if i < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0 (got {i})")
    return i


def _add_project_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--content-root", required=True, help="Path to the v2 content root.")
    parser.add_argument(
        "--sqlite-path",
        help="Runtime SQLite path. Defaults to <content-root>/.owcopilot/runtime.sqlite.",
    )
    parser.add_argument("--output", help="Write the JSON result to this file as well as stdout.")


def _add_llm_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--llm-mode",
        choices=["offline", "real"],
        default=os.getenv("OWCOPILOT_LLM_MODE", "offline"),
        help=(
            "real: a configured OpenAI-compatible provider (the product path). offline: the "
            "deterministic doubles — a test/CI dry-run only, gated behind "
            "OWCOPILOT_ALLOW_OFFLINE_LLM so it is never a shipped product mode."
        ),
    )
    parser.add_argument(
        "--llm-model",
        default=os.getenv("OWCOPILOT_CHEAP_MODEL", "deepseek-v4-flash"),
        help="Model id for --llm-mode real.",
    )
    parser.add_argument(
        "--max-cost-usd",
        type=_nonneg_float,
        help="Soft budget: the cost_budget output flags over_budget when exceeded.",
    )


def _llm_gateway(
    args: argparse.Namespace,
    *,
    task: str,
    offline_provider: Any,
    real_json_mode: bool = True,
    extra_tasks: list[str] | None = None,
) -> tuple[LLMGateway, TelemetryCollector]:
    telemetry = TelemetryCollector()
    real = getattr(args, "llm_mode", "offline") == "real"
    if real:
        load_dotenv()  # pick up OPENAI_BASE_URL / OPENAI_API_KEY from .env; shell env wins
        # The ReAct agent needs free-form Thought/Action text, so its caller disables JSON mode —
        # otherwise the provider would force a single json_object and break the ReAct format.
        provider: Any = build_real_provider(model=args.llm_model, json_mode=real_json_mode)
    else:
        require_offline_llm_allowed()  # `--llm-mode offline` is a test/CI dry-run, not a product
        provider = offline_provider
    mapping = {task: "cheap"}
    for extra in extra_tasks or []:
        mapping[extra] = "cheap"
    gateway = LLMGateway(
        providers={"cheap": provider},
        router=StaticRouter(mapping=mapping),
        cache=NoOpCache(),
        telemetry=telemetry,
        max_retries=1 if real else 0,
        retry_backoff_seconds=1.0 if real else 0.0,
    )
    return gateway, telemetry


def _cmd_ingest(args: argparse.Namespace) -> int:
    with _project(args) as project:
        input_paths: list[str | Path] = [Path(path) for path in args.input]
        mapping_doc = _load_mapping_doc(Path(args.field_mapping)) if args.field_mapping else None
        if mapping_doc is None:
            result = run_ingest(
                project,
                input_paths,
                dry_run=not args.write,
                write_non_conflicting=args.skip_conflicts,
            )
            return _emit(
                {
                    "dry_run": result.dry_run,
                    "content_hash_before": result.content_hash_before,
                    "content_hash_after": result.content_hash_after,
                    "incoming_count": result.incoming_count,
                    "has_errors": result.has_errors,
                    "changes": [change.model_dump(mode="json") for change in result.changes],
                    "issues": [issue.model_dump(mode="json") for issue in result.issues],
                    "cost_budget": _deterministic_cost_budget("ingest"),
                },
                args,
            )
        raw_by_input = [
            (
                path,
                _parse_with_mapping(mapping_doc, Path(path)),
            )
            for path in input_paths
        ]
        result = ingest_raw_objects(
            [raw for _path, raws in raw_by_input for raw in raws],
            store=project.content_store,
            dry_run=not args.write,
            write_non_conflicting=args.skip_conflicts,
        )
        if args.write and (not result.has_errors or args.skip_conflicts):
            project.reload()
        return _emit(
            {
                "dry_run": result.dry_run,
                "content_hash_before": result.content_hash_before,
                "content_hash_after": result.content_hash_after,
                "incoming_count": result.incoming_count,
                "has_errors": result.has_errors,
                "changes": [change.model_dump(mode="json") for change in result.changes],
                "issues": [issue.model_dump(mode="json") for issue in result.issues],
                "per_input": [
                    {
                        "input": str(path),
                        "raw_count": len(raws),
                        "mapping_applied": bool(raws and raws[0].data.get("kind")),
                    }
                    for path, raws in raw_by_input
                ],
                "cost_budget": _deterministic_cost_budget("ingest"),
            },
            args,
        )


def _cmd_audit(args: argparse.Namespace) -> int:
    with _project(args) as project:
        if args.baseline:
            project.audit_runner = AuditRunner(
                build_default_rule_registry(),
                baseline=_load_baseline(Path(args.baseline)),
            )
        result = run_full_audit(project, persist=not args.no_persist)
        payload: dict[str, Any] = {
            "content_hash": content_hash(project.bundle),
            "audit_run": result.run.model_dump(mode="json"),
            "issues": [issue.model_dump(mode="json") for issue in result.issues],
            "open_errors": len(result.open_errors),
            "cost_budget": _deterministic_cost_budget("audit_project"),
        }
        if args.update_baseline:
            baseline = _load_baseline(Path(args.baseline)) if args.baseline else AuditBaseline()
            for issue in result.issues:
                if issue.status is IssueStatus.OPEN:
                    baseline.add(issue)
            baseline_path = Path(args.update_baseline)
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            baseline_path.write_text(
                json.dumps(
                    {"fingerprints": sorted(baseline.fingerprints)},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            payload["baseline_written"] = str(baseline_path)
            payload["baseline_size"] = len(baseline.fingerprints)
        if args.markdown_report:
            report_path = Path(args.markdown_report)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                render_audit_markdown(result, content_hash=payload["content_hash"]),
                encoding="utf-8",
            )
            payload["markdown_report"] = str(report_path)
        _emit(payload, args)
        return 1 if args.fail_on_error and result.open_errors else 0


def _cmd_readiness(args: argparse.Namespace) -> int:
    with _project(args) as project:
        report = assess_readiness(project.bundle)
        payload = report.model_dump(mode="json")
        items = payload["items"]
        if args.kind:
            items = [it for it in items if it["kind"] == args.kind]
        if args.only_incomplete:
            items = [it for it in items if not it["ready"]]
        payload["items"] = items
        payload["cost_budget"] = _deterministic_cost_budget("readiness")
        return _emit(payload, args)


def _read_state_file(path: str | None) -> dict | None:
    if not path:
        return None
    state = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise ValueError(f"--initial-state 应是 JSON 对象（变量名→值）：{path}")
    return state


def _split_choices(raw: str | None) -> list[str]:
    return [c.strip() for c in (raw or "").split(",") if c.strip()]


def _cmd_quest_simulate(args: argparse.Namespace) -> int:
    from ..logic import simulate_quest, walk_dialogue_tree

    initial_state = _read_state_file(args.initial_state)
    choices = _split_choices(args.choices)
    with _project(args) as project:
        quest = project.bundle.quests.get(args.quest_id)
        if quest is None:
            raise ValueError(
                f"任务「{args.quest_id}」不存在。"
                "先用 `owcopilot readiness --content-root <root> --kind quest` 列出可用的任务 id。"
            )
        walk_dump = None
        if args.dialogue_tree:
            tree = project.bundle.dialogue_trees.get(args.dialogue_tree)
            if tree is None:
                raise ValueError(
                    f"对白树「{args.dialogue_tree}」不存在。可用 id："
                    f"{'、'.join(sorted(project.bundle.dialogue_trees)) or '（无）'}"
                )
            walk = walk_dialogue_tree(tree, choices=_split_choices(args.dialogue_choices) or None)
            walk_dump = walk.model_dump(mode="json")
            # composition contract: dialogue outcome seeds the quest; explicit overrides win
            merged = dict(walk.final_state)
            merged.update(initial_state or {})
            initial_state = merged
        run = simulate_quest(quest, choices=choices or None, initial_state=initial_state)
        payload = {
            **run.model_dump(mode="json"),
            "cost_budget": _deterministic_cost_budget("quest_simulate"),
        }
        if walk_dump is not None:
            payload["dialogue_walk"] = walk_dump
        return _emit(payload, args)


def _cmd_dialogue_walk(args: argparse.Namespace) -> int:
    from ..logic import explore_dialogue_tree, walk_dialogue_tree

    initial_state = _read_state_file(args.initial_state)
    with _project(args) as project:
        tree = project.bundle.dialogue_trees.get(args.tree_id)
        if tree is None:
            raise ValueError(
                f"对白树「{args.tree_id}」不存在。可用 id："
                f"{'、'.join(sorted(project.bundle.dialogue_trees)) or '（无）'}"
            )
        if args.explore:
            result = explore_dialogue_tree(tree, initial_state=initial_state).model_dump(
                mode="json"
            )
        else:
            result = walk_dialogue_tree(
                tree, choices=_split_choices(args.choices) or None, initial_state=initial_state
            ).model_dump(mode="json")
        return _emit({**result, "cost_budget": _deterministic_cost_budget("dialogue_walk")}, args)


def _cmd_quest_explore(args: argparse.Namespace) -> int:
    from ..logic import explore_quest

    with _project(args) as project:
        quest = project.bundle.quests.get(args.quest_id)
        if quest is None:
            raise ValueError(
                f"任务「{args.quest_id}」不存在。"
                "先用 `owcopilot readiness --content-root <root> --kind quest` 列出可用的任务 id。"
            )
        report = explore_quest(quest, max_states=args.max_states, max_depth=args.max_depth)
        return _emit(
            {
                **report.model_dump(mode="json"),
                "cost_budget": _deterministic_cost_budget("quest_explore"),
            },
            args,
        )


def _cmd_quality_harness(args: argparse.Namespace) -> int:
    with _project(args) as project:
        report = run_quality_harness(
            project,
            propose_fixes=not args.no_propose_fixes,
            max_issues=args.max_issues,
            max_candidates_per_issue=args.max_candidates_per_issue,
        )
        return _emit(
            {
                **report.model_dump(mode="json"),
                "cost_budget": _deterministic_cost_budget("quality_harness"),
            },
            args,
        )


def _recall_agent_memory(
    episodic: Any,
    store: Any,
    embedder: Any,
    goal: str,
    *,
    enabled: bool,
) -> list[str] | None:
    """Assemble the recalled episodic context for one agent run, or ``None`` when there is none.

    Combines the two recall channels into the single ``reflections`` list ``ReActAgent.run`` already
    accepts: the MemGPT similar-run block first (as one context block), then the Reflexion buffer's
    reflections. ``enabled=False`` (--no-recall) short-circuits WITHOUT calling recall at all, so
    the read side is genuinely off. Fail-open: any recall error is logged and treated as no hits, so
    memory retrieval can never break the agent run. Empty ⇒ ``None`` (byte-identical prompt)."""
    if not enabled:
        return None
    blocks: list[str] = []
    try:
        run_hits = episodic.recall_similar_runs(store, embedder, goal)
        if run_hits:
            blocks.append(episodic.format_history_block(run_hits))
        blocks.extend(hit.reflection for hit in episodic.recall_reflections(store, embedder, goal))
    except Exception as exc:  # noqa: BLE001 — recall is best-effort; never break the run
        logger.warning("agent memory recall failed (%s); running without recalled context", exc)
        return None
    return blocks or None


def _cmd_agent(args: argparse.Namespace) -> int:
    from ..agent import ReActAgent, episodic, persist_agent_run
    from ..agent.offline import OfflineReactProvider
    from ..mcp_server import tools as tools_mod
    from ..retrieval.embedding import resolve_embedder
    from ..skills import default_skill_registry

    if not args.goal.strip():
        raise ValueError(
            "--goal 不能为空字符串，请描述 agent 需要完成的目标。"
            "例如: --goal 'check quest consistency'"
        )
    content_root = Path(args.content_root)
    if not content_root.exists():
        raise FileNotFoundError(f"content root does not exist: {content_root}")
    sqlite_path = str(args.sqlite_path or _default_sqlite_path(content_root))
    if sqlite_path != ":memory:":
        Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
    # The agent reasons in free-form ReAct text, so disable JSON mode on the real provider.
    gateway, telemetry = _llm_gateway(
        args, task="agent_react", offline_provider=OfflineReactProvider(), real_json_mode=False
    )
    # Open one shared ProjectContext for the whole ReAct run so tool calls reuse the same
    # parsed project state and see writes from earlier steps.
    project = ProjectContext.open(content_root, sqlite_path=sqlite_path)
    token = tools_mod._shared_project.set(project)
    try:
        registry = default_skill_registry(content_root=str(content_root), sqlite_path=sqlite_path)
        # Wire --skills allowlist into ReActAgent so users can restrict tool access.
        # None means "all skills"; an empty list would restrict to none.
        allowed_skills: set[str] | None = set(args.skills) if args.skills is not None else None
        # Cross-validate --skills against the registry; warn (not fail) on unknown names
        # so the user knows the typo/stale skill name is silently excluded from the manifest.
        if allowed_skills is not None:
            unknown_skills = sorted(allowed_skills - set(registry.names()))
            if unknown_skills:
                print(
                    json.dumps(
                        {
                            "warning": "unknown skill(s) in --skills allowlist (will be ignored)",
                            "unknown_skills": unknown_skills,
                            "known_skills": registry.names(),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                )
        # Episodic-memory READ side: recall semantically-similar past runs (MemGPT recall storage)
        # and reflections (Reflexion buffer) and hand them to the actor's reflection slot — the one
        # existing episodic-memory injection channel (no new injection surface). Default on;
        # --no-recall disables it. Empty recall ⇒ reflections=None ⇒ byte-identical prompt.
        embedder = resolve_embedder()
        reflections = _recall_agent_memory(
            episodic, project.sqlite_store, embedder, args.goal, enabled=not args.no_recall
        )
        result = ReActAgent(
            gateway=gateway,
            registry=registry,
            max_steps=args.max_steps,
            allowed_skills=allowed_skills,
            early_stopping=args.early_stopping,
        ).run(args.goal, reflections=reflections)
        # T19: persist the run's full trajectory (episodic/recall memory WRITE side) while the
        # project store is still open. Best-effort — a failure logs a WARNING and yields None,
        # so the agent's already-completed result is emitted unchanged either way.
        agent_run_id = persist_agent_run(project.sqlite_store, result)
        # Recall WRITE side: embed (goal + outcome) so a FUTURE run can recall this one. Runs only
        # once the transcript is stored (needs the run_id) and is fail-open on the embed.
        if agent_run_id is not None:
            episodic.persist_run_embedding(
                project.sqlite_store,
                embedder,
                run_id=agent_run_id,
                goal=result.goal,
                final_answer=result.final_answer,
            )
    finally:
        tools_mod._shared_project.reset(token)
        project.close()
    telemetry_summary = telemetry.summary()
    cost_budget = summarize_workflow(
        [llm_step("agent_react", telemetry_summary)], budget_usd=args.max_cost_usd
    ).budget
    return _emit(
        {
            **result.model_dump(mode="json"),
            "agent_run_id": agent_run_id,
            "skills": registry.names(),
            "llm_mode": args.llm_mode,
            "telemetry": telemetry_summary,
            "cost_budget": cost_budget.model_dump(mode="json"),
        },
        args,
    )


def _cmd_multi_agent(args: argparse.Namespace) -> int:
    from ..agent.offline import OfflineReactProvider
    from ..mcp_server import tools as tools_mod
    from ..multi_agent import MultiAgentSession
    from ..retrieval.embedding import resolve_embedder
    from ..skills import default_skill_registry

    if not args.goal.strip():
        raise ValueError(
            "--goal 不能为空字符串，请描述多 agent 系统要达成的目标。"
            "例如: --goal '让世界达到可导出状态'"
        )
    content_root = Path(args.content_root)
    if not content_root.exists():
        raise FileNotFoundError(f"content root does not exist: {content_root}")
    sqlite_path = str(args.sqlite_path or _default_sqlite_path(content_root))
    if sqlite_path != ":memory:":
        Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
    # Agents reason in free-form ReAct text, so disable JSON mode on the real provider.
    # The orchestrator's decompose task expects JSON but degrades gracefully to the
    # built-in subtasks when the response is not parseable (e.g. the offline double).
    gateway, telemetry = _llm_gateway(
        args, task="agent_react", offline_provider=OfflineReactProvider(), real_json_mode=False
    )
    # Open one shared ProjectContext for the whole multi-agent session so every worker sees the
    # same parsed project state and any issues persisted by earlier workers.
    project = ProjectContext.open(content_root, sqlite_path=sqlite_path)
    token = tools_mod._shared_project.set(project)
    try:
        registry = default_skill_registry(content_root=str(content_root), sqlite_path=sqlite_path)
        # Wire the project store + embedder so the Reflexion buffer persists across sessions: a
        # retry's distilled reflection is saved to the runtime DB, and a later worker dispatched on
        # a similar subtask recalls it into its reflection slot. Absent these, the session behaves
        # exactly as before (no persistence, no recall).
        session = MultiAgentSession(
            gateway=gateway,
            registry=registry,
            store=project.sqlite_store,
            embedder=resolve_embedder(),
        )
        try:
            report = session.run(args.goal)
            # The blackboard message flow is the product-facing proof of true multi-agent
            # collaboration: each row is attributed (from→to) and ordered.
            flow = [
                {
                    "msg_type": msg.msg_type,
                    "from_agent": msg.from_agent,
                    "to_agent": msg.to_agent,
                    "status": msg.status,
                }
                for msg in session.blackboard.session_flow(report.session_id)
            ]
        finally:
            session.close()
    finally:
        tools_mod._shared_project.reset(token)
        project.close()
    telemetry_summary = telemetry.summary()
    cost_budget = summarize_workflow(
        [llm_step("multi_agent", telemetry_summary)], budget_usd=args.max_cost_usd
    ).budget
    return _emit(
        {
            "session_id": report.session_id,
            "goal": report.goal,
            "participants": report.participants,
            "blackboard_flow": flow,
            "worker_summaries": report.worker_summaries,
            "verifier_verdicts": report.verifier_verdicts,
            "synthesis": report.synthesis,
            # ② Surface decomposition degradation to CLI consumers — no silent downgrade
            "decomposition_degraded": report.decomposition_degraded,
            "llm_mode": args.llm_mode,
            "telemetry": telemetry_summary,
            "cost_budget": cost_budget.model_dump(mode="json"),
        },
        args,
    )


def _cmd_agent_history(args: argparse.Namespace) -> int:
    with _project(args) as project:
        store = project.sqlite_store
        if args.run_id:
            run = store.get_agent_run(args.run_id)
            if run is None:
                raise ValueError(
                    f"未找到 agent run '{args.run_id}'。"
                    "先用 `owcopilot agent-history --content-root <root>` 列出可用的 run id。"
                )
            return _emit(
                {"run": run, "cost_budget": _deterministic_cost_budget("agent_history")},
                args,
            )
        runs = store.list_agent_runs(limit=args.limit)
        return _emit(
            {
                "count": len(runs),
                "runs": runs,
                "cost_budget": _deterministic_cost_budget("agent_history"),
            },
            args,
        )


def _cmd_issues(args: argparse.Namespace) -> int:
    with _project(args) as project:
        issues = project.sqlite_store.list_issues(
            severity=args.severity,
            rule_code=args.rule_code,
            status=args.status,
        )
        return _emit(
            {
                "count": len(issues),
                "issues": [issue.model_dump(mode="json") for issue in issues],
                "cost_budget": _deterministic_cost_budget("list_issues"),
            },
            args,
        )


def _cmd_context_pack(args: argparse.Namespace) -> int:
    with _project(args) as project:
        pack = project.context_builder.build(args.query, budget_tokens=args.budget_tokens)
        return _emit(
            {
                "query": pack.query,
                "budget_tokens": pack.budget_tokens,
                "refs": pack.refs,
                "hits": [hit.model_dump(mode="json") for hit in pack.hits],
                "cost_budget": _deterministic_cost_budget("build_context_pack"),
            },
            args,
        )


def _cmd_ask(args: argparse.Namespace) -> int:
    if args.faithfulness == "llm" and args.llm_mode != "real":
        # No silent downgrade: the llm judge would otherwise be the offline QA double, which is not
        # an entailment judge and would mislabel good answers. Fail fast, before any work.
        raise ValueError(
            "--faithfulness llm 需要 --llm-mode real（用真实模型当判官）；"
            "离线场景请改用 --faithfulness offline（确定性 $0 判官）。"
        )
    with _project(args) as project:
        gateway, telemetry = _llm_gateway(
            args,
            task="qa_answer",
            offline_provider=OfflineQAProvider(),
            extra_tasks=["qa_expand"],
        )
        service = LoreQAService(
            gateway=gateway,
            # also recall the GraphRAG macro-overview reports (a no-op until `build-overview` runs)
            context_builder=project.qa_context_builder(),
            bundle=project.bundle,
            expand=(args.llm_mode == "real"),
            agentic=args.agentic,
            faithfulness=args.faithfulness,
        )
        answer = service.ask(args.query, budget_tokens=args.budget_tokens)
        telemetry_summary = telemetry.summary()
        cost_budget = summarize_workflow(
            [llm_step("ask_lore", telemetry_summary)],
            budget_usd=args.max_cost_usd,
        ).budget
        return _emit(
            {
                "answer": answer.model_dump(mode="json"),
                "llm_mode": args.llm_mode,
                "llm_model": args.llm_model if args.llm_mode == "real" else "offline",
                "telemetry": telemetry_summary,
                "cost_budget": cost_budget.model_dump(mode="json"),
            },
            args,
        )


def _cmd_build_overview(args: argparse.Namespace) -> int:
    with _project(args) as project:
        gateway, telemetry = _llm_gateway(
            args, task="community_report", offline_provider=OfflineCommunityReportProvider()
        )
        result = CommunityIndexService(
            gateway=gateway, store=project.sqlite_store, bundle=project.bundle
        ).build()
        telemetry_summary = telemetry.summary()
        cost_budget = summarize_workflow(
            [llm_step("build_overview", telemetry_summary)], budget_usd=args.max_cost_usd
        ).budget
        return _emit(
            {
                "community_count": result.community_count,
                "regenerated": result.regenerated,
                "reports": [r.model_dump(mode="json") for r in result.reports],
                "llm_mode": args.llm_mode,
                "telemetry": telemetry_summary,
                "cost_budget": cost_budget.model_dump(mode="json"),
            },
            args,
        )


def _cmd_export(args: argparse.Namespace) -> int:
    with _project(args) as project:
        target_engine = EngineTarget(args.target_engine)
        output_dir = Path(args.output_dir) / target_engine.value
        assert_export_ready(project)
        manifest = export_content_bundle(
            project.bundle,
            output_dir,
            target_engine=target_engine,
            unity_localization=args.unity_localization,
        )
        return _emit(
            {
                "output_dir": str(output_dir),
                "manifest": manifest.model_dump(mode="json"),
                "cost_budget": _deterministic_cost_budget("export_project"),
            },
            args,
        )


def _cmd_sync_back(args: argparse.Namespace) -> int:
    from ..app.actions import sync_back_action
    from ..assist.offline import OfflineEngineExplainProvider

    gateway = None
    if args.explain:
        gateway, _telemetry = _llm_gateway(
            args,
            task="engine_explain",
            offline_provider=OfflineEngineExplainProvider(),
            real_json_mode=False,
        )
    result = sync_back_action(
        args.content_root,
        input_path=args.input,
        baseline_path=args.baseline,
        apply=args.apply,
        explain=args.explain,
        gateway=gateway,
        sqlite_path=getattr(args, "sqlite_path", None),
    )
    return _emit(result, args)


def _cmd_recognize(args: argparse.Namespace) -> int:
    from ..app.actions import recognize_import_action

    mapping = json.loads(Path(args.mapping).read_text(encoding="utf-8")) if args.mapping else None
    result = recognize_import_action(
        args.content_root,
        source_format=args.source_format,
        input_path=args.input,
        field_mapping=mapping,
        apply=args.apply,
        operator=args.operator,
        sqlite_path=args.sqlite_path,
    )
    _emit(result, args)
    return 0


def _cmd_eval_golden(args: argparse.Namespace) -> int:
    report = run_golden_evaluation(args.workspace)
    _emit(report.model_dump(mode="json"), args)
    return 0 if report.passed else 1


def _cmd_eval_repair(args: argparse.Namespace) -> int:
    from ..evaluation.repair_bench import run_repair_benchmark

    rows = run_repair_benchmark()
    return _emit(
        {
            "comparisons": [row.model_dump(mode="json") for row in rows],
            "invariant_mcts_never_worse": all(row.mcts_at_least_greedy for row in rows),
            "cost_budget": _deterministic_cost_budget("eval_repair"),
        },
        args,
    )


def _cmd_eval_acceptance(args: argparse.Namespace) -> int:
    from ..qa.offline_entailment import OfflineEntailmentJudge

    # Default the faithfulness gate to the deterministic offline judge so it runs without
    # requiring a configured model provider.
    report = run_acceptance_evaluation(args.workspace, faithfulness_judge=OfflineEntailmentJudge())
    _emit(report.model_dump(mode="json"), args)
    return 0 if report.passed else 1


def _cmd_prompts(args: argparse.Namespace) -> int:
    """List the prompt registry, or show one prompt's full text (+metadata) with --show."""
    from ..prompts import prompt_registry

    registry = prompt_registry()
    if args.show:
        spec = registry.get(args.show)
        if spec is None:
            known = ", ".join(sorted(registry))
            raise ValueError(f"unknown prompt '{args.show}'. Registered prompts: {known}")
        payload: dict[str, Any] = {
            "name": spec.name,
            "purpose": spec.purpose,
            "task": spec.task,
            "kind": spec.kind,
            "output_contract": spec.output_contract,
            "source": spec.source,
            "version": spec.version,
            "text": spec.text(),  # None for builders — the source function is authoritative
        }
        return _emit(payload, args)
    return _emit(
        {
            "prompts": [
                {
                    "name": spec.name,
                    "purpose": spec.purpose,
                    "task": spec.task,
                    "kind": spec.kind,
                    "version": spec.version,
                    "source": spec.source,
                }
                for spec in registry.values()
            ],
            "count": len(registry),
        },
        args,
    )


def _cmd_memory_report(args: argparse.Namespace) -> int:
    """Surface review queue, reviewer calibration, and lesson feedback state for a workspace."""
    with _project(args) as project:
        queue = ReviewQueue(project.sqlite_store)
        pending = queue.list_pending()
        resolved = queue.list_resolved()
        calibration = build_calibration_report(resolved)
        payload = {
            "agent_memory_loop": {
                "stages": [
                    "review_queue (the only canon-write path)",
                    "human accept/reject decision",
                    "calibration (Wilson false-pass rate over critic 'pass' verdicts)",
                    "lessons (distilled from calibration, fed back into critic/generation prompts)",
                    "retrieval over the versioned content store",
                ],
                "review_queue": {
                    "pending": len(pending),
                    "resolved": len(resolved),
                    "accepted": sum(1 for item in resolved if item.status == "accepted"),
                    "rejected": sum(1 for item in resolved if item.status == "rejected"),
                },
                "calibration": calibration.model_dump(mode="json"),
            },
            "cost_budget": _deterministic_cost_budget("memory_report"),
        }
        if args.write_lessons:
            from ..assist.lessons import extract_lessons_from_report

            payload["agent_memory_loop"]["lessons_written"] = extract_lessons_from_report(
                calibration, project.sqlite_store
            )
        return _emit(payload, args)


def _cmd_lessons(args: argparse.Namespace) -> int:
    """Manage the cross-session lesson archive (currently: revoke one lesson).

    Revocation soft-deletes a lesson so it is no longer injected into generation/critique prompts.
    A missing id raises a guided error (surfaced by main() as a JSON error), never a silent no-op.
    """
    from ..app.actions import revoke_lesson_action

    # args.action is constrained to "revoke" by the parser; branch kept explicit for future actions.
    result = revoke_lesson_action(
        args.content_root,
        lesson_id=args.lesson_id,
        reason=args.reason,
        sqlite_path=args.sqlite_path,
    )
    return _emit(result, args)


def _cmd_eval_agent(args: argparse.Namespace) -> int:
    from ..evaluation.agent_eval import AGENT_EVAL_FLOOR, run_jd_dimension_scorecard

    # The five dimensions are deterministic; a drop below the floor indicates a regression.
    scorecard = run_jd_dimension_scorecard()
    _emit(scorecard.model_dump(mode="json"), args)
    return 0 if scorecard.overall >= AGENT_EVAL_FLOOR else 1


def _cmd_impact(args: argparse.Namespace) -> int:
    with _project(args) as project:
        changes: list[Change] = []
        for spec in args.change:
            change_type, _, target_ref = spec.partition(":")
            try:
                parsed_type = ChangeType(change_type)
            except ValueError as e:
                raise ValueError(
                    f"unknown change type '{change_type}'; expected one of: "
                    + ", ".join(item.value for item in ChangeType)
                ) from e
            if not target_ref:
                raise ValueError(f"change spec '{spec}' is missing a target_ref")
            changes.append(Change(change_type=parsed_type, target_ref=target_ref))
        result = ImpactAnalyzer(project.graph).analyze(
            ChangeSet(changes=changes), max_depth=args.max_depth
        )
        return _emit(
            {
                "changes": [change.model_dump(mode="json") for change in changes],
                "must_change": [
                    item.model_dump(mode="json")
                    for item in result.by_level(ImpactLevel.MUST_CHANGE)
                ],
                "suggest_check": [
                    item.model_dump(mode="json")
                    for item in result.by_level(ImpactLevel.SUGGEST_CHECK)
                ],
                "total": len(result.items),
                "cost_budget": _deterministic_cost_budget("impact_of"),
            },
            args,
        )


def _cmd_suggest(args: argparse.Namespace) -> int:
    with _project(args) as project:
        issue = find_issue(project, args.issue_id)
        telemetry = TelemetryCollector()
        gateway = None
        if args.llm_mode == "real":
            gateway, telemetry = _llm_gateway(args, task="patch_suggest", offline_provider=None)
        result = suggest_for_issue(
            project,
            issue,
            gateway=gateway,
            max_candidates=args.max_candidates,
            budget_tokens=args.budget_tokens,
        )
        telemetry_summary = telemetry.summary()
        cost_budget = (
            summarize_workflow(
                [llm_step("patch_suggest", telemetry_summary)], budget_usd=args.max_cost_usd
            ).budget
            if result.used_llm
            else summarize_workflow([deterministic_step("patch_suggest")]).budget
        )
        return _emit(
            {
                "issue_id": args.issue_id,
                "candidates": [
                    {
                        "patch_id": ranked.candidate.id,
                        "source": ranked.source,
                        "target_resolved": ranked.target_resolved,
                        "resolved_error_count": len(ranked.resolved_errors),
                        "ops": [op.model_dump(mode="json") for op in ranked.candidate.ops],
                        "rationale": ranked.candidate.rationale,
                    }
                    for ranked in result.candidates
                ],
                "rejected_count": result.rejected_count,
                "parse_failed": result.parse_failed,
                "used_llm": result.used_llm,
                "llm_mode": args.llm_mode,
                "telemetry": telemetry_summary,
                "cost_budget": cost_budget.model_dump(mode="json"),
            },
            args,
        )


def _cmd_repair_plan(args: argparse.Namespace) -> int:
    from ..patches import PatchSuggestService, deterministic_candidates
    from ..patches.search import CandidateProvider, plan_repairs

    with _project(args) as project:
        telemetry = TelemetryCollector()
        candidate_provider: CandidateProvider = deterministic_candidates
        used_llm = False
        # Opt-in LATS-style widening: build the LLM proposer only in real mode (mirrors `suggest`,
        # which has no offline double for patch_suggest — the deterministic fixers ARE its $0 path).
        if args.llm_candidates and args.llm_mode == "real":
            gateway, telemetry = _llm_gateway(args, task="patch_suggest", offline_provider=None)
            candidate_provider = PatchSuggestService(
                bundle=project.bundle,
                audit_runner=project.audit_runner,
                gateway=gateway,
                context_builder=project.context_builder,
            ).as_candidate_provider(max_llm_calls=args.max_llm_calls)
            used_llm = True
        plan = plan_repairs(
            project.bundle,
            project.audit_runner,
            max_iterations=args.max_iterations,
            max_depth=args.max_depth,
            seed=args.seed,
            candidate_provider=candidate_provider,
        )
        telemetry_summary = telemetry.summary()
        cost_budget = (
            summarize_workflow(
                [llm_step("patch_suggest", telemetry_summary)], budget_usd=args.max_cost_usd
            ).budget
            if used_llm
            else summarize_workflow([deterministic_step("repair_plan")]).budget
        )
        return _emit(
            {
                **plan.model_dump(mode="json"),
                "used_llm": used_llm,
                "llm_mode": args.llm_mode,
                "cost_budget": cost_budget.model_dump(mode="json"),
            },
            args,
        )


def _cmd_apply(args: argparse.Namespace) -> int:
    with _project(args) as project:
        outcome = apply_patch_workflow(project, args.patch_id, operator=args.operator)
        if not outcome.applied:
            _emit(
                {
                    "applied": False,
                    "patch_id": outcome.patch_id,
                    "reason": outcome.reason,
                    "introduced_errors": outcome.introduced_errors,
                    "cost_budget": _deterministic_cost_budget("apply_patch"),
                },
                args,
            )
            return 1
        return _emit(
            {
                "applied": True,
                "patch_id": outcome.patch_id,
                "applied_by": args.operator,
                "rollback_ops_count": outcome.rollback_ops_count,
                "resolved_errors": outcome.resolved_errors,
                "post_audit_open_errors": outcome.post_audit_open_errors,
                "cost_budget": _deterministic_cost_budget("apply_patch"),
            },
            args,
        )


def _cmd_rollback(args: argparse.Namespace) -> int:
    with _project(args) as project:
        outcome = rollback_patch_workflow(project, args.patch_id, operator=args.operator)
        return _emit(
            {
                "rolled_back": outcome.rolled_back,
                "patch_id": outcome.patch_id,
                "rolled_back_by": args.operator,
                "post_audit_open_errors": outcome.post_audit_open_errors,
                "cost_budget": _deterministic_cost_budget("rollback_patch"),
            },
            args,
        )


def _cmd_extract(args: argparse.Namespace) -> int:
    # Reuses the shared app actions so CLI and the web UI behave identically.
    from ..app.actions import (
        fill_extraction_gaps_action,
        run_extraction_action,
        submit_extraction_action,
    )
    from ..extraction import decode_document_bytes

    source = Path(args.input)
    text = decode_document_bytes(source.read_bytes(), source.name)
    title = args.title or source.stem
    result = run_extraction_action(
        args.content_root,
        title=title,
        text=text,
        source_kind=args.source_kind,
        sqlite_path=args.sqlite_path,
        llm_mode=args.llm_mode,
        llm_model=args.llm_model,
    )
    draft = result["draft"]
    if args.fill_gaps and draft.get("gaps"):
        filled = fill_extraction_gaps_action(
            args.content_root,
            draft=draft,
            sqlite_path=args.sqlite_path,
            llm_mode=args.llm_mode,
            llm_model=args.llm_model,
        )
        draft = filled["draft"]
        answers = {
            gap["ref"]: gap["suggestion"] for gap in draft.get("gaps", []) if gap.get("suggestion")
        }
    else:
        answers = {}
    payload: dict[str, Any] = {
        "draft": draft,
        "stats": result["stats"],
        "llm_mode": args.llm_mode,
        "cost_budget": result["cost_budget"],
    }
    if args.submit:
        submitted = submit_extraction_action(
            args.content_root,
            draft=draft,
            answers=answers,
            include_beats_as_quests=args.beats_as_quests,
            sqlite_path=args.sqlite_path,
        )
        payload["review_item_id"] = submitted["review_item_id"]
        payload["open_gaps"] = submitted["open_gaps"]
        payload["issues"] = submitted["issues"]
    return _emit(payload, args)


def _cmd_draft(args: argparse.Namespace) -> int:
    with _project(args) as project:
        gateway, telemetry = _llm_gateway(
            args, task="quest_draft", offline_provider=OfflineQuestDraftProvider()
        )
        service = QuestDraftService(
            gateway=gateway,
            context_builder=project.context_builder,
            audit_runner=project.audit_runner,
            bundle=project.bundle,
        )
        result = service.draft_quest(args.brief, budget_tokens=args.budget_tokens)
        queue = ReviewQueue(project.sqlite_store)
        item = queue.add_quest_draft(
            result.quest.model_dump(mode="json", exclude_none=True),
            issue_refs=[issue_fingerprint(issue) for issue in result.issues],
        )
        telemetry_summary = telemetry.summary()
        return _emit(
            {
                "quest": result.quest.model_dump(mode="json", exclude_none=True),
                "issues": [issue.model_dump(mode="json") for issue in result.issues],
                "context_refs": result.context_refs,
                "review_item_id": item.id,
                "review_status": "pending_review",
                "llm_mode": args.llm_mode,
                "telemetry": telemetry_summary,
                "cost_budget": summarize_workflow(
                    [llm_step("quest_draft", telemetry_summary)], budget_usd=args.max_cost_usd
                ).budget.model_dump(mode="json"),
            },
            args,
        )


def _cmd_expand(args: argparse.Namespace) -> int:
    # Reuses the shared app action so CLI, REST and the web UI behave identically.
    from ..app.actions import run_world_expand_action

    result = run_world_expand_action(
        args.content_root,
        brief={
            "focus_ref": args.focus,
            "angle": args.angle,
            "poi_count": args.pois,
            "npc_count": args.npcs,
            "quest_count": args.quests,
        },
        sqlite_path=args.sqlite_path,
        budget_tokens=args.budget_tokens,
        llm_mode=args.llm_mode,
        llm_model=args.llm_model,
        refine_rounds=args.refine_rounds,
    )
    return _emit(
        {
            "id": result["id"],
            "focus_ref": result["focus_ref"],
            "focus_label": result["focus_label"],
            "angle": result["angle"],
            "counts": result["counts"],
            "grounding": result["grounding"],
            "refine_trail": result["refine_trail"],
            "issues": result["issues"],
            "review_item_id": result["review_item_id"],
            "review_status": "pending_review",
            "llm_mode": args.llm_mode,
            "telemetry": result["telemetry"],
            "cost_budget": result["cost_budget"],
        },
        args,
    )


def _cmd_barks(args: argparse.Namespace) -> int:
    with _project(args) as project:
        speakers = [item.strip() for item in args.speakers.split(",") if item.strip()]
        if not speakers:
            raise ValueError(
                "--speakers 不能为空：请提供至少一个 speaker 实体 ID（逗号分隔）。"
                "例如: --speakers npc_alice,npc_bob"
            )
        unknown = [speaker for speaker in speakers if speaker not in project.bundle.entities]
        if unknown:
            raise ValueError(f"unknown speaker entities: {', '.join(unknown)}")
        allowed = set(speakers)
        if args.allowed_entities:
            allowed.update(
                item.strip() for item in args.allowed_entities.split(",") if item.strip()
            )
        gateway, telemetry = _llm_gateway(
            args, task="barks_batch", offline_provider=OfflineBarksProvider()
        )
        service = BarkBatchService(
            gateway=gateway,
            bundle=project.bundle,
            review_queue=ReviewQueue(project.sqlite_store),
        )
        result = service.generate(
            speaker_ids=speakers,
            topic=args.topic,
            variants_per_speaker=args.variants,
            max_chars=args.max_chars,
            allowed_entity_ids=allowed,
        )
        telemetry_summary = telemetry.summary()
        return _emit(
            {
                "accepted": [
                    {"speaker_id": variant.speaker_id, "text": variant.text}
                    for variant in result.accepted
                ],
                "rejected": [
                    {
                        "speaker_id": rejected.speaker_id,
                        "text": rejected.text,
                        "issues": [issue.model_dump(mode="json") for issue in rejected.issues],
                    }
                    for rejected in result.rejected
                ],
                "review_item_ids": [item.id for item in result.review_items],
                "llm_mode": args.llm_mode,
                "telemetry": telemetry_summary,
                "cost_budget": summarize_workflow(
                    [llm_step("barks_batch", telemetry_summary)], budget_usd=args.max_cost_usd
                ).budget.model_dump(mode="json"),
            },
            args,
        )


def _cmd_review(args: argparse.Namespace) -> int:
    with _project(args) as project:
        if args.accept and args.reject:
            raise ValueError("--accept and --reject are mutually exclusive")
        if args.accept or args.reject:
            if not args.operator:
                raise ValueError("--operator is required with --accept/--reject")
            outcome = decide_review_item(
                project,
                args.accept or args.reject,
                decision="accepted" if args.accept else "rejected",
                operator=args.operator,
            )
            payload: dict[str, Any] = {
                "decision": outcome.decision,
                "item": outcome.item.model_dump(mode="json"),
                "cost_budget": _deterministic_cost_budget("review_decide"),
            }
            if outcome.decision == "accepted":
                payload["written_ref"] = outcome.written_ref
                payload["post_audit_open_errors"] = outcome.post_audit_open_errors
            return _emit(payload, args)
        pending = ReviewQueue(project.sqlite_store).list_pending()
        return _emit(
            {
                "count": len(pending),
                "items": [item.model_dump(mode="json") for item in pending],
                "cost_budget": _deterministic_cost_budget("review_list"),
            },
            args,
        )


class _ProjectHandle:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.project: ProjectContext | None = None

    def __enter__(self) -> ProjectContext:
        content_root = Path(self.args.content_root)
        if self.args.command != "ingest" and not content_root.exists():
            raise FileNotFoundError(f"content root does not exist: {content_root}")
        sqlite_path = self.args.sqlite_path or _default_sqlite_path(content_root)
        if sqlite_path != ":memory:":
            Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        self.project = ProjectContext.open(content_root, sqlite_path=sqlite_path)
        return self.project

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.project is not None:
            self.project.close()


def _project(args: argparse.Namespace) -> _ProjectHandle:
    return _ProjectHandle(args)


def _default_sqlite_path(content_root: Path) -> Path:
    return content_root / ".owcopilot" / "runtime.sqlite"


def _load_baseline(path: Path) -> AuditBaseline:
    return AuditBaseline.model_validate_json(path.read_text(encoding="utf-8"))


def _load_mapping_doc(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"field mapping 文件不是合法 JSON（{path.name}）："
            f"{exc.msg} 在第 {exc.lineno} 行第 {exc.colno} 列"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError("field mapping 文件必须是 JSON 对象（{...}）")
    return data


def _mapping_for_path(mapping_doc: dict[str, Any], path: Path) -> FieldMapping | None:
    if "columns" in mapping_doc:
        return FieldMapping(
            columns=dict(mapping_doc.get("columns") or {}),
            default_kind=mapping_doc.get("type") or mapping_doc.get("default_kind"),
        )
    normalized_path = str(path).replace("\\", "/")
    best: dict[str, Any] | None = None
    best_len = -1
    for key, value in mapping_doc.items():
        if key.startswith("_") or not isinstance(value, dict):
            continue
        normalized_key = key.replace("\\", "/")
        if normalized_path.endswith(normalized_key) and len(normalized_key) > best_len:
            best = value
            best_len = len(normalized_key)
    if best is None:
        return None
    return FieldMapping(
        columns=dict(best.get("columns") or {}),
        default_kind=best.get("type") or best.get("default_kind"),
    )


def _parse_with_mapping(mapping_doc: dict[str, Any], path: Path) -> list[Any]:
    raw_objects = parse_paths([path])
    mapping = _mapping_for_path(mapping_doc, path) or _mapping_for_raw_objects(
        mapping_doc, raw_objects
    )
    return apply_field_mapping(raw_objects, mapping)


def _mapping_for_raw_objects(
    mapping_doc: dict[str, Any], raw_objects: list[Any]
) -> FieldMapping | None:
    if not raw_objects:
        return None
    source_columns = set(raw_objects[0].data)
    best: dict[str, Any] | None = None
    best_score = 0
    for key, value in mapping_doc.items():
        if key.startswith("_") or not isinstance(value, dict):
            continue
        columns = set((value.get("columns") or {}).keys())
        if not columns:
            continue
        score = len(columns & source_columns)
        if score > best_score:
            best = value
            best_score = score
    if best is None or best_score == 0:
        return None
    return FieldMapping(
        columns=dict(best.get("columns") or {}),
        default_kind=best.get("type") or best.get("default_kind"),
    )


def _deterministic_cost_budget(step_name: str) -> dict[str, Any]:
    return summarize_workflow([deterministic_step(step_name)]).budget.model_dump(mode="json")


def _emit(payload: dict[str, Any], args: argparse.Namespace) -> int:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
