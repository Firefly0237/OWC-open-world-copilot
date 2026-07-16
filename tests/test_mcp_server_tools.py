from __future__ import annotations

from pathlib import Path

import pytest

from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
from owcopilot.content.store import ContentStore
from owcopilot.mcp_server import (
    ask_lore,
    audit_project,
    build_context_pack,
    export_project,
    list_issues,
    quality_harness,
)


def _write_project(content_root) -> None:
    ContentStore(content_root).save(
        ContentBundle(
            entities={
                "npc_aldric": Entity(
                    id="npc_aldric",
                    name="Aldric",
                    type=EntityType.NPC,
                    description="Caravan master",
                )
            },
            quests={"q1": Quest(id="q1", title="Q1", giver_npc="npc_missing")},
        )
    )


def _write_clean_project(content_root) -> None:
    ContentStore(content_root).save(
        ContentBundle(
            entities={
                "npc_aldric": Entity(
                    id="npc_aldric",
                    name="Aldric",
                    type=EntityType.NPC,
                    description="Caravan master",
                )
            },
            quests={
                "q1": Quest(
                    id="q1",
                    title="Q1",
                    giver_npc="npc_aldric",
                    objective="Help Aldric.",
                    localization_keys=["quest.q1.objective"],
                )
            },
        )
    )


def _write_overview_project(content_root) -> None:
    ContentStore(content_root).save(
        ContentBundle.model_validate(
            {
                "entities": {
                    "fac_iron": {
                        "id": "fac_iron",
                        "name": "铁盟",
                        "type": "faction",
                        "description": "铁盟是一个势力。",
                    },
                    "fac_salt": {
                        "id": "fac_salt",
                        "name": "盐会",
                        "type": "faction",
                        "description": "盐会是一个势力。",
                    },
                    "fac_mist": {
                        "id": "fac_mist",
                        "name": "雾党",
                        "type": "faction",
                        "description": "雾党是一个势力。",
                    },
                    "npc_a": {
                        "id": "npc_a",
                        "name": "阿尔",
                        "type": "npc",
                        "description": "阿尔是一名角色。",
                    },
                    "npc_b": {
                        "id": "npc_b",
                        "name": "贝拉",
                        "type": "npc",
                        "description": "贝拉是一名角色。",
                    },
                    "npc_c": {
                        "id": "npc_c",
                        "name": "卡尔",
                        "type": "npc",
                        "description": "卡尔是一名角色。",
                    },
                    "npc_d": {
                        "id": "npc_d",
                        "name": "黛西",
                        "type": "npc",
                        "description": "黛西是一名角色。",
                    },
                    "npc_e": {
                        "id": "npc_e",
                        "name": "俄岚",
                        "type": "npc",
                        "description": "俄岚是一名角色。",
                    },
                },
                "relations": [
                    {"source": "npc_a", "target": "fac_iron", "kind": "member_of"},
                    {"source": "npc_b", "target": "fac_iron", "kind": "member_of"},
                    {"source": "npc_c", "target": "fac_salt", "kind": "member_of"},
                    {"source": "npc_d", "target": "fac_salt", "kind": "member_of"},
                    {"source": "npc_e", "target": "fac_mist", "kind": "member_of"},
                    {"source": "fac_iron", "target": "fac_salt", "kind": "enemy_of"},
                    {"source": "fac_salt", "target": "fac_mist", "kind": "rival_of"},
                ],
            }
        )
    )


def test_mcp_audit_project_persists_and_lists_issues(tmp_path) -> None:
    content_root = tmp_path / "content"
    _write_project(content_root)

    audit = audit_project(content_root=str(content_root))
    issues = list_issues(
        content_root=str(content_root),
        rule_code="UNKNOWN_ENTITY_REF",
        status="open",
    )

    assert audit["open_errors"] >= 1
    assert "UNKNOWN_ENTITY_REF" in {issue["rule_code"] for issue in audit["issues"]}
    assert audit["cost_budget"]["used_usd"] == 0.0
    assert issues["count"] == 1
    assert issues["issues"][0]["target_ref"] == "quest:q1"
    assert issues["cost_budget"]["used_usd"] == 0.0


def test_mcp_list_issues_treats_empty_filter_as_no_filter(tmp_path) -> None:
    content_root = tmp_path / "content"
    _write_project(content_root)
    audit_project(content_root=str(content_root))  # persist the issues first

    # A tool-calling model commonly passes "" to mean "unset" (real DeepSeek did this); an empty
    # filter must NOT match zero rows.
    issues = list_issues(content_root=str(content_root), severity="", rule_code="", status="")
    assert issues["count"] >= 1


