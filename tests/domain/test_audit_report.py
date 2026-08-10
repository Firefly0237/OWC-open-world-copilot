"""Tests for the Markdown audit report."""

from __future__ import annotations

import json
from pathlib import Path

from owcopilot.audit.context import AuditContext
from owcopilot.audit.default_rules import build_default_rule_registry
from owcopilot.audit.report import render_audit_markdown
from owcopilot.audit.runner import AuditRunner
from owcopilot.cli.main import main
from owcopilot.content.hash import content_hash
from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
from owcopilot.content.store import ContentStore


def _bundle_with_error() -> ContentBundle:
    return ContentBundle(
        entities={
            "npc_mara": Entity(
                id="npc_mara", name="Mara", type=EntityType.NPC, description="Scout."
            )
        },
        quests={
            "quest_patrol": Quest(
                id="quest_patrol",
                title="Patrol",
                giver_npc="npc_ghost",
                objective="Walk.",
                localization_keys=["quest.quest_patrol.objective"],
            )
        },
    )


def test_render_markdown_lists_open_issues_with_evidence() -> None:
    bundle = _bundle_with_error()
    result = AuditRunner(build_default_rule_registry()).run(AuditContext.from_bundle(bundle))
    text = render_audit_markdown(result, content_hash=content_hash(bundle))
    assert "# Content audit report" in text
    assert "UNKNOWN_ENTITY_REF" in text
    assert "quest:quest_patrol" in text
    assert "`giver_npc`" in text  # evidence path surfaces
    assert "| Rule | Open count |" in text


def test_render_markdown_clean_world() -> None:
    bundle = _bundle_with_error()
    bundle.quests["quest_patrol"].giver_npc = "npc_mara"
    result = AuditRunner(build_default_rule_registry()).run(AuditContext.from_bundle(bundle))
    text = render_audit_markdown(result, content_hash=content_hash(bundle))
    assert "No open issues" in text


def test_cli_audit_writes_markdown_report(tmp_path: Path, capsys) -> None:
    root = tmp_path / "content"
    ContentStore(root).save(_bundle_with_error())
    report_path = tmp_path / "report.md"
    code = main(
        [
            "audit",
            "--content-root",
            str(root),
            "--markdown-report",
            str(report_path),
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["markdown_report"] == str(report_path)
    assert "UNKNOWN_ENTITY_REF" in report_path.read_text(encoding="utf-8")
