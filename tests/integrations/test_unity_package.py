"""Offline contract gate for the Unity UPM package.

Package path: ``integrations/unity/com.owcopilot.content-import``.

A Unity/dotnet toolchain is required to *compile and run* the C# (that is the honest live-run gate
kept outside this offline test). What is verifiable with no engine — and is
locked here
— is:

* the UPM package skeleton is canonical (``package.json`` name/version/unity/dependency, the
  Runtime/Core/Editor/Tests assembly split, editor-only platforms, the engine-free Core);
* every asset and folder ships a ``.meta`` with a unique 32-hex GUID (dropping/regenerating one
  silently breaks references in every consuming project — so this is a real regression risk);
* the checked-in C# test fixture is **byte-identical** to the current output of the real Python
  exporter (``export_content_bundle``). This is what guarantees the C# EditMode/off-engine tests run
  against genuine wire bytes rather than a hand-rolled shape, and it byte-regresses the contract the
  two sides share.

No business logic is duplicated: the fixture is produced by importing and running the production
exporter, and the manifest/asmdef facts are read straight off the shipped files.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from owcopilot.content.models import (
    Branch,
    ContentBundle,
    DialogueRef,
    Effect,
    Entity,
    EntityType,
    LogicVar,
    LogicVarType,
    Quest,
    QuestLogic,
    QuestStage,
    Relation,
    StageLogic,
)
from owcopilot.exporters import export_content_bundle

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PKG = _REPO_ROOT / "integrations" / "unity" / "com.owcopilot.content-import"
_FIXTURE = _PKG / "Tests" / "Editor" / "Fixtures" / "content_bundle.json"

_GUID_RE = re.compile(r"^[0-9a-f]{32}$")


def _fixture_bundle() -> ContentBundle:
    """The bundle behind the committed C# fixture. Deliberately mixes resolvable references with
    unresolved ones (``quest_missing`` / ``dlg_ghost`` / ``ent_ghost`` / relation source
    ``ghost_x``) so the C# resolver test can assert the honest 'kept for review' warnings, and
    includes a full QuestLogic so the QuestLogic SO mapping is exercised."""
    return ContentBundle(
        entities={
            "npc_mara": Entity(
                id="npc_mara", name="玛拉", type=EntityType.NPC, description="边境斥候。"
            ),
            "loc_town": Entity(id="loc_town", name="Town", type=EntityType.LOCATION),
            "fac_watch": Entity(id="fac_watch", name="守夜人", type=EntityType.FACTION),
        },
        quests={
            "quest_intro": Quest(id="quest_intro", title="序章"),
            "quest_patrol": Quest(
                id="quest_patrol",
                title="巡逻边境",
                giver_npc="npc_mara",
                location="loc_town",
                objective="在天黑前巡视边境线。",
                prerequisites=["quest_intro", "quest_missing"],  # quest_missing is unresolved
                dialogue_refs=["dlg_hello", "dlg_ghost"],  # dlg_ghost is unresolved
                timeline_order=1,
                stages=[
                    QuestStage(
                        id="s1",
                        summary="出发",
                        location="loc_town",
                        required_entities=["npc_mara", "ent_ghost"],  # ent_ghost is unresolved
                    )
                ],
                logic=QuestLogic(
                    variables=[
                        LogicVar(id="v_seen", name="seen", type=LogicVarType.BOOL, default=False)
                    ],
                    stage_logic=[
                        StageLogic(
                            stage_id="s1",
                            effects_on_complete=[Effect(var="v_seen", op="set", value=True)],
                        )
                    ],
                    branches=[
                        Branch(id="br1", from_stage="s1", condition="v_seen", outcome="done")
                    ],
                    unlocks=["quest_next"],
                ),
            ),
        },
        dialogues={
            "dlg_hello": DialogueRef(
                id="dlg_hello",
                text_key="dlg.hello",
                speaker_id="npc_mara",
                quest_id="quest_patrol",
                text="站住！",
                locale="zh-CN",
            )
        },
        relations=[
            Relation(source="npc_mara", target="loc_town", kind="resides_in"),
            Relation(source="npc_mara", target="fac_watch", kind="member_of"),
            Relation(source="ghost_x", target="loc_town", kind="haunts"),  # source unresolved
        ],
    )


# --- package skeleton + manifest ----------------------------------------------------------------


def test_package_manifest_is_canonical() -> None:
    manifest = json.loads((_PKG / "package.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "com.owcopilot.content-import"  # reverse-DNS, lowercase + hyphens
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])  # SemVer major.minor.patch
    assert re.fullmatch(r"\d+\.\d+", manifest["unity"])  # min editor "major.minor"
    # Newtonsoft is a declared dependency pinned to an exact version — never a vendored DLL.
    deps = manifest["dependencies"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", deps["com.unity.nuget.newtonsoft-json"])


def test_no_vendored_binaries_or_engine_junk() -> None:
    # A text-only package: no DLLs, no Unity project scaffolding committed.
    banned = {".dll", ".uasset", ".meta.meta"}
    for path in _PKG.rglob("*"):
        if path.is_file():
            assert path.suffix.lower() not in banned, f"unexpected binary/artifact: {path}"
    for junk in ("Library", "Temp", "obj", "Logs"):
        assert not (_PKG / junk).exists()


# --- assembly definition split ------------------------------------------------------------------


def _asmdef(relpath: str) -> dict:
    return json.loads((_PKG / relpath).read_text(encoding="utf-8"))


def test_runtime_asmdef_ships_in_builds() -> None:
    asm = _asmdef("Runtime/com.owcopilot.content-import.asmdef")
    assert asm["name"] == "com.owcopilot.content-import"
    assert asm["includePlatforms"] == []  # all platforms -> SO types ship in the game build
    assert asm["noEngineReferences"] is False


def test_core_asmdef_is_engine_free_for_offline_tests() -> None:
    asm = _asmdef("Core/com.owcopilot.content-import.Core.asmdef")
    # The load-bearing parse/resolve logic is UnityEngine-free so it is dotnet-testable off-engine.
    assert asm["noEngineReferences"] is True
    assert "Unity.Newtonsoft.Json" in asm["references"]


def test_editor_asmdef_is_editor_only_and_wired() -> None:
    asm = _asmdef("Editor/com.owcopilot.content-import.Editor.asmdef")
    assert asm["includePlatforms"] == ["Editor"]  # importer/window/postprocessor never ship
    refs = set(asm["references"])
    assert {
        "com.owcopilot.content-import",
        "com.owcopilot.content-import.Core",
        "Unity.Newtonsoft.Json",
    } <= refs


def test_tests_asmdef_is_editor_only_nunit() -> None:
    asm = _asmdef("Tests/Editor/com.owcopilot.content-import.Editor.Tests.asmdef")
    assert asm["includePlatforms"] == ["Editor"]
    assert "nunit.framework.dll" in asm["precompiledReferences"]
    assert asm["defineConstraints"] == ["UNITY_INCLUDE_TESTS"]
    refs = set(asm["references"])
    assert "com.owcopilot.content-import.Core" in refs
    assert "com.owcopilot.content-import.Editor" in refs


# --- .meta / GUID discipline --------------------------------------------------------------------


def _is_hidden(path: Path) -> bool:
    """Unity hides folders whose name ends with '~' (Documentation~/Samples~); their contents are
    NOT imported as assets, so they neither have nor need .meta files."""
    return any(part.endswith("~") for part in path.relative_to(_PKG).parts)


def test_every_asset_and_folder_has_a_meta() -> None:
    missing: list[str] = []
    for path in _PKG.rglob("*"):
        if _is_hidden(path):
            continue
        if path.is_file() and path.suffix == ".meta":
            continue
        if path.is_dir():
            # subfolders need a sibling <folder>.meta; the package root itself does not
            if path == _PKG:
                continue
            meta = path.with_name(path.name + ".meta")
        else:
            meta = path.with_name(path.name + ".meta")
        if not meta.exists():
            missing.append(str(meta.relative_to(_PKG)))
    assert not missing, f"missing .meta files: {missing}"


def test_meta_guids_are_valid_and_unique() -> None:
    guids: dict[str, str] = {}
    for meta in _PKG.rglob("*.meta"):
        if _is_hidden(meta):
            continue
        match = re.search(r"^guid:\s*([0-9a-fA-F]+)\s*$", meta.read_text(encoding="utf-8"), re.M)
        assert match, f"no guid in {meta.relative_to(_PKG)}"
        guid = match.group(1).lower()
        assert _GUID_RE.match(guid), f"malformed guid in {meta.relative_to(_PKG)}: {guid}"
        assert guid not in guids, (
            f"duplicate guid {guid} in {meta.relative_to(_PKG)} and {guids[guid]}"
        )
        guids[guid] = str(meta.relative_to(_PKG))


# --- fixture faithfulness (the byte-regression gate the C# tests depend on) ----------------------


def test_committed_fixture_matches_real_exporter_bytes(tmp_path: Path) -> None:
    export_content_bundle(_fixture_bundle(), tmp_path)
    produced = (tmp_path / "content_bundle.json").read_text(encoding="utf-8")
    committed = _FIXTURE.read_text(encoding="utf-8")
    assert committed == produced, (
        "Tests/Editor/Fixtures/content_bundle.json is stale vs the real exporter. "
        "Regenerate it from _fixture_bundle() (the C# tests run against these exact bytes)."
    )


def test_fixture_encodes_the_cross_language_invariants() -> None:
    """Anchor the counts the C# resolver test cross-asserts, so a drift on either side is caught."""
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert len(data["entities"]) == 3
    assert len(data["quests"]) == 2
    assert len(data["dialogues"]) == 1
    assert len(data["relations"]) == 3
    # the quest logic survived the round-trip into the fixture the SO layer must model
    logic = data["quests"]["quest_patrol"]["logic"]
    assert logic["variables"][0]["id"] == "v_seen"
    assert logic["stage_logic"][0]["effects_on_complete"][0]["value"] is True


if __name__ == "__main__":  # pragma: no cover - convenience for regenerating the fixture
    pytest.main([__file__, "-q"])
