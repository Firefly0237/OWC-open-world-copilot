"""Golden World fixture and deterministic evaluation runner."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..content.models import ContentBundle, Entity, EntityType, Quest, Relation
from ..content.store import ContentStore
from ..exporters import EngineTarget, export_content_bundle
from ..llm.cache import HashingEmbedder, NoOpCache
from ..llm.gateway import LLMGateway
from ..llm.router import StaticRouter
from ..llm.telemetry import TelemetryCollector
from ..pipeline.audit import run_full_audit
from ..pipeline.project import ProjectContext
from ..qa.offline import OfflineQAProvider
from ..qa.service import LoreQAService
from ..trust import summarize_provenance


class GoldenCheck(BaseModel):
    name: str
    passed: bool
    details: dict[str, Any] = Field(default_factory=dict)


class GoldenEvaluationReport(BaseModel):
    passed: bool
    checks: list[GoldenCheck]
    metrics: dict[str, Any] = Field(default_factory=dict)


def golden_content_bundle() -> ContentBundle:
    return ContentBundle(
        entities={
            "npc_aldric": Entity(
                id="npc_aldric",
                name="Aldric",
                type=EntityType.NPC,
                description="Caravan master who hires scouts for Northwatch.",
            ),
            "location_northwatch": Entity(
                id="location_northwatch",
                name="Northwatch",
                type=EntityType.LOCATION,
                description="A fortified trade town on the northern road.",
            ),
        },
        relations=[Relation(source="npc_aldric", target="location_northwatch", kind="located_in")],
        quests={
            "quest_missing_caravan": Quest(
                id="quest_missing_caravan",
                title="Missing Caravan",
                giver_npc="npc_aldric",
                location="location_northwatch",
                objective="Find the missing caravan before nightfall.",
                localization_keys=["quest.missing_caravan.objective"],
            )
        },
    )


def write_golden_world(content_root: str | Path) -> ContentBundle:
    bundle = golden_content_bundle()
    ContentStore(content_root).save(bundle)
    return bundle


def run_golden_evaluation(workspace: str | Path) -> GoldenEvaluationReport:
    root = Path(workspace)
    content_root = root / "golden_world"
    export_root = root / "exports"
    write_golden_world(content_root)

    project = ProjectContext.open(
        content_root, sqlite_path=root / "runtime.sqlite", embedder=HashingEmbedder()
    )
    try:
        audit = run_full_audit(project)
        pack = project.context_builder.build("Aldric caravan", budget_tokens=200)
        telemetry = TelemetryCollector()
        answer = LoreQAService(
            gateway=LLMGateway(
                providers={"cheap": OfflineQAProvider()},
                router=StaticRouter(mapping={"qa_answer": "cheap"}),
                cache=NoOpCache(),
                telemetry=telemetry,
            ),
            context_builder=project.context_builder,
            bundle=project.bundle,
        ).ask("Who is Aldric?", budget_tokens=200)
        manifest = export_content_bundle(
            project.bundle,
            export_root / EngineTarget.GENERIC.value,
            target_engine=EngineTarget.GENERIC,
        )
        provenance = summarize_provenance(project.bundle)
    finally:
        project.close()

    checks = [
        GoldenCheck(
            name="audit_no_open_errors",
            passed=not audit.open_errors,
            details={"open_errors": len(audit.open_errors), "totals": audit.run.totals},
        ),
        GoldenCheck(
            name="retrieval_has_aldric",
            passed="entity:npc_aldric" in pack.refs,
            details={"refs": pack.refs},
        ),
        GoldenCheck(
            name="qa_grounded",
            passed=(not answer.refused and bool(answer.citations)),
            details=answer.model_dump(mode="json"),
        ),
        GoldenCheck(
            name="export_manifest_written",
            passed=(export_root / EngineTarget.GENERIC.value / "manifest.json").exists(),
            details=manifest.model_dump(mode="json"),
        ),
        GoldenCheck(
            name="provenance_all_approved",
            passed=not provenance.unreviewed_ai_refs,
            details=provenance.model_dump(mode="json"),
        ),
    ]
    return GoldenEvaluationReport(
        passed=all(check.passed for check in checks),
        checks=checks,
        metrics={
            "content_root": str(content_root),
            "export_root": str(export_root),
            "qa_telemetry": telemetry.summary(),
        },
    )
