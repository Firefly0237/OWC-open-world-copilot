from __future__ import annotations

import pytest

from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
from owcopilot.content.store import ContentStore
from owcopilot.core.skills import (
    CostTier,
    SideEffect,
    Skill,
    SkillError,
    SkillParameter,
    SkillRegistry,
    default_skill_registry,
)


def _dirty_project(content_root) -> None:
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


def _echo_skill(name: str = "echo", *, required: bool = False) -> Skill:
    return Skill(
        name=name,
        description="Echo the args back.",
        cost_tier=CostTier.DETERMINISTIC,
        side_effect=SideEffect.READ_ONLY,
        handler=lambda **kw: {"echo": kw},
        parameters=(SkillParameter("value", "string", "Anything.", required=required),),
    )


# --------------------------------------------------------------------------- abstraction
def test_registry_register_get_and_contains() -> None:
    registry = SkillRegistry()
    registry.register(_echo_skill())
    assert "echo" in registry
    assert registry.names() == ["echo"]
    assert len(registry) == 1
    assert registry.get("echo").name == "echo"


def test_registry_rejects_duplicate_registration() -> None:
    registry = SkillRegistry()
    registry.register(_echo_skill())
    with pytest.raises(SkillError, match="already registered"):
        registry.register(_echo_skill())


def test_registry_unknown_skill_lists_available() -> None:
    registry = SkillRegistry()
    registry.register(_echo_skill())
    with pytest.raises(SkillError, match="unknown skill 'nope'.*echo"):
        registry.run("nope", {})


def test_skill_missing_required_argument_raises() -> None:
    registry = SkillRegistry()
    registry.register(_echo_skill(required=True))
    with pytest.raises(SkillError, match="missing required argument.*value"):
        registry.run("echo", {})


def test_skill_drops_undeclared_arguments() -> None:
    # A hallucinated extra arg (or a re-supplied session arg) must not reach the bound handler.
    registry = SkillRegistry()
    registry.register(_echo_skill())
    out = registry.run("echo", {"value": "ok", "content_root": "/hax", "bogus": 1})
    assert out == {"echo": {"value": "ok"}}


def test_skill_signature_and_manifest_marks_required() -> None:
    skill = _echo_skill(required=True)
    assert skill.signature() == "echo(value*: string)"
    line = skill.manifest_line()
    assert "echo(value*: string)" in line
    assert "[deterministic; read_only]" in line


# --------------------------------------------------------------------------- builtin set
def test_default_registry_exposes_safe_tool_surface(tmp_path) -> None:
    registry = default_skill_registry(content_root=str(tmp_path))
    assert set(registry.names()) == {
        "audit_project",
        "list_issues",
        "build_context_pack",
        "impact_of",
        "propose_fix",
        "quality_harness",
    }
    # Whole action space is deterministic + (read-only | proposes a patch) — never a canon write.
    for skill in registry:
        assert skill.cost_tier is CostTier.DETERMINISTIC
        assert skill.side_effect in {SideEffect.READ_ONLY, SideEffect.PROPOSES_PATCH}
    assert registry.get("propose_fix").side_effect is SideEffect.PROPOSES_PATCH


def test_default_registry_hides_session_args_from_the_model(tmp_path) -> None:
    registry = default_skill_registry(content_root=str(tmp_path))
    for skill in registry:
        names = {p.name for p in skill.parameters}
        assert "content_root" not in names
        assert "sqlite_path" not in names


def test_default_registry_audit_skill_runs_against_real_project(tmp_path) -> None:
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))

    result = registry.run("audit_project", {})

    assert result["open_errors"] >= 1
    assert "UNKNOWN_ENTITY_REF" in {issue["rule_code"] for issue in result["issues"]}


def test_default_registry_propose_fix_requires_issue_id(tmp_path) -> None:
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))
    with pytest.raises(SkillError, match="missing required argument.*issue_id"):
        registry.run("propose_fix", {})