def test_mcp_context_pack_and_ask_lore(tmp_path) -> None:
    content_root = tmp_path / "content"
    _write_project(content_root)

    pack = build_context_pack(content_root=str(content_root), query="Aldric caravan")
    answer = ask_lore(content_root=str(content_root), query="Who is Aldric?", max_cost_usd=0.0)

    assert "entity:npc_aldric" in pack["refs"]
    assert pack["cost_budget"]["used_usd"] == 0.0
    assert answer["answer"]["citations"][0]["ref"] == "entity:npc_aldric"
    assert answer["telemetry"]["calls"] == 1
    assert answer["cost_budget"]["over_budget"] is True


def test_mcp_ask_lore_uses_qa_overview_reports(tmp_path, monkeypatch) -> None:
    from owcopilot.app.actions import run_build_overview_action

    # Feature test on a tiny world: open the RetrievalPolicy size-gate so GraphRAG community
    # reports are used (they would be omitted at this small scale by default).
    monkeypatch.setattr("owcopilot.retrieval.policy.GRAPHRAG_MIN_SIZE", 0)
    content_root = tmp_path / "content"
    sqlite_path = str(tmp_path / "runtime.sqlite")
    _write_overview_project(content_root)
    run_build_overview_action(content_root, sqlite_path=sqlite_path, llm_mode="offline")

    answer = ask_lore(
        content_root=str(content_root),
        sqlite_path=sqlite_path,
        query="列出这个世界的主要势力以及它们之间的关系",
        budget_tokens=800,
    )

    assert answer["answer"]["refused"] is False
    assert any(
        citation["ref"].startswith("community:") for citation in answer["answer"]["citations"]
    )


def test_mcp_quality_harness_returns_loop_state_and_proposals(tmp_path) -> None:
    content_root = tmp_path / "content"
    _write_project(content_root)

    result = quality_harness(content_root=str(content_root), max_issues=3)

    assert result["phase"] == "repair"
    assert result["export_ready"] is False
    assert result["top_issues"]
    assert result["patch_proposals"]
    assert result["patch_proposals"][0]["candidates"]
    assert result["next_tool_calls"][0]["tool"] == "list_issues"
    assert "audit_project" in result["tool_trace"]
    assert result["cost_budget"]["used_usd"] == 0.0


def test_mcp_quality_harness_reports_export_ready_for_clean_project(tmp_path) -> None:
    content_root = tmp_path / "content"
    _write_clean_project(content_root)

    result = quality_harness(content_root=str(content_root), propose_fixes=False)

    assert result["phase"] in {"complete_design", "ready_to_export"}
    assert result["export_ready"] is True
    assert result["export_blockers"] == []


def test_mcp_export_project_writes_engine_scoped_bundle(tmp_path, monkeypatch) -> None:
    content_root = tmp_path / "content"
    output_root = tmp_path / "exports"
    _write_clean_project(content_root)
    # output_root is outside content_root/.owcopilot/exports; widen the export root to tmp_path.
    monkeypatch.setenv("OWCOPILOT_EXPORT_ROOT", str(tmp_path))

    result = export_project(
        content_root=str(content_root),
        output_dir=str(output_root),
        target_engine="generic",
    )

    export_dir = Path(result["output_dir"])
    assert export_dir.name == "generic"
    assert (export_dir / "content_bundle.json").exists()
    assert (export_dir / "manifest.json").exists()
    assert result["manifest"]["target_engine"] == "generic"
    assert result["cost_budget"]["used_usd"] == 0.0


def test_mcp_export_project_blocks_open_errors(tmp_path, monkeypatch) -> None:
    content_root = tmp_path / "content"
    output_root = tmp_path / "exports"
    _write_project(content_root)
    monkeypatch.setenv("OWCOPILOT_EXPORT_ROOT", str(tmp_path))

    with pytest.raises(ValueError, match="导出被发布门阻断"):
        export_project(
            content_root=str(content_root),
            output_dir=str(output_root),
            target_engine="generic",
        )


def test_mcp_export_project_rejects_path_escape(tmp_path, monkeypatch) -> None:
    """A model/client-supplied traversal or out-of-root path is refused with a guided error
    naming OWCOPILOT_EXPORT_ROOT — never a silent write outside the sandbox."""
    content_root = tmp_path / "content"
    _write_clean_project(content_root)
    monkeypatch.setenv("OWCOPILOT_EXPORT_ROOT", str(tmp_path / "allowed"))

    with pytest.raises(ValueError, match="OWCOPILOT_EXPORT_ROOT"):
        export_project(
            content_root=str(content_root),
            output_dir=str(tmp_path / "elsewhere"),  # sibling, outside the allowed root
            target_engine="generic",
        )
    with pytest.raises(ValueError, match="OWCOPILOT_EXPORT_ROOT"):
        export_project(
            content_root=str(content_root),
            output_dir="../escape",  # relative traversal out of the root
            target_engine="generic",
        )


