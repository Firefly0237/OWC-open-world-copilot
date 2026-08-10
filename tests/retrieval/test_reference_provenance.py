"""Rights declaration at the intake doors (B2 / N2.2).

Extraction produces canon drafts, so it only takes material the project owns or has licensed;
third-party books belong in the inspiration library, which takes all three provenances because
it is physically separate from canon.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from owcopilot.app.actions import (
    add_reference_action,
    list_references_action,
    run_extraction_action,
)
from owcopilot.cli.main import main
from owcopilot.content.models import ContentBundle
from owcopilot.content.store import ContentStore
from owcopilot.inspiration.models import Provenance, ensure_canon_intake_allowed, parse_provenance
from owcopilot.inspiration.store import ReferenceStore

_MANUSCRIPT = (
    "雾铃渡口的领航员林潮生替商会押运盐引。"
    "他与守夜人苏窈约定在潮汐钟响前交接账册，否则渡口的封锁令就会落下。"
)


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "content"
    ContentStore(root).save(ContentBundle())
    return root


def test_reference_source_written_before_this_field_reads_back_as_third_party(
    tmp_path: Path,
) -> None:
    """A sources/*.json from before provenance existed must still load — as the safest value."""
    root = _project(tmp_path)
    store = ReferenceStore(root)
    store.sources_dir.mkdir(parents=True, exist_ok=True)
    legacy = {
        "id": "ref_legacy_0000",
        "title": "旧素材",
        "source_type": "uploaded_file",
        "original_filename": None,
        "allowed_uses": ["inspiration"],
        "text_hash": "0" * 64,
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    (store.sources_dir / "ref_legacy_0000.json").write_text(
        json.dumps(legacy, ensure_ascii=False), encoding="utf-8"
    )

    loaded = store.list_sources()
    assert [source.id for source in loaded] == ["ref_legacy_0000"]
    assert loaded[0].provenance is Provenance.THIRD_PARTY_REFERENCE


def test_inspiration_library_accepts_all_three_provenances(tmp_path: Path) -> None:
    root = _project(tmp_path)
    for index, provenance in enumerate(Provenance):
        add_reference_action(
            root,
            title=f"素材{index}",
            text=f"第{index}份参考材料，讲的是盐道与商会。",
            provenance=provenance.value,
        )

    listed = list_references_action(root)["sources"]
    assert {source["provenance"] for source in listed} == {item.value for item in Provenance}


def test_library_default_is_the_most_conservative_provenance(tmp_path: Path) -> None:
    root = _project(tmp_path)
    added = add_reference_action(root, title="没声明归属", text="一段没有声明归属的参考文本。")
    assert added["source"]["provenance"] == Provenance.THIRD_PARTY_REFERENCE.value


def test_extraction_refuses_third_party_material_with_guidance(tmp_path: Path) -> None:
    root = _project(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        run_extraction_action(
            root,
            title="别人的小说",
            text=_MANUSCRIPT,
            provenance=Provenance.THIRD_PARTY_REFERENCE.value,
        )
    message = str(excinfo.value)
    assert "素材库" in message
    assert "灵感库" in message
    assert "own_ip" in message


def test_extraction_accepts_owned_and_licensed_material(tmp_path: Path) -> None:
    for provenance in (Provenance.OWN_IP, Provenance.LICENSED):
        root = _project(tmp_path / provenance.value)
        result = run_extraction_action(
            root, title="第一章", text=_MANUSCRIPT, provenance=provenance.value
        )
        assert result["draft"]["bundle"]["entities"]
        assert result["provenance"] == provenance.value


def test_extraction_default_keeps_existing_callers_working(tmp_path: Path) -> None:
    """Every caller that predates this field was implicitly claiming its own material."""
    root = _project(tmp_path)
    result = run_extraction_action(root, title="第一章", text=_MANUSCRIPT)
    assert result["provenance"] == Provenance.OWN_IP.value


def test_unknown_provenance_is_refused_with_the_vocabulary(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as excinfo:
        parse_provenance("public_domain")
    message = str(excinfo.value)
    assert "未知的来源归属" in message
    assert "own_ip" in message and "licensed" in message


def test_ensure_canon_intake_allowed_returns_the_parsed_value() -> None:
    assert ensure_canon_intake_allowed("licensed") is Provenance.LICENSED
    assert ensure_canon_intake_allowed(Provenance.OWN_IP) is Provenance.OWN_IP


def test_cli_extract_refuses_third_party(tmp_path: Path, capsys) -> None:
    root = _project(tmp_path)
    manuscript = tmp_path / "第一章.txt"
    manuscript.write_text(_MANUSCRIPT, encoding="utf-8")
    argv = [
        "extract",
        "--content-root",
        str(root),
        "--input",
        str(manuscript),
        "--provenance",
        "third_party_reference",
    ]

    assert main(argv) == 2
    error = json.loads(capsys.readouterr().err)
    assert error["type"] == "ValueError"
    assert "灵感库" in error["error"]


def test_cli_extract_reports_the_accepted_declaration(tmp_path: Path, capsys) -> None:
    root = _project(tmp_path)
    manuscript = tmp_path / "第一章.txt"
    manuscript.write_text(_MANUSCRIPT, encoding="utf-8")

    assert main(["extract", "--content-root", str(root), "--input", str(manuscript)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["provenance"] == Provenance.OWN_IP.value
