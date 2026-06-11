"""Command-line entrypoint for the v2 project workflow."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..assist.barks import BarkBatchService
from ..assist.drafts import QuestDraftService
from ..assist.offline import OfflineBarksProvider, OfflineQuestDraftProvider
from ..assist.review_queue import ReviewItemType, ReviewQueue
from ..audit.baseline import AuditBaseline, issue_fingerprint
from ..audit.default_rules import build_default_rule_registry
from ..audit.models import IssueStatus
from ..audit.report import render_audit_markdown
from ..audit.runner import AuditRunner
from ..content.hash import content_hash
from ..content.ingest import ingest_raw_objects, parse_paths
from ..content.mapping import FieldMapping, apply_field_mapping
from ..content.models import Quest, ReviewStatus
from ..evaluation import run_acceptance_evaluation, run_golden_evaluation
from ..exporters import EngineTarget, export_content_bundle
from ..impact import Change, ChangeSet, ChangeType, ImpactAnalyzer, ImpactLevel
from ..llm.cache import NoOpCache
from ..llm.gateway import LLMGateway, OpenAICompatProvider
from ..llm.router import StaticRouter
from ..llm.telemetry import TelemetryCollector
from ..pipeline.audit import run_full_audit
from ..pipeline.ingest import run_ingest
from ..pipeline.patches import (
    apply_patch_workflow,
    find_issue,
    rollback_patch_workflow,
    suggest_for_issue,
)
from ..pipeline.project import ProjectContext
from ..qa.offline import OfflineQAProvider
from ..qa.service import LoreQAService
from ..telemetry import deterministic_step, llm_step, summarize_workflow
from ..util import load_dotenv


def main(argv: Sequence[str] | None = None) -> int:
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

    issues = subparsers.add_parser("issues", help="List persisted audit issues.")
    _add_project_args(issues)
    issues.add_argument("--severity")
    issues.add_argument("--rule-code")
    issues.add_argument("--status")
    issues.set_defaults(handler=_cmd_issues)

    context = subparsers.add_parser("context-pack", help="Build a retrieval context pack.")
    _add_project_args(context)
    context.add_argument("--query", required=True)
    context.add_argument("--budget-tokens", type=int, default=800)
    context.set_defaults(handler=_cmd_context_pack)

    ask = subparsers.add_parser("ask", help="Answer a lore question with grounded citations.")
    _add_project_args(ask)
    ask.add_argument("--query", required=True)
    ask.add_argument("--budget-tokens", type=int, default=800)
    ask.add_argument("--max-cost-usd", type=float)
    _add_llm_args(ask)
    ask.set_defaults(handler=_cmd_ask)

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
    impact.add_argument("--max-depth", type=int, default=2)
    impact.set_defaults(handler=_cmd_impact)

    suggest = subparsers.add_parser(
        "suggest",
        help="Propose shadow-validated fix candidates for a persisted audit issue.",
    )
    _add_project_args(suggest)
    suggest.add_argument("--issue-id", required=True, help="Issue id from `owcopilot issues`.")
    suggest.add_argument("--max-candidates", type=int, default=3)
    suggest.add_argument("--budget-tokens", type=int, default=600)
    _add_llm_args(suggest)
    suggest.set_defaults(handler=_cmd_suggest)

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
    draft.add_argument("--budget-tokens", type=int, default=800)
    _add_llm_args(draft)
    draft.set_defaults(handler=_cmd_draft)

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

    export = subparsers.add_parser("export", help="Export project content as engine files.")
    _add_project_args(export)
    export.add_argument("--output-dir", required=True)
    export.add_argument(
        "--target-engine",
        choices=[target.value for target in EngineTarget],
        default=EngineTarget.GENERIC.value,
    )
    export.set_defaults(handler=_cmd_export)

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

    return parser


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
        help="Use the deterministic offline provider or a real OpenAI-compatible provider.",
    )
    parser.add_argument(
        "--llm-model",
        default=os.getenv("OWCOPILOT_CHEAP_MODEL", "deepseek-v4-flash"),
        help="Model id for --llm-mode real.",
    )


def _llm_gateway(
    args: argparse.Namespace, *, task: str, offline_provider: Any
) -> tuple[LLMGateway, TelemetryCollector]:
    telemetry = TelemetryCollector()
    real = getattr(args, "llm_mode", "offline") == "real"
    if real:
        load_dotenv()  # pick up OPENAI_BASE_URL / OPENAI_API_KEY from .env; shell env wins
        provider: Any = OpenAICompatProvider(model=args.llm_model)
    else:
        provider = offline_provider
    gateway = LLMGateway(
        providers={"cheap": provider},
        router=StaticRouter(mapping={task: "cheap"}),
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
                "changes": [
                    change.model_dump(mode="json")
                    for change in result.changes
                ],
                "issues": [
                    issue.model_dump(mode="json")
                    for issue in result.issues
                ],
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
            baseline = (
                _load_baseline(Path(args.baseline)) if args.baseline else AuditBaseline()
            )
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
    with _project(args) as project:
        gateway, telemetry = _llm_gateway(
            args, task="qa_answer", offline_provider=OfflineQAProvider()
        )
        service = LoreQAService(
            gateway=gateway,
            context_builder=project.context_builder,
            bundle=project.bundle,
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


def _cmd_export(args: argparse.Namespace) -> int:
    with _project(args) as project:
        target_engine = EngineTarget(args.target_engine)
        output_dir = Path(args.output_dir) / target_engine.value
        manifest = export_content_bundle(
            project.bundle,
            output_dir,
            target_engine=target_engine,
        )
        return _emit(
            {
                "output_dir": str(output_dir),
                "manifest": manifest.model_dump(mode="json"),
                "cost_budget": _deterministic_cost_budget("export_project"),
            },
            args,
        )


def _cmd_eval_golden(args: argparse.Namespace) -> int:
    report = run_golden_evaluation(args.workspace)
    _emit(report.model_dump(mode="json"), args)
    return 0 if report.passed else 1


def _cmd_eval_acceptance(args: argparse.Namespace) -> int:
    report = run_acceptance_evaluation(args.workspace)
    _emit(report.model_dump(mode="json"), args)
    return 0 if report.passed else 1


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
            summarize_workflow([llm_step("patch_suggest", telemetry_summary)]).budget
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
                    [llm_step("quest_draft", telemetry_summary)]
                ).budget.model_dump(mode="json"),
            },
            args,
        )


def _cmd_barks(args: argparse.Namespace) -> int:
    with _project(args) as project:
        speakers = [item.strip() for item in args.speakers.split(",") if item.strip()]
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
                    [llm_step("barks_batch", telemetry_summary)]
                ).budget.model_dump(mode="json"),
            },
            args,
        )


def _cmd_review(args: argparse.Namespace) -> int:
    with _project(args) as project:
        queue = ReviewQueue(project.sqlite_store)
        if args.accept and args.reject:
            raise ValueError("--accept and --reject are mutually exclusive")
        if args.accept or args.reject:
            if not args.operator:
                raise ValueError("--operator is required with --accept/--reject")
        if args.accept:
            item = queue.get(args.accept)
            if item.item_type is ReviewItemType.PATCH_CANDIDATE:
                raise ValueError(
                    "patch candidates are applied with `owcopilot apply --patch-id ...`"
                )
            written_ref: str | None = None
            if item.item_type is ReviewItemType.QUEST_DRAFT:
                quest = Quest.model_validate(item.payload)
                quest = quest.model_copy(update={"review_status": ReviewStatus.APPROVED})
                bundle = project.content_store.load()
                bundle.quests[quest.id] = quest
                project.content_store.save(bundle)
                project.reload()
                written_ref = f"quest:{quest.id}"
            decided = queue.mark(item.id, "accepted", decided_by=args.operator)
            audit = run_full_audit(project, persist=True)
            return _emit(
                {
                    "decision": "accepted",
                    "item": decided.model_dump(mode="json"),
                    "written_ref": written_ref,
                    "post_audit_open_errors": len(audit.open_errors),
                    "cost_budget": _deterministic_cost_budget("review_decide"),
                },
                args,
            )
        if args.reject:
            decided = queue.mark(args.reject, "rejected", decided_by=args.operator)
            return _emit(
                {
                    "decision": "rejected",
                    "item": decided.model_dump(mode="json"),
                    "cost_budget": _deterministic_cost_budget("review_decide"),
                },
                args,
            )
        pending = queue.list_pending()
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
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("field mapping file must contain a JSON object")
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