def test_mcp_list_issues_pagination(tmp_path) -> None:
    """limit/offset page the issues with a total + truncation_notice (never a silent drop); the
    default (no limit) keeps the original key set exactly."""
    content_root = tmp_path / "content"
    _write_project(content_root)  # one quest with a missing giver → ≥1 issue; add more below
    ContentStore(content_root).save(
        ContentBundle(
            entities={
                "npc_a": Entity(id="npc_a", name="A", type=EntityType.NPC, description="x"),
            },
            quests={
                f"q{i}": Quest(id=f"q{i}", title=f"Q{i}", giver_npc="npc_missing") for i in range(5)
            },
        )
    )
    audit_project(content_root=str(content_root))  # persist issues
    total = list_issues(content_root=str(content_root))["count"]
    assert total >= 5

    page1 = list_issues(content_root=str(content_root), limit=2, offset=0)
    assert len(page1["issues"]) == 2
    assert page1["total"] == total
    assert page1["next_offset"] == 2
    assert "truncation_notice" in page1

    page2 = list_issues(content_root=str(content_root), limit=2, offset=2)
    assert {i["id"] for i in page1["issues"]}.isdisjoint({i["id"] for i in page2["issues"]})

    # default (no pagination) → exactly the original keys, no total/notice
    default = list_issues(content_root=str(content_root))
    assert set(default) == {"count", "issues", "cost_budget"}


def test_mcp_audit_project_limit_bounds_issues_but_reports_true_total(tmp_path) -> None:
    content_root = tmp_path / "content"
    ContentStore(content_root).save(
        ContentBundle(
            quests={
                f"q{i}": Quest(id=f"q{i}", title=f"Q{i}", giver_npc="npc_missing") for i in range(5)
            }
        )
    )
    full = audit_project(content_root=str(content_root))
    assert set(full) == {"content_hash", "audit_run", "issues", "open_errors", "cost_budget"}
    n = len(full["issues"])
    assert n >= 5

    capped = audit_project(content_root=str(content_root), limit=2)
    assert len(capped["issues"]) == 2
    assert capped["total"] == n
    assert capped["open_errors"] == full["open_errors"]  # true un-paged total, not the page size
    assert "list_issues" in capped["truncation_notice"]


def test_mcp_tools_reject_missing_content_root(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="content root does not exist"):
        audit_project(content_root=str(tmp_path / "missing"))


# ---------------------------------------------------------------------------
# ask_lore honours the deployment LLM mode (audit finding: it used to hardwire the
# offline double with no gate and no real-provider branch — the only runtime surface
# that bypassed the OWCOPILOT_ALLOW_OFFLINE_LLM convention).
# ---------------------------------------------------------------------------


def test_mcp_ask_lore_fails_closed_without_offline_optin(tmp_path, monkeypatch) -> None:
    """Default (offline) mode must fail closed like CLI/REST/app — never silently serve
    fake extractive answers to a real MCP client."""
    content_root = tmp_path / "content"
    _write_project(content_root)
    monkeypatch.delenv("OWCOPILOT_ALLOW_OFFLINE_LLM", raising=False)
    monkeypatch.delenv("OWCOPILOT_LLM_MODE", raising=False)
    with pytest.raises(RuntimeError, match="未接入模型"):
        ask_lore(content_root=str(content_root), query="Who is Aldric?")


def test_mcp_ask_lore_real_mode_uses_real_provider_no_optin_needed(monkeypatch) -> None:
    """OWCOPILOT_LLM_MODE=real routes through build_real_provider and does NOT require the
    offline opt-in (a real deployment never sets the test fixture env)."""
    from owcopilot.mcp_server import tools

    monkeypatch.delenv("OWCOPILOT_ALLOW_OFFLINE_LLM", raising=False)
    monkeypatch.setenv("OWCOPILOT_LLM_MODE", "real")
    built = {}

    class _Stub:
        def complete(self, *, system, user, model):
            return "ok", 1, 1

    def _fake_build(model):
        built["model"] = model
        return _Stub()

    monkeypatch.setattr(tools, "build_real_provider", _fake_build)
    gateway, telemetry = tools._qa_gateway()
    assert built["model"]  # the real branch built the provider from the configured model id
    assert gateway.complete(task="qa_answer", system="s", user="u") == "ok"
