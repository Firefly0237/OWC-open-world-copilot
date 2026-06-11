"""End-to-end CLI tests for the v2 close-the-loop commands:
audit -> issues -> suggest -> apply -> rollback, impact, draft/review, barks, update-baseline.
All offline, $0.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from owcopilot.cli.main import main
from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest, Relation
from owcopilot.content.store import ContentStore


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "content"
    bundle = ContentBundle(
        entities={
            "npc_mara": Entity(
                id="npc_mara", name="Mara", type=EntityType.NPC, description="Border scout."
            ),
            "loc_fort": Entity(
                id="loc_fort",
                name="Border Fort",
                type=EntityType.LOCATION,
                description="A fort on the border.",
            ),
            "fac_guard": Entity(
                id="fac_guard",
                name="Border Guard",
                type=EntityType.FACTION,
                description="Keeps the border roads safe.",
            ),
        },
        relations=[
            Relation(source="npc_mara", target="loc_fort", kind="located_in"),
            Relation(source="npc_mara", target="fac_guard", kind="member_of"),
        ],
        quests={
            "quest_patrol": Quest(
                id="quest_patrol",
                title="Patrol the Border",
                giver_npc="npc_ghost",  # seeded UNKNOWN_ENTITY_REF error
                location="loc_fort",
                objective="Walk the border line before dusk.",
                localization_keys=["quest.quest_patrol.objective"],
            )
        },
    )
    ContentStore(root).save(bundle)
    return root


def _run(capsys, *argv: str) -> tuple[int, dict]:
    code = main(list(argv))
    out = capsys.readouterr().out.strip().splitlines()
    payload = json.loads(out[-1]) if out else {}
    return code, payload


def test_suggest_apply_rollback_loop(project_root: Path, capsys) -> None:
    root = str(project_root)
    code, audit = _run(capsys, "audit", "--content-root", root, "--fail-on-error")
    assert code == 1  # the seeded error trips the gate
    assert audit["open_errors"] == 1

    code, issues = _run(
        capsys, "issues", "--content-root", root, "--rule-code", "UNKNOWN_ENTITY_REF"
    )
    assert code == 0 and issues["count"] == 1
    issue_id = issues["issues"][0]["id"]

    code, suggest = _run(capsys, "suggest", "--content-root", root, "--issue-id", issue_id)
    assert code == 0
    assert not suggest["used_llm"]
    assert suggest["candidates"], "deterministic candidate expected"
    patch_id = suggest["candidates"][0]["patch_id"]
    assert suggest["candidates"][0]["target_resolved"]

    code, applied = _run(
        capsys, "apply", "--content-root", root, "--patch-id", patch_id, "--operator", "lead"
    )
    assert code == 0 and applied["applied"] is True
    assert applied["post_audit_open_errors"] == 0
    quest_file = json.loads(
        (project_root / "quests" / "quest_patrol.json").read_text(encoding="utf-8")
    )
    assert "giver_npc" not in quest_file  # dangling ref removed on disk

    # Re-applying the same patch must be refused (status moved on).
    code, _ = _run(
        capsys, "apply", "--content-root", root, "--patch-id", patch_id, "--operator", "lead"
    )
    assert code == 2

    code, rolled = _run(
        capsys, "rollback", "--content-root", root, "--patch-id", patch_id, "--operator", "lead"
    )
    assert code == 0 and rolled["rolled_back"] is True
    quest_file = json.loads(
        (project_root / "quests" / "quest_patrol.json").read_text(encoding="utf-8")
    )
    assert quest_file["giver_npc"] == "npc_ghost"  # restored
    assert rolled["post_audit_open_errors"] == 1


def test_impact_command(project_root: Path, capsys) -> None:
    code, payload = _run(
        capsys,
        "impact",
        "--content-root",
        str(project_root),
        "--change",
        "entity_delete:entity:loc_fort",
    )
    assert code == 0
    must = {item["target_ref"] for item in payload["must_change"]}
    assert "quest:quest_patrol" in must or "entity:npc_mara" in must
    assert payload["total"] >= 1


def test_impact_rejects_unknown_change_type(project_root: Path, capsys) -> None:
    code, _ = _run(
        capsys, "impact", "--content-root", str(project_root), "--change", "nuke:entity:x"
    )
    assert code == 2


def test_draft_review_accept_writes_quest(project_root: Path, capsys) -> None:
    root = str(project_root)
    code, draft = _run(
        capsys, "draft", "--content-root", root, "--brief", "Escort the salt caravan to the fort"
    )
    assert code == 0
    assert draft["quest"]["origin"] == "ai_draft"
    assert draft["quest"]["review_status"] == "pending_review"
    item_id = draft["review_item_id"]

    code, pending = _run(capsys, "review", "--content-root", root)
    assert code == 0 and pending["count"] == 1

    code, decided = _run(
        capsys, "review", "--content-root", root, "--accept", item_id, "--operator", "lead"
    )
    assert code == 0
    assert decided["decision"] == "accepted"
    written = decided["written_ref"]
    assert written and written.startswith("quest:")
    quest_id = written.split(":", 1)[1]
    saved = json.loads(
        (project_root / "quests" / f"{quest_id}.json").read_text(encoding="utf-8")
    )
    assert saved["review_status"] == "approved"
    assert saved["origin"] == "ai_draft"  # provenance survives approval

    code, pending = _run(capsys, "review", "--content-root", root)
    assert pending["count"] == 0


def test_review_reject_keeps_content_untouched(project_root: Path, capsys) -> None:
    root = str(project_root)
    _, draft = _run(capsys, "draft", "--content-root", root, "--brief", "A second quest idea")
    item_id = draft["review_item_id"]
    quest_id = draft["quest"]["id"]
    code, decided = _run(
        capsys, "review", "--content-root", root, "--reject", item_id, "--operator", "lead"
    )
    assert code == 0 and decided["decision"] == "rejected"
    assert not (project_root / "quests" / f"{quest_id}.json").exists()


def test_barks_offline_into_review_queue(project_root: Path, capsys) -> None:
    root = str(project_root)
    code, barks = _run(
        capsys,
        "barks",
        "--content-root",
        root,
        "--speakers",
        "npc_mara",
        "--topic",
        "spotted an intruder",
        "--variants",
        "3",
        "--max-chars",
        "60",
    )
    assert code == 0
    assert len(barks["accepted"]) == 3
    assert all(len(item["text"]) <= 60 for item in barks["accepted"])
    assert len(barks["review_item_ids"]) == 3

    code, pending = _run(capsys, "review", "--content-root", root)
    assert pending["count"] == 3


def test_barks_rejects_unknown_speaker(project_root: Path, capsys) -> None:
    code, _ = _run(
        capsys,
        "barks",
        "--content-root",
        str(project_root),
        "--speakers",
        "npc_nobody",
        "--topic",
        "hello",
    )
    assert code == 2


def test_update_baseline_then_gate_passes(project_root: Path, capsys, tmp_path: Path) -> None:
    root = str(project_root)
    baseline_path = str(tmp_path / "baseline.json")
    code, payload = _run(
        capsys,
        "audit",
        "--content-root",
        root,
        "--update-baseline",
        baseline_path,
    )
    assert code == 0
    assert payload["baseline_size"] >= 1
    assert Path(baseline_path).exists()

    # With the baseline accepted, the same world now passes the gate (ratchet mode).
    code, gated = _run(
        capsys,
        "audit",
        "--content-root",
        root,
        "--baseline",
        baseline_path,
        "--fail-on-error",
    )
    assert code == 0
    assert gated["open_errors"] == 0
