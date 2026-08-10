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
from ..config import DEFAULT_CHEAP_MODEL, DEFAULT_LLM_MODE
from ..content.hash import content_hash
from ..content.ingest import IngestResult, ingest_raw_objects, parse_paths
from ..content.mapping import FieldMapping, apply_field_mapping
from ..evaluation import run_acceptance_evaluation, run_golden_evaluation
from ..evaluation.agent_task_bench import (
    DEFAULT_BUDGET_USD as DEFAULT_AGENT_TASK_BUDGET_USD,
)
from ..evaluation.agent_task_bench import (
    DEFAULT_MAX_STEPS as DEFAULT_AGENT_TASK_MAX_STEPS,
)
from ..evaluation.agent_task_bench import (
    DEFAULT_RUNG as AGENT_TASK_DEFAULT_RUNG,
)
from ..evaluation.agent_task_bench import (
    DEFAULT_TRIALS as DEFAULT_AGENT_TASK_TRIALS,
)
from ..evaluation.agent_task_bench import (
    PROMPT_VARIANTS as AGENT_TASK_PROMPT_VARIANTS,
)
from ..evaluation.agent_task_bench import (
    RUNGS as AGENT_TASK_RUNGS,
)
from ..evaluation.report_actionability import (
    DEFAULT_ACTIONABILITY_BUDGET_USD,
    DEFAULT_FIXER_MAX_STEPS,
)
from ..exporters import EngineTarget
from ..impact import Change, ChangeSet, ChangeType, ImpactAnalyzer, ImpactLevel
from ..inspiration.models import REFERENCE_USES, Provenance
from ..llm.cache import NoOpCache
from ..llm.gateway import LLMGateway, require_offline_llm_allowed
from ..llm.resilience import build_real_provider
from ..llm.router import StaticRouter
from ..llm.telemetry import TelemetryCollector
from ..pipeline.audit import run_full_audit
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
from ..qa.offline import OfflineCommunityReportProvider
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

    flag_xref = subparsers.add_parser(
        "flag-xref",
        help=(
            "Cross-reference world state flags: who declares / writes / reads each one, plus the "
            "decidable problems (conflicting declarations, gates nothing can open, dead writes)."
        ),
    )
    _add_project_args(flag_xref)
    flag_xref.add_argument(
        "--flag",
        help="Narrow to one variable id; omitted, every flag in the world is listed.",
    )
    flag_xref.set_defaults(handler=_cmd_flag_xref)

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
    agent.add_argument(
        "--goal",
        required=False,
        default=None,
        help=(
            "What the agent should achieve / investigate. Required for a fresh run; ignored with "
            "--resume (the goal is recovered from the checkpoint being continued)."
        ),
    )
    agent.add_argument(
        "--max-steps",
        type=int,
        default=None,
        help="Planning-turn budget (fresh default: 6; resume restores its frozen value).",
    )
    agent.add_argument(
        "--max-model-calls",
        type=int,
        default=None,
        help=(
            "Hard total across planning, compression, finish and reflection calls; fresh runs "
            "derive a bounded compatibility default and resume restores its frozen value."
        ),
    )
    agent.add_argument(
        "--max-tool-calls",
        type=int,
        default=None,
        help="Independent tool-call budget; defaults to --max-steps for compatibility.",
    )
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
        "--tool-transport",
        choices=["native", "mcp"],
        default=None,
        help=(
            "Execution transport for audit_project: native runs it in-process; mcp crosses the "
            "checked local stdio MCP boundary. Fresh runs default to native; on resume, omitting "
            "this flag restores the frozen route. MCP failures never fall back silently."
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
            "transcript collected so far, instead of discarding it; stop_reason becomes "
            "'max_steps_generate'."
        ),
    )
    agent.add_argument(
        "--thread-id",
        default=None,
        help=(
            "Pin this run to a known checkpoint thread id so it can be resumed later. A "
            "checkpointer is ALWAYS attached (each run auto-mints a unique thread id, surfaced as "
            "`thread_id` in the output); pass this to choose the id yourself. Reusing an existing "
            "id without --resume is refused (add --resume to continue it, or use a fresh id)."
        ),
    )
    agent.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Continue the unfinished run stored under --thread-id (invoke(None)) from its last "
            "completed step — already-run tools do NOT re-run. Requires --thread-id. Honest limit: "
            "middleware instance state (truncated-observation tails, compression counters) is NOT "
            "checkpointed, so it restarts empty after a resume."
        ),
    )
    agent.add_argument(
        "--checkpoint-path",
        default=None,
        help=(
            "Where the resume checkpoints live. Defaults to "
            "<content-root>/.owcopilot/checkpoints.sqlite (independent from runtime.sqlite)."
        ),
    )
    agent.add_argument(
        "--hitl",
        action="store_true",
        help=(
            "T6 HITL form a (inline interrupt): PAUSE before every sensitive tool call (the "
            "PROPOSES_PATCH skills — the agent's only write-adjacent action) and wait for a human "
            "decision. On a pause the run stops with status=interrupted and prints the pending "
            "gate + the thread id; resume with --thread-id <ID> --resume --decision approve|reject."
        ),
    )
    agent.add_argument(
        "--decision",
        choices=["approve", "reject"],
        default=None,
        help=(
            "The human decision for a --resume of a run paused at a HITL gate (form a): 'approve' "
            "runs the proposed tool call, 'reject' skips it and tells the model not to retry. "
            "The one decision applies to every action in the pending gate."
        ),
    )
    agent.add_argument(
        "--decision-message",
        default=None,
        help="Optional reason attached to a --decision reject (fed back to the model).",
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
    multi_agent.add_argument(
        "--max-tool-calls",
        type=int,
        default=None,
        help="Independent per-worker/verifier tool-call budget (default: each model budget).",
    )
    multi_agent.add_argument(
        "--max-model-calls",
        type=int,
        default=None,
        help=(
            "Hard model-call budget shared by decomposition, all workers, verifier and "
            "Reflexion (default: 128)."
        ),
    )
    multi_agent.add_argument(
        "--max-total-tool-calls",
        type=int,
        default=None,
        help="Hard tool-call budget shared by every worker and verifier (default: 256).",
    )
    multi_agent.add_argument(
        "--checkpoint-path",
        default=None,
        help=(
            "Where the supervisor-graph checkpoints live (scheme Y — in-process resume / T6 "
            "substrate; cross-process resume is not supported for multi-agent). Defaults to "
            "<content-root>/.owcopilot/checkpoints.sqlite."
        ),
    )
    _add_llm_args(multi_agent)
    multi_agent.set_defaults(handler=_cmd_multi_agent)

    mcp_serve = subparsers.add_parser(
        "mcp-serve",
        help=(
            "Serve the owcopilot tools over the MCP protocol (FastMCP). Opens ONE shared "
            "project for the server's lifetime (C-5 lifespan) and exposes the tool subset plus "
            "canon/issues resources and a consistency-review prompt. Blocks until the client "
            "exits. Requires the optional MCP SDK: `pip install -e .[mcp]`."
        ),
    )
    mcp_serve.add_argument("--content-root", required=True, help="Project content root to serve.")
    mcp_serve.add_argument(
        "--sqlite-path",
        help="Runtime SQLite path. Defaults to <content-root>/.owcopilot/runtime.sqlite.",
    )
    mcp_serve.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="MCP transport (default: stdio — what an MCP host launches as a subprocess).",
    )
    mcp_serve.add_argument("--name", default="owcopilot", help="Server name advertised to clients.")
    mcp_serve.set_defaults(handler=_cmd_mcp_serve)

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
    draft.add_argument(
        "--use-references",
        action="store_true",
        help=(
            "Also ground the draft on the inspiration library (only sources whose allowed_uses "
            "include 'drafting'). Off by default; an empty library still drafts normally. The "
            "reply then carries inspiration_refs plus a recomputed reference-leakage report."
        ),
    )
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
        "--provenance",
        default=Provenance.OWN_IP.value,
        choices=[item.value for item in Provenance],
        help=(
            "Rights status of the manuscript. This door writes canon drafts, so "
            "third_party_reference is refused here — put that material in the inspiration "
            "library instead."
        ),
    )
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

    reference = subparsers.add_parser(
        "reference",
        help=(
            "Manage the inspiration reference library (grounding material, never canon): "
            "add / list / search reference sources."
        ),
    )
    _add_project_args(reference)
    reference.add_argument(
        "action",
        choices=["add", "list", "search"],
        help=(
            "add: ingest reference text. list: list stored sources. "
            "search: retrieval lookup by query."
        ),
    )
    reference.add_argument("--title", help="Reference title (action=add).")
    reference.add_argument("--text", help="Reference text content (action=add).")
    reference.add_argument(
        "--source-type", default="uploaded_file", help="Source type label (action=add)."
    )
    reference.add_argument(
        "--original-filename",
        help="Original filename, if the text came from a file (action=add).",
    )
    reference.add_argument(
        "--allowed-use",
        action="append",
        dest="allowed_uses",
        choices=sorted(REFERENCE_USES),
        help=(
            "Repeatable; which pipelines may retrieve this source (action=add). "
            "Omit to default to 'inspiration' only."
        ),
    )
    reference.add_argument(
        "--provenance",
        default=Provenance.THIRD_PARTY_REFERENCE.value,
        choices=[item.value for item in Provenance],
        help=(
            "Rights status of the material (action=add). Unlike `extract`, this library takes "
            "all three provenances — it is physically separate from canon."
        ),
    )
    reference.add_argument("--query", help="Search query text (action=search).")
    reference.add_argument(
        "--budget-tokens",
        type=int,
        default=1000,
        help="Search token budget (action=search, default 1000).",
    )
    reference.set_defaults(handler=_cmd_reference)

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
    barks.add_argument(
        "--use-references",
        action="store_true",
        help=(
            "Also ground the barks on the inspiration library (only sources whose allowed_uses "
            "include 'drafting'). Off by default; an empty library still drafts normally. The "
            "reply then carries inspiration_refs plus a recomputed reference-leakage report."
        ),
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

    style_sweep = subparsers.add_parser(
        "style-sweep",
        help=(
            "Recall a versioned StyleRubric checklist across the whole world (L1 word/regex -> "
            "L2 embedding concept -> L3 LLM judge cascade). A $0 diagnostic report for a human, "
            "same shape as flag-xref/quest-explore -- never a pass/fail gate."
        ),
    )
    _add_project_args(style_sweep)
    style_sweep.add_argument(
        "--rubric-id",
        required=True,
        help="StyleRubric id from the content bundle (bundle.style_rubrics).",
    )
    style_sweep.add_argument(
        "--use-llm",
        action="store_true",
        help="Also run the L3 judge over the L2 candidate pool (needs --llm-mode real for a "
        "real model).",
    )
    style_sweep.add_argument("--max-judge", type=_nonneg_int, default=400)
    style_sweep.add_argument("--semantic-threshold", type=_nonneg_float, default=0.5)
    _add_llm_args(style_sweep)
    style_sweep.set_defaults(handler=_cmd_style_sweep)

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
    export.add_argument(
        "--row-struct-map",
        help=(
            "UNREAL target only: path to a JSON file that renames DataTable columns to the "
            "target project's own USTRUCT field names, so exported rows land in an existing "
            "schema instead of the generated OWCopilotRows.h. Shape: "
            '{"<table>": {"<CanonicalField>": "<ExportName>"}} '
            '(e.g. {"quests": {"DisplayName": "sDisplayName"}}). Reserved keys Name/Type are '
            "never remapped. Omit to keep the default field names (export unchanged)."
        ),
    )
    export.add_argument(
        "--planning-workbook",
        action="store_true",
        help=(
            "Also write planning_workbook.xlsx: 任务表/台词表/实体表/术语表 as sheets in one "
            "workbook, columns matching the generic CSV/XLSX import's own field names. "
            "Engine-agnostic (written regardless of --target-engine). Off by default."
        ),
    )
    export.add_argument(
        "--planning-notes",
        action="store_true",
        help=(
            "Also write planning_notes.docx: 世界概览/任务卡/人物设定卡, plus a 审查报告 section "
            "listing every open audit finding. Engine-agnostic. Off by default."
        ),
    )
    export.set_defaults(handler=_cmd_export)

    prose_check = subparsers.add_parser(
        "prose-check",
        help=(
            "Check a pasted chapter against the archive: resolved mentions, forbidden-term "
            "spellings, unknown proper-noun candidates. Deterministic, no LLM."
        ),
    )
    _add_project_args(prose_check)
    prose_check.add_argument(
        "--input", required=True, help="Path to the prose/chapter text file to check (UTF-8)."
    )
    prose_check.set_defaults(handler=_cmd_prose_check)

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
    recognize.add_argument(
        "--enable-llm",
        action="store_true",
        help=(
            "Also run the §8-guarded LLM relation pass (closed-world + evidence-grounded, "
            "default off) on top of the deterministic recognizers. Needs --llm-mode/--llm-model "
            "for the model to use."
        ),
    )
    _add_llm_args(recognize)
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

    eval_agent_tasks = subparsers.add_parser(
        "eval-agent-tasks",
        help=(
            "Task-level agent benchmark: seeded defect worlds, the deterministic audit's open "
            "findings as ground truth, k real-model trials per task, machine-scored recall / "
            "precision / pass^k. Costs money with --llm-mode real."
        ),
    )
    eval_agent_tasks.add_argument("--workspace", required=True)
    eval_agent_tasks.add_argument(
        "--trials",
        type=int,
        default=DEFAULT_AGENT_TASK_TRIALS,
        help="Trials per task (k in pass^k). Must be >= 1.",
    )
    eval_agent_tasks.add_argument(
        "--max-steps", type=int, default=DEFAULT_AGENT_TASK_MAX_STEPS, help="Agent step budget."
    )
    eval_agent_tasks.add_argument(
        "--max-replans",
        type=int,
        default=1,
        help="Bounded plan-and-execute replans per trial (0 disables; hard cap 2).",
    )
    eval_agent_tasks.add_argument(
        "--budget-usd",
        type=_nonneg_float,
        default=DEFAULT_AGENT_TASK_BUDGET_USD,
        help="Hard spend stop: the run aborts and reports budget_stopped when exceeded.",
    )
    eval_agent_tasks.add_argument(
        "--task",
        action="append",
        dest="task_ids",
        help="Run only these task ids (repeatable, e.g. --task T01 --task T02).",
    )
    eval_agent_tasks.add_argument(
        "--rung",
        choices=list(AGENT_TASK_RUNGS),
        default=AGENT_TASK_DEFAULT_RUNG,
        help=(
            "Ablation rung; each adds ONE capability to the one below it. L0 = no tools at all. "
            "L1 = + retrieval/graph tools (unaided discovery through the product's read surface). "
            "L2 = + the winning prompt variant. L3 = + dialogue trees in the read surface. "
            "L4 = + the deterministic checker (full production surface). Corpus, gold set, scorer "
            "and thresholds are identical on every rung, so a rung-to-rung gap is that one "
            "variable's measured contribution."
        ),
    )
    eval_agent_tasks.add_argument(
        "--prompt-variant",
        choices=list(AGENT_TASK_PROMPT_VARIANTS),
        default=None,
        help=(
            "Override the rung's prompt variant. Used for the prompt A/B (which sweeps variants at "
            "a fixed rung) and for protocol-isolation runs; recorded in the report, so an "
            "overridden run is never mistaken for the ladder's own rung."
        ),
    )
    eval_agent_tasks.add_argument(
        "--memory-ab",
        action="store_true",
        help=(
            "Run the episodic-memory A/B instead of a single ladder run: the same corpus twice, "
            "with the recalled memory readable and not (the product's own --no-recall switch). "
            "Both arms write memory; only one reads it, so the pair differs by one variable. "
            "--budget-usd then bounds the WHOLE A/B (half per arm). Reported as pass@1: with "
            "memory on a task's trials share state, so pass^k would inflate k."
        ),
    )
    eval_agent_tasks.add_argument(
        "--output", help="Write the JSON result to this file as well as stdout."
    )
    _add_llm_args(eval_agent_tasks)
    eval_agent_tasks.set_defaults(handler=_cmd_eval_agent_tasks)

    eval_report = subparsers.add_parser(
        "eval-report-actionability",
        help=(
            "Measure whether the audit report is actionable: every open finding is handed, one at "
            "a time, to a fresh fixer that gets ONLY that finding's report text plus read/write "
            "access to the content files (no audit tool, no source). Judged by re-running the "
            "audit. Costs money with --llm-mode real."
        ),
    )
    eval_report.add_argument(
        "--workspace", required=True, help="Scratch directory for the per-finding world copies."
    )
    eval_report.add_argument(
        "--label",
        default="未标注",
        help="Which report generation this run measures, e.g. 改造前基线 / 改造后.",
    )
    eval_report.add_argument(
        "--max-steps",
        type=int,
        default=DEFAULT_FIXER_MAX_STEPS,
        help="Model-call budget for one fixer.",
    )
    eval_report.add_argument(
        "--budget-usd",
        type=_nonneg_float,
        default=DEFAULT_ACTIONABILITY_BUDGET_USD,
        help="Hard spend stop: the run aborts and reports budget_stopped when exceeded.",
    )
    eval_report.add_argument(
        "--world",
        action="append",
        dest="world_ids",
        help="Measure only these seeded worlds (repeatable, e.g. --world T01 --world T03).",
    )
    eval_report.add_argument(
        "--no-file-listing",
        action="store_true",
        help=(
            "Stricter variant: take the file-tree listing away, so finding the right file depends "
            "on the report alone. Use when the headline rate has saturated."
        ),
    )
    eval_report.add_argument(
        "--replay-report-text",
        help=(
            "Re-measure an OLDER report generation: read each finding's report text from a stored "
            "run's JSON instead of rendering today's. Use to re-judge a past run under a changed "
            "judging rule (e.g. the content-preservation clause) without restoring old code."
        ),
    )
    eval_report.add_argument(
        "--report-out",
        help="Write the per-finding table + JSON record to this path stem (.md / .json).",
    )
    eval_report.add_argument(
        "--output", help="Write the JSON result to this file as well as stdout."
    )
    _add_llm_args(eval_report)
    eval_report.set_defaults(handler=_cmd_eval_report_actionability)

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
        default=os.getenv("OWCOPILOT_LLM_MODE", DEFAULT_LLM_MODE),
        help=(
            "real: a configured OpenAI-compatible provider (the product path). offline: the "
            "deterministic doubles — a test/CI dry-run only, gated behind "
            "OWCOPILOT_ALLOW_OFFLINE_LLM so it is never a shipped product mode."
        ),
    )
    parser.add_argument(
        "--llm-model",
        default=os.getenv("OWCOPILOT_CHEAP_MODEL", DEFAULT_CHEAP_MODEL),
        help="Model id for --llm-mode real.",
    )
    parser.add_argument(
        "--max-cost-usd",
        type=_nonneg_float,
        help=(
            "Agent runs: once completed-call telemetry reaches this value, block the next model "
            "dispatch; one in-flight call may overshoot and illustrative prices remain estimates. "
            "Other LLM workflows retain their report-only cost_budget semantics."
        ),
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


def _agent_chat_model(args: argparse.Namespace) -> Any:
    """Resolve ``--llm-mode``/``--llm-model`` into the agent's LangChain chat model.

    The model assembly itself lives in
    :func:`~owcopilot.app.actions.agent.build_agent_chat_model` (shared with the REST agent job);
    this only reads the two flags off the parsed namespace, so both agent front-ends construct the
    model exactly one way.
    """
    from ..app.actions import build_agent_chat_model

    return build_agent_chat_model(
        llm_mode=getattr(args, "llm_mode", "offline"),
        llm_model=getattr(args, "llm_model", DEFAULT_CHEAP_MODEL),
    )


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

        def ingest_mapped_objects() -> IngestResult:
            return ingest_raw_objects(
                [raw for _path, raws in raw_by_input for raw in raws],
                store=project.content_store,
                dry_run=not args.write,
                write_non_conflicting=args.skip_conflicts,
            )

        if args.write:
            from ..storage.world_lock import world_write_lock

            with world_write_lock(project.content_store.root):
                result = ingest_mapped_objects()
                if not result.has_errors or args.skip_conflicts:
                    project.reload()
        else:
            result = ingest_mapped_objects()
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
        # N2.4 / B7: the CLI is the one readiness caller that already has the project's reference
        # store in hand, so it is the natural (and lowest-friction) place to turn on the opt-in
        # dangling-inspiration-ref check — see assess_readiness()'s docstring for why this landed
        # in readiness rather than a new audit rule. REST/analytics/harness call assess_readiness
        # without this kwarg and keep their pre-existing behaviour untouched.
        known_chunk_ids = frozenset(chunk.id for chunk in project.reference_store.load_chunks())
        report = assess_readiness(project.bundle, known_reference_chunk_ids=known_chunk_ids)
        payload = report.model_dump(mode="json")
        items = payload["items"]
        if args.kind:
            items = [it for it in items if it["kind"] == args.kind]
        if args.only_incomplete:
            items = [it for it in items if not it["ready"]]
        payload["items"] = items
        payload["cost_budget"] = _deterministic_cost_budget("readiness")
        return _emit(payload, args)


def _cmd_flag_xref(args: argparse.Namespace) -> int:
    from ..logic import build_flag_xref

    with _project(args) as project:
        payload = build_flag_xref(project.bundle).report(args.flag)
        payload["cost_budget"] = _deterministic_cost_budget("flag_xref")
        return _emit(payload, args)


def _read_state_file(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    state = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise ValueError(f"--initial-state 应是 JSON 对象（变量名→值）：{path}")
    return state


def _split_choices(raw: str | None) -> list[str]:
    return [c.strip() for c in (raw or "").split(",") if c.strip()]


def _cmd_quest_simulate(args: argparse.Namespace) -> int:
    from ..app.actions import simulate_quest_action

    initial_state = _read_state_file(args.initial_state)
    result = simulate_quest_action(
        args.content_root,
        quest_id=args.quest_id,
        choices=_split_choices(args.choices) or None,
        initial_state=initial_state,
        dialogue_tree_id=args.dialogue_tree,
        dialogue_choices=_split_choices(args.dialogue_choices) or None,
        sqlite_path=args.sqlite_path,
    )
    payload = {**result["run"], "cost_budget": result["cost_budget"]}
    if "dialogue_walk" in result:
        payload["dialogue_walk"] = result["dialogue_walk"]
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


def _cmd_mcp_serve(args: argparse.Namespace) -> int:
    """Run the MCP server (blocks). The shared-project lifespan and concurrency reasoning live in
    ``mcp_server/serve.py``; this handler only validates the root and hands off to it."""
    from ..mcp_server import MCPTransportUnavailable, run_server

    content_root = Path(args.content_root)
    if not content_root.exists():
        raise FileNotFoundError(f"content root does not exist: {content_root}")
    try:
        run_server(
            content_root,
            sqlite_path=args.sqlite_path,
            transport=args.transport,
            name=args.name,
        )
    except MCPTransportUnavailable as exc:
        # Guided error (not a raw ImportError): the transport is an optional extra.
        raise RuntimeError(str(exc)) from exc
    return 0


def _cmd_agent(args: argparse.Namespace) -> int:
    """`owcopilot agent` — one bounded ReAct diagnosis over a project.

    The assembly (checkpoint store, thread policy, scoped registry, recall, HITL gate, run
    persistence) lives in :func:`~owcopilot.app.actions.run_agent_diagnose_action`, shared verbatim
    with the ``agent_diagnose`` async job the REST surface exposes — there is exactly one agent
    execution path. What stays here is CLI-shaped: flag-named guided errors, the unknown-skill
    warning on stderr, and the copy-pasteable resume line printed after a HITL pause.
    """
    from ..app.actions import run_agent_diagnose_action

    content_root = Path(args.content_root)
    if not content_root.exists():
        raise FileNotFoundError(f"content root does not exist: {content_root}")
    # A fresh run needs a goal; a resume recovers it from the checkpoint, so --goal is optional.
    resume = bool(args.resume)
    if not resume and not (args.goal or "").strip():
        raise ValueError(
            "--goal 不能为空字符串，请描述 agent 需要完成的目标。"
            "例如: --goal 'check quest consistency'"
        )
    # --decision is the human answer for a paused HITL gate (T6 form a) — only meaningful when
    # continuing that gate, so require --resume alongside it (guided error, not a silent no-op).
    if getattr(args, "decision", None) and not resume:
        raise ValueError(
            "--decision 只用于恢复一次暂停在 HITL 审批门的 run（形态 a）；"
            "请配合 --thread-id <ID> --resume 使用，或去掉 --decision。"
        )
    payload = run_agent_diagnose_action(
        content_root,
        goal=args.goal or "",
        max_steps=args.max_steps,
        max_model_calls=args.max_model_calls,
        max_tool_calls=args.max_tool_calls,
        skills=list(args.skills) if args.skills is not None else None,
        recall=not args.no_recall,
        hitl=bool(args.hitl),
        early_stopping=args.early_stopping,
        thread_id=args.thread_id,
        resume=resume,
        decision=args.decision,
        decision_message=args.decision_message,
        checkpoint_path=args.checkpoint_path,
        llm_mode=args.llm_mode,
        llm_model=args.llm_model,
        sqlite_path=args.sqlite_path,
        max_cost_usd=args.max_cost_usd,
        tool_transport=args.tool_transport,
        chat_model=_agent_chat_model(args),
    )
    # Cross-validation of --skills against the registry: warn (not fail) on unknown names so the
    # user learns the typo/stale skill name was excluded. Popped off the payload so stdout carries
    # exactly the run result.
    unknown_skills = payload.pop("unknown_skills", None)
    known_skills = payload.pop("known_skills", None)
    if unknown_skills:
        print(
            json.dumps(
                {
                    "warning": "unknown skill(s) in --skills allowlist (will be ignored)",
                    "unknown_skills": unknown_skills,
                    "known_skills": known_skills,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
    # T6 HITL form a: when the run paused at a human gate, tell the operator exactly how to
    # continue (the pending gate itself is already on the payload as interrupt_request).
    if payload["status"] == "interrupted":
        payload["next_step"] = (
            f"resume with: owcopilot agent --content-root {args.content_root} "
            f"--thread-id {payload['thread_id']} --resume --decision approve|reject"
        )
    return _emit(payload, args)


def _cmd_multi_agent(args: argparse.Namespace) -> int:
    """Expose the shared multi-agent action through the CLI adapter."""
    from ..app.actions import run_multi_agent_action

    payload = run_multi_agent_action(
        args.content_root,
        goal=args.goal,
        max_tool_calls=args.max_tool_calls,
        max_total_tool_calls=args.max_total_tool_calls,
        max_model_calls=args.max_model_calls,
        checkpoint_path=args.checkpoint_path,
        llm_mode=args.llm_mode,
        llm_model=args.llm_model,
        sqlite_path=args.sqlite_path,
        max_cost_usd=args.max_cost_usd,
        chat_model=_agent_chat_model(args),
    )
    return _emit(payload, args)


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
    from ..app.actions import run_ask_action

    payload = run_ask_action(
        args.content_root,
        query=args.query,
        sqlite_path=args.sqlite_path,
        budget_tokens=args.budget_tokens,
        llm_mode=args.llm_mode,
        llm_model=args.llm_model,
        max_cost_usd=args.max_cost_usd,
        agentic=args.agentic,
        faithfulness=args.faithfulness,
    )
    return _emit(payload, args)


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
    from ..app.actions import run_project_export_action

    row_struct_map = (
        _load_row_struct_map(Path(args.row_struct_map)) if args.row_struct_map else None
    )
    return _emit(
        run_project_export_action(
            args.content_root,
            output_dir=args.output_dir,
            target_engine=args.target_engine,
            row_struct_map=row_struct_map,
            unity_localization=args.unity_localization,
            planning_workbook=args.planning_workbook,
            planning_notes=args.planning_notes,
            sqlite_path=args.sqlite_path,
        ),
        args,
    )


def _cmd_prose_check(args: argparse.Namespace) -> int:
    # Reuses the shared app action so CLI and REST (POST /projects/{p}/prose:check) behave
    # identically; the action already existed (app/actions/export.py) but had zero callers.
    from ..app.actions import run_prose_check_action

    text = Path(args.input).read_text(encoding="utf-8")
    result = run_prose_check_action(
        args.content_root,
        text=text,
        sqlite_path=getattr(args, "sqlite_path", None),
    )
    return _emit(result, args)


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
        enable_llm=args.enable_llm,
        llm_mode=args.llm_mode,
        llm_model=args.llm_model,
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


def _cmd_eval_agent_tasks(args: argparse.Namespace) -> int:
    """Run the task-level agent benchmark (``evaluation/agent_task_bench.py``).

    Unlike ``eval-agent`` (a deterministic $0 scorecard) this SPENDS MONEY in ``--llm-mode real``:
    it drives the real agent over every seeded world ``--trials`` times. ``--budget-usd`` is a hard
    stop checked before each trial. Progress is written to stderr so a long run is observable
    without polluting the JSON on stdout.
    """
    from ..evaluation.agent_task_bench import TrialRecord, run_agent_task_bench, run_memory_ab

    if args.trials < 1:
        raise ValueError("--trials 必须 >= 1（pass^k 的 k 至少为 1）。")

    def progress(record: TrialRecord) -> None:
        print(
            f"[{args.rung} {record.task_id} trial {record.trial}] "
            f"recall={record.score.recall_pair:.2f} "
            f"precision={record.score.precision_pair} "
            f"contract={'Y' if record.score.contract_followed else 'N'} "
            f"steps={record.step_count} cost=${record.cost_usd:.5f} "
            f"recalled={record.recalled_blocks}"
            + (f" ERROR={record.error}" if record.error else ""),
            file=sys.stderr,
            flush=True,
        )

    common = {
        "chat_model_factory": lambda: _agent_chat_model(args),
        "llm_mode": args.llm_mode,
        "model": args.llm_model if args.llm_mode == "real" else "offline-double",
        "trials": args.trials,
        "max_steps": args.max_steps,
        "max_replans": args.max_replans,
        "budget_usd": args.budget_usd,
        "task_ids": args.task_ids,
        "rung": args.rung,
        "prompt_variant": args.prompt_variant,
        "on_trial": progress,
    }
    if args.memory_ab:
        ab = run_memory_ab(args.workspace, **common)
        _emit(ab.model_dump(mode="json"), args)
        return 1 if ab.off.budget_stopped or ab.on.budget_stopped else 0

    report = run_agent_task_bench(args.workspace, **common)
    _emit(report.model_dump(mode="json"), args)
    # A budget abort is a real outcome, not a crash: report it and exit non-zero so a script
    # notices the run was cut short rather than reading a partial table as complete.
    return 1 if report.budget_stopped else 0


def _cmd_eval_report_actionability(args: argparse.Namespace) -> int:
    """Measure the audit report's actionability (``evaluation/report_actionability.py``).

    SPENDS MONEY in ``--llm-mode real``: one fresh fixer per open finding. ``--budget-usd`` is a
    hard stop checked before each finding. Progress goes to stderr so a long run is observable
    without polluting the JSON on stdout.
    """
    from ..evaluation.report_actionability import (
        ActionabilityTrial,
        load_report_texts,
        run_report_actionability,
        write_actionability_artifacts,
    )

    def progress(trial: ActionabilityTrial) -> None:
        # DAMAGED = 指纹判据算通过、内容保全没过 —— 也就是"删出来的通过",单独标出来。
        outcome = "FIXED  " if trial.fixed else ("DAMAGED" if trial.fingerprint_ok else "MISS   ")
        print(
            f"[{trial.world_id} {trial.rule_code}] "
            f"{outcome} "
            f"files={len(trial.files_written)} calls={trial.llm_calls} "
            f"cost=${trial.cost_usd:.5f} — {trial.verdict}",
            file=sys.stderr,
            flush=True,
        )

    report = run_report_actionability(
        args.workspace,
        chat_model_factory=lambda: _agent_chat_model(args),
        llm_mode=args.llm_mode,
        model=args.llm_model if args.llm_mode == "real" else "offline-double",
        label=args.label,
        max_steps=args.max_steps,
        budget_usd=args.budget_usd,
        world_ids=set(args.world_ids) if args.world_ids else None,
        file_listing=not args.no_file_listing,
        report_texts=(
            load_report_texts(args.replay_report_text) if args.replay_report_text else None
        ),
        on_trial=progress,
    )
    if args.report_out:
        write_actionability_artifacts(report, args.report_out)
    _emit(report.model_dump(mode="json"), args)
    return 1 if report.budget_stopped else 0


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
        provenance=args.provenance,
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
        "provenance": result["provenance"],
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
            reference_context_builder=project.reference_context_builder,
        )
        result = service.draft_quest(
            args.brief, budget_tokens=args.budget_tokens, use_references=args.use_references
        )
        queue = ReviewQueue(project.sqlite_store)
        item = queue.add_quest_draft(
            result.quest.model_dump(mode="json", exclude_none=True),
            issue_refs=[issue_fingerprint(issue) for issue in result.issues],
        )
        telemetry_summary = telemetry.summary()
        # 只有开了 --use-references 才多这两个键（开了但库里没素材时是两个空列表——那是"开关生效、
        # 无可用素材"，与"没开"要分得清）；没开时输出与接线之前逐键一致。
        reference_lineage: dict[str, Any] = (
            {
                "inspiration_refs": result.inspiration_refs,
                "reference_report": [
                    row.model_dump(mode="json") for row in result.reference_report
                ],
            }
            if args.use_references
            else {}
        )
        return _emit(
            {
                "quest": result.quest.model_dump(mode="json", exclude_none=True),
                "issues": [issue.model_dump(mode="json") for issue in result.issues],
                "context_refs": result.context_refs,
                **reference_lineage,
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


def _cmd_reference(args: argparse.Namespace) -> int:
    """add/list/search over the inspiration reference library — a single `reference` subcommand
    with a positional action (git-style), reusing exactly the store/builder calls the REST
    add/list/search_references_action wrap (project.reference_store.* / .reference_context_builder,
    same as `build_context_pack` reuses `project.context_builder` for canon)."""
    with _project(args) as project:
        if args.action == "add":
            if not args.title or not args.text:
                raise ValueError(
                    "`reference add` 需要同时提供 --title 与 --text：请填写参考素材的标题和"
                    "正文内容后重试。"
                )
            result = project.reference_store.add_text(
                title=args.title,
                text=args.text,
                source_type=args.source_type,
                original_filename=args.original_filename,
                allowed_uses=args.allowed_uses,
                provenance=args.provenance,
            )
            project.reference_store.sync_index(project.sqlite_store)
            return _emit(
                {
                    "source": result.source.model_dump(mode="json"),
                    "chunks": [chunk.model_dump(mode="json") for chunk in result.chunks],
                    "indexed_count": result.indexed_count,
                    "injection_flagged_chunks": result.injection_flagged_chunks,
                    "cost_budget": _deterministic_cost_budget("reference_add"),
                },
                args,
            )
        if args.action == "list":
            sources = project.reference_store.list_sources()
            return _emit(
                {
                    "count": len(sources),
                    "sources": [source.model_dump(mode="json") for source in sources],
                    "cost_budget": _deterministic_cost_budget("reference_list"),
                },
                args,
            )
        # action == "search"
        if not args.query:
            raise ValueError(
                "`reference search` 需要提供 --query：请填写要检索的关键词或问题后重试。"
            )
        pack = project.reference_context_builder.build(args.query, budget_tokens=args.budget_tokens)
        return _emit(
            {
                "query": args.query,
                "refs": pack.refs,
                "hits": [hit.model_dump(mode="json") for hit in pack.hits],
                "cost_budget": _deterministic_cost_budget("reference_search"),
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
            reference_context_builder=project.reference_context_builder,
        )
        result = service.generate(
            speaker_ids=speakers,
            topic=args.topic,
            variants_per_speaker=args.variants,
            max_chars=args.max_chars,
            allowed_entity_ids=allowed,
            use_references=args.use_references,
        )
        telemetry_summary = telemetry.summary()
        # 只有开了 --use-references 才多这两个键（开了但库里没素材时是两个空列表——那是"开关生效、
        # 无可用素材"，与"没开"要分得清）；没开时输出与接线之前逐键一致。
        reference_lineage: dict[str, Any] = (
            {
                "inspiration_refs": result.inspiration_refs,
                "reference_report": [
                    row.model_dump(mode="json") for row in result.reference_report
                ],
            }
            if args.use_references
            else {}
        )
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
                **reference_lineage,
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


def _cmd_style_sweep(args: argparse.Namespace) -> int:
    """Product entry for :class:`~owcopilot.assist.sweep.RubricSweepService` (N8.3): a standalone
    leaf subcommand -- neither folded into ``review`` nor grafted onto the existing theme-sweep
    REST route. Decision + reasoning (checked at execution time, not assumed):

    * Not "review 家族": ``review`` is shaped around the queue's accept/reject decision
      (``--accept``/``--reject``/``--operator``) -- it is the ONE AI-content write path in the
      CLI. A rubric sweep never writes canon; it is a read-only diagnostic report over the whole
      world, the same shape as ``flag-xref``/``quest-explore`` (already-standalone subcommands
      with no ``app.actions`` wrapper). Bolting an unrelated argument surface
      (``--rubric-id``/``--use-llm``/``--max-judge``/``--semantic-threshold``) onto a command
      whose contract is "decide one queue item" would be MORE invasive than a new leaf command.
    * Not "挂进既有 sweep 入口": ``ThemeSweepService`` (the ``/projects/{p}/sweeps:run`` REST
      route) has no CLI counterpart at all to hook into, and its request/response schema is
      theme-shaped (``theme: str``, flat ``hits``/``review_suggested``) -- widening it with
      rubric-only fields (``rubric_id``, per-element L1/L2/L3 breakdown) for a shape that does
      not fit is a bigger diff than a new command, and the task explicitly asks for a CLI entry.
    """
    from ..assist.sweep import OfflineSweepJudgeProvider, RubricSweepService, render_rubric_markdown

    with _project(args) as project:
        rubric = project.bundle.style_rubrics.get(args.rubric_id)
        if rubric is None:
            available = (
                "、".join(sorted(project.bundle.style_rubrics)) or "（当前世界没有风格清单）"
            )
            raise ValueError(f"风格清单「{args.rubric_id}」不存在。当前世界可用清单：{available}")
        gateway = None
        telemetry = None
        if args.use_llm:
            # RubricSweepService's L3 judge reuses ThemeSweepService._judge_batch verbatim, which
            # routes through the gateway with task="theme_sweep" -- match that here so --llm-mode
            # real resolves the same provider mapping.
            gateway, telemetry = _llm_gateway(
                args, task="theme_sweep", offline_provider=OfflineSweepJudgeProvider()
            )
        report = RubricSweepService(
            bundle=project.bundle, gateway=gateway, embedder=project.embedder
        ).sweep(
            rubric,
            use_llm=args.use_llm,
            max_judge=args.max_judge,
            semantic_threshold=args.semantic_threshold,
        )
        payload: dict[str, Any] = {
            "rubric_id": report.rubric_id,
            "rubric_label": report.rubric_label,
            "rubric_version": report.rubric_version,
            "rubric_fingerprint": report.rubric_fingerprint,
            "abstract_tag": report.abstract_tag,
            "scanned_total": report.scanned_total,
            "scanned_by_kind": report.scanned_by_kind,
            "scanned_fields": report.scanned_fields,
            "semantic_used": report.semantic_used,
            "semantic_threshold": report.semantic_threshold,
            "llm_used": report.llm_used,
            "judged_count": report.judged_count,
            "judge_skipped": report.judge_skipped,
            "dismissed_count": report.dismissed_count,
            # Per-level recall breakdown (design §6.3): each level's contribution to coverage
            # stays separately inspectable instead of collapsing into one opaque hit count.
            "l1_total": report.l1_total,
            "l2_total": report.l2_total,
            "l1_l2_overlap": report.l1_l2_overlap,
            "hits": [finding.__dict__ for finding in report.hits],
            "review_suggested": [finding.__dict__ for finding in report.review_suggested],
            "elements": [
                {
                    "element_id": element.element_id,
                    "element_text": element.element_text,
                    "kind": element.kind,
                    "polarity": element.polarity,
                    "l1_refs": element.l1_refs,
                    "l2_refs": element.l2_refs,
                    "l3_refs": element.l3_refs,
                    "candidate_refs": element.candidate_refs,
                    "findings": [finding.__dict__ for finding in element.findings],
                }
                for element in report.elements
            ],
            "markdown": render_rubric_markdown(report),
            "cost_budget": (
                summarize_workflow(
                    [llm_step("theme_sweep", telemetry.summary())]
                ).budget.model_dump(mode="json")
                if telemetry is not None
                else _deterministic_cost_budget("style_sweep")
            ),
        }
        return _emit(payload, args)


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


def _default_checkpoint_path(content_root: Path) -> Path:
    # Independent from runtime.sqlite (design §4): execution-state blobs never pollute the
    # business/audit DB and the two write paths never contend on one file's WAL write lock.
    return content_root / ".owcopilot" / "checkpoints.sqlite"


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


def _load_row_struct_map(path: Path) -> dict[str, dict[str, str]]:
    """Read the export-side row-struct rename map ({table: {canonical: export_name}}).

    Guided errors instead of raw tracebacks: the file must be a JSON object whose values are
    themselves string->string objects. Validation stops here so the exporter only ever sees a
    well-typed map."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"row-struct-map 文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"row-struct-map 文件不是合法 JSON（{path.name}）："
            f"{exc.msg} 在第 {exc.lineno} 行第 {exc.colno} 列"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(
            "row-struct-map 文件必须是 JSON 对象，形如 "
            '{"<table>": {"<CanonicalField>": "<ExportName>"}}'
        )
    result: dict[str, dict[str, str]] = {}
    for table, fields in data.items():
        if not isinstance(fields, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in fields.items()
        ):
            raise ValueError(
                f"row-struct-map 表 {table!r} 的值必须是字段名到导出名的对象"
                '（{"<CanonicalField>": "<ExportName>"}）'
            )
        result[table] = dict(fields)
    return result


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
