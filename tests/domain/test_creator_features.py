"""Creator-facing round: templates, recents, prose check, lorebook, key probe."""

from __future__ import annotations

import zipfile
from pathlib import Path

from owcopilot.app.actions import (
    probe_llm_connection_action,
    run_lorebook_export_action,
    run_prose_check_action,
)
from owcopilot.app.genesis_templates import GENESIS_TEMPLATES
from owcopilot.app.workspaces import load_recent_workspaces, remember_workspace
from owcopilot.assist.prose_check import check_prose
from owcopilot.content.models import ContentBundle, Entity, EntityType, Term
from owcopilot.content.store import ContentStore
from owcopilot.exporters.lorebook import render_lorebook_markdown, write_lorebook


def _bundle() -> ContentBundle:
    return ContentBundle(
        entities={
            "npc_shen": Entity(
                id="npc_shen",
                name="沈青澜",
                type=EntityType.NPC,
                description="边军斥候出身的调查员。",
                aliases=["青澜"],
            ),
            "loc_wuyin": Entity(
                id="loc_wuyin", name="雾隐城", type=EntityType.LOCATION, description="港口城。"
            ),
        },
        terms={
            "term_key": Term(
                id="term_key",
                canonical="玄武之钥",
                forbidden=["玄武钥匙"],
                description="开启旧城门的信物。",
            )
        },
    )


# ------------------------------------------------------------------ genesis templates
def test_genesis_templates_have_complete_fields() -> None:
    assert len(GENESIS_TEMPLATES) >= 5
    for name, template in GENESIS_TEMPLATES.items():
        assert template["idea"], name
        assert isinstance(template["world_styles"], list) and template["world_styles"]
        for key in ("game_genre", "tone", "era", "player_fantasy", "core_conflict"):
            assert isinstance(template[key], str) and template[key], f"{name}.{key}"


# ------------------------------------------------------------------ recent workspaces
def test_remember_workspace_dedupes_and_caps(tmp_path: Path) -> None:
    store = tmp_path / "recent.json"
    for index in range(10):
        remember_workspace(tmp_path / f"w{index}", path=store, limit=8)
    recent = load_recent_workspaces(store)
    assert len(recent) == 8
    assert recent[0].endswith("w9")
    remember_workspace(tmp_path / "w5", path=store, limit=8)
    recent = load_recent_workspaces(store)
    assert recent[0].endswith("w5")
    assert len(recent) == 8


def test_load_recent_workspaces_survives_corrupt_file(tmp_path: Path) -> None:
    store = tmp_path / "recent.json"
    store.write_text("{not json", encoding="utf-8")
    assert load_recent_workspaces(store) == []


# ------------------------------------------------------------------ prose check
def test_prose_check_resolves_known_and_flags_planted_errors() -> None:
    text = (
        "沈青澜来到雾隐城，低声道：「玄武之钥」不该出现在这里。陌生人柳无衣说：把玄武钥匙交出来。"
    )
    report = check_prose(text, _bundle())
    refs = {m.ref for m in report.resolved_mentions}
    assert {"entity:npc_shen", "entity:loc_wuyin", "term:term_key"} <= refs
    kinds = {(i.kind, i.message) for i in report.issues}
    assert any(kind == "forbidden_term" for kind, _ in kinds)
    assert any(kind == "unknown_mention" and "柳无衣" in msg for kind, msg in kinds)
    forbidden = next(i for i in report.issues if i.kind == "forbidden_term")
    assert "玄武之钥" in forbidden.suggestion


def test_prose_check_clean_text_reports_no_issues() -> None:
    report = check_prose("沈青澜检查了雾隐城的城门。", _bundle())
    assert report.issues == []
    assert report.stats["resolved_mentions"] >= 2


def test_prose_check_action_round_trip(tmp_path: Path) -> None:
    root = tmp_path / "content"
    ContentStore(root).save(_bundle())
    result = run_prose_check_action(root, text="沈青澜遇到了不认识的洛千山说。")
    assert result["stats"]["issues"] >= 1
    assert result["cost_budget"]["used_usd"] == 0.0


# ------------------------------------------------------------------ lorebook export
def test_lorebook_markdown_renders_all_sections() -> None:
    markdown = render_lorebook_markdown(_bundle(), title="测试设定集")
    assert markdown.startswith("# 测试设定集")
    for heading in ("## 角色", "## 地点", "## 术语表"):
        assert heading in markdown
    assert "沈青澜（青澜）" in markdown
    assert "玄武之钥" in markdown and "禁用：玄武钥匙" in markdown


def test_write_lorebook_emits_md_and_valid_docx(tmp_path: Path) -> None:
    files = write_lorebook(_bundle(), tmp_path, formats=("md", "docx"))
    kinds = {row["kind"] for row in files}
    assert kinds == {"lorebook_markdown", "lorebook_docx"}
    assert all(len(row["sha256"]) == 64 for row in files)
    with zipfile.ZipFile(tmp_path / "lorebook.docx") as archive:
        names = set(archive.namelist())
        assert {"[Content_Types].xml", "_rels/.rels", "word/document.xml"} <= names
        document = archive.read("word/document.xml").decode("utf-8")
        assert "沈青澜" in document


def test_lorebook_export_action(tmp_path: Path) -> None:
    root = tmp_path / "content"
    ContentStore(root).save(_bundle())
    result = run_lorebook_export_action(root, output_dir=tmp_path / "out", formats=("md",))
    assert [row["kind"] for row in result["files"]] == ["lorebook_markdown"]
    assert (tmp_path / "out" / "lorebook.md").exists()


# ------------------------------------------------------------------ key probe
class _FakeProbeProvider:
    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        return "pong", 4, 1


class _ExplodingProvider:
    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        raise RuntimeError("401 unauthorized")


def test_llm_connection_probe_ok_with_injected_provider(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://old.example")
    result = probe_llm_connection_action(
        base_url="https://api.example.com",
        api_key="sk-test",
        model="demo-model",
        provider=_FakeProbeProvider(),
    )
    assert result["ok"] is True
    assert result["sample"].startswith("pong")
    # env restored afterwards
    import os

    assert os.environ["OPENAI_BASE_URL"] == "https://old.example"


def test_llm_connection_probe_classifies_failure() -> None:
    result = probe_llm_connection_action(
        base_url="https://api.example.com",
        api_key="sk-bad",
        model="demo-model",
        provider=_ExplodingProvider(),
    )
    assert result["ok"] is False
    assert result["category"] == "auth"
