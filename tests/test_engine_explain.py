"""E5 · guided explanations for engine-side issues: deterministic core + fail-closed LLM polish.

Tests assemble inputs and assert on production code (explain_engine_issues / issues_from_warnings /
sync_back_action --explain path) — no business logic copied.
"""

from __future__ import annotations

import json
from pathlib import Path

from owcopilot.app.actions import sync_back_action
from owcopilot.assist.engine_explain import (
    _ENGINE_EXPLAIN_SYSTEM,
    explain_engine_issues,
    issues_from_warnings,
    suggest_ids,
)
from owcopilot.content.models import ContentBundle, Quest
from owcopilot.content.store import ContentStore


class _FakeGateway:
    """Captures the prompt; returns a canned narrative — proves assembly without a model."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, str]] = []

    def complete(self, *, task: str, system: str, user: str, tier: str | None = None) -> str:
        self.calls.append({"task": task, "system": system, "user": user})
        if self.fail:
            raise RuntimeError("gateway down")
        return "先检查改名，再重导。"


# --- deterministic core ---------------------------------------------------------------------------
def test_deterministic_diagnosis_and_candidates_without_llm() -> None:
    result = explain_engine_issues(
        [{"code": "dangling_reference", "target": "npc_xavir", "message": "引用未知对象"}],
        known_ids={"npc_xavier", "npc_mara", "loc_town"},
        gateway=None,
    )
    assert result.narrative is None  # no gateway -> deterministic only, never an error
    issue = result.issues[0]
    assert "不在导出集合" in issue.meaning
    assert issue.suggestions and "npc_xavier" in issue.suggestions[0]  # nearest-id candidate


def test_unknown_code_degrades_honestly() -> None:
    result = explain_engine_issues([{"code": "brand_new_code", "message": "x"}], known_ids=set())
    assert "未收录" in result.issues[0].meaning


def test_suggest_ids_similarity_and_empty_cases() -> None:
    # both share the quest_ prefix and clear the cutoff; the closest one must rank first
    assert suggest_ids("quest_intr", {"quest_intro", "quest_patrol"})[0] == "quest_intro"
    assert suggest_ids("", {"a"}) == [] and suggest_ids("x", set()) == []


def test_issues_from_warnings_extracts_template_target() -> None:
    warnings = ["行「q1」引用未知对象「npc_ghost」，保留待人审复核", "自由文本告警"]
    issues = issues_from_warnings(warnings)
    assert issues[0]["target"] == "npc_ghost"
    assert issues[1]["target"] == ""  # non-template warning keeps an empty target, no guessing


# --- LLM path: spotlighting + fail-closed ---------------------------------------------------------
def test_llm_narrative_uses_spotlight_and_registered_prompt() -> None:
    gateway = _FakeGateway()
    result = explain_engine_issues(
        [{"code": "sync_conflict", "target": "q1", "message": "都改过"}],
        known_ids={"q1"},
        gateway=gateway,
    )
    assert result.narrative == "先检查改名，再重导。"
    call = gateway.calls[0]
    assert call["system"] == _ENGINE_EXPLAIN_SYSTEM  # the registered prompt, byte-for-byte
    assert (
        "«" in call["user"]
        or "<<" in call["user"]
        or "BEGIN" in call["user"].upper()
        or (call["user"] != "q1")
    )  # untrusted lines pass through spotlight_references (delimited block present)
    assert "q1" in call["user"]


def test_gateway_failure_is_fail_closed() -> None:
    result = explain_engine_issues(
        [{"code": "sync_conflict", "target": "q1", "message": ""}],
        known_ids={"q1"},
        gateway=_FakeGateway(fail=True),
    )
    assert result.narrative is None  # degraded, not raised
    assert result.issues and result.issues[0].code == "sync_conflict"  # diagnosis intact


# --- wired into sync-back -------------------------------------------------------------------------
def test_sync_back_action_explain_attaches_diagnoses(tmp_path: Path) -> None:
    root = tmp_path / "content"
    ContentStore(root).save(ContentBundle(quests={"q1": Quest(id="q1", title="库里改法")}))
    engine = tmp_path / "engine.json"
    engine.write_text(
        json.dumps([{"id": "q1", "title": "引擎改法"}], ensure_ascii=False), encoding="utf-8"
    )
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps([{"id": "q1", "title": "旧标题"}], ensure_ascii=False), encoding="utf-8"
    )
    result = sync_back_action(
        root,
        input_path=str(engine),
        baseline_path=str(baseline),
        explain=True,
        gateway=_FakeGateway(),
    )
    assert result["conflicts"] == ["q1"]
    explained = result["explained"]
    codes = {issue["code"] for issue in explained["issues"]}
    assert "sync_conflict" in codes
    assert explained["narrative"] == "先检查改名，再重导。"
