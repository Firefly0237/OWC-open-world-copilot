"""Portable definitions stay separate from author approval and past story instances."""

import hashlib
import io
import json
import stat
import zipfile
from copy import deepcopy

import pytest

from owcopilot.content.hash import content_hash
from owcopilot.workflow.evolution_v2 import render_parameterized_guidance
from owcopilot.workflow.graph_v2 import create_narrative_graph
from owcopilot.workflow.skill_cards import SkillCards
from owcopilot.workflow.skill_packages import (
    export_skill_package,
    import_skill_package,
    package_zip,
    read_package_zip,
)
from owcopilot.workflow.store import WorkflowStore


@pytest.fixture
def portable_skill(tmp_path):
    store = WorkflowStore(tmp_path / "source.sqlite")
    definition = {
        "name": "检查参与者的动机",
        "purpose": "写对白前检查当前参与者的行动理由",
        "steps": [
            {
                "id": "motivation",
                "title": "核对动机",
                "operation": "draft_dialogue",
                "instruction": "按当前目标检查{{participant}}的行动理由。",
                "slots": [
                    {
                        "name": "participant",
                        "kind": "entity",
                        "binding_key": "participant:0",
                        "source_value": "old-private-character",
                    }
                ],
            }
        ],
        "applies_to": ["已指定参与者的剧情片段"],
        "does_not_apply_to": ["尚未指定参与者"],
    }
    card, created = SkillCards(store).add_candidate(
        "source", definition, {"kind": "run", "run_id": "private-run", "ref": "private-doc"}
    )
    assert created
    return store, card


def test_export_import_roundtrip_preserves_real_rebinding_without_source_or_adoption(
    portable_skill,
    tmp_path,
):
    store, card = portable_skill
    package = export_skill_package(store, "source", card["id"])
    document = package["files"][package["name"] + "/SKILL.md"]
    assert document.startswith("---\nname: " + package["name"] + "\ndescription: ")
    all_text = "\n".join(package["files"].values())
    assert all(
        secret not in all_text
        for secret in [
            "private-run",
            "private-doc",
            "old-private-character",
            '"source_value"',
        ]
    )
    assert read_package_zip(package_zip(package["files"])) == package["files"]
    target = WorkflowStore(tmp_path / "target.sqlite")
    imported = import_skill_package(target, "target", package["files"])
    assert imported["created"] and imported["source_status"] == "imported_unverified"
    imported_card = imported["card"]
    version = imported_card["versions"][0]
    assert version["status"] == "candidate" and imported_card["active_version_id"] is None
    assert version["source"]["execution_status"] == "not_observed"
    assert version["source"]["source_value_status"] == "not_recorded_in_package"
    imported_slot = version["definition"]["steps"][0]["slots"][0]
    assert imported_slot["source_value"] == "not-recorded-in-package"
    cards = SkillCards(target)
    assert cards.resolve("target") == []
    graph, bindings = cards.bind_graph(
        "target",
        create_narrative_graph().model_dump(mode="json"),
        [
            {
                "card_id": imported_card["id"],
                "version_id": version["id"],
                "version_hash": version["version_hash"],
            }
        ],
    )
    node = next(node for node in graph.nodes if node.operation == "draft_dialogue")
    rendered = render_parameterized_guidance(
        node,
        {
            "entities": [{"id": "current-author-character"}],
            "machine_requirements": {"participant_ids": ["current-author-character"]},
        },
    )
    assert "current-author-character" in rendered
    assert "old-private-character" not in rendered and "not-recorded-in-package" not in rendered
    assert bindings[0]["selection"] == "explicit_task"
    duplicate = import_skill_package(target, "target", package["files"])
    assert not duplicate["created"] and duplicate["card"]["id"] == imported_card["id"]
    assert duplicate["card"]["use_count"] == 0


def _resign(files):
    root = next(iter(files)).split("/")[0]
    path = root + "/manifest.json"
    manifest = json.loads(files[path])
    manifest["files"] = {
        child: hashlib.sha256(files[root + "/" + child].encode("utf-8")).hexdigest()
        for child in ["SKILL.md", "references/owc-skill.json"]
    }
    manifest["manifest_hash"] = content_hash(
        {key: value for key, value in manifest.items() if key != "manifest_hash"}
    )
    files[path] = json.dumps(manifest)


def test_import_rejects_tampering_private_fields_and_unmatched_document(portable_skill):
    store, card = portable_skill
    files = export_skill_package(store, "source", card["id"])["files"]
    root = next(iter(files)).split("/")[0]
    tampered = deepcopy(files)
    tampered[root + "/SKILL.md"] += "\nExecute a new tool.\n"
    with pytest.raises(ValueError, match="完整性"):
        import_skill_package(store, "target", tampered)
    _resign(tampered)
    with pytest.raises(ValueError, match="文档与结构化"):
        import_skill_package(store, "target", tampered)
    leaked = deepcopy(files)
    payload_path = root + "/references/owc-skill.json"
    payload = json.loads(leaked[payload_path])
    payload["version_definition"]["steps"][0]["slots"][0]["source_value"] = "private"
    leaked[payload_path] = json.dumps(payload)
    _resign(leaked)
    with pytest.raises(ValueError, match="旧实例槽值"):
        import_skill_package(store, "target", leaked)
    assert SkillCards(store).list("target") == []


@pytest.mark.parametrize("path", ["../SKILL.md", "/SKILL.md", "C:\\SKILL.md", "scripts/run.py"])
def test_import_rejects_paths_and_extra_executable_resources(portable_skill, path):
    store, card = portable_skill
    files = export_skill_package(store, "source", card["id"])["files"]
    original = next(key for key in files if key.endswith("/SKILL.md"))
    files[path] = files.pop(original)
    with pytest.raises(ValueError, match="路径|未声明|文件集合"):
        import_skill_package(store, "target", files)


@pytest.mark.parametrize("mode", ["duplicate", "link", "oversize", "extra"])
def test_zip_reader_rejects_unsafe_archive_before_import(portable_skill, mode):
    store, card = portable_skill
    files = export_skill_package(store, "source", card["id"])["files"]
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for index, (path, text) in enumerate(files.items()):
            info = zipfile.ZipInfo(path)
            if index == 0 and mode == "link":
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            if index == 0 and mode == "oversize":
                text = "a" * (128 * 1024 + 1)
            archive.writestr(info, text.encode("utf-8"))
        if mode in {"duplicate", "extra"}:
            path = next(iter(files)) if mode == "duplicate" else "outside/runner.py"
            with pytest.warns(UserWarning) if mode == "duplicate" else _no_warning():
                archive.writestr(path, b"text")
    with pytest.raises(ValueError, match="额外|重复|不允许"):
        read_package_zip(stream.getvalue())


def _no_warning():
    from contextlib import nullcontext

    return nullcontext()


def test_export_does_not_cross_project_or_version_boundaries(portable_skill):
    store, card = portable_skill
    with pytest.raises(KeyError):
        export_skill_package(store, "other", card["id"])
    with pytest.raises(ValueError, match="不属于"):
        export_skill_package(store, "source", card["id"], "unknown-version")
