"""Reference-library lifecycle: physical delete + binary upload (N2.3 / B6).

Reference material is not canon, so removal is a straight file delete (no tombstone) — but if an
already-adopted canon object's ``inspiration_refs`` still cites one of the source's chunks, the
caller gets a non-blocking warning naming which object now has a stale citation. Structured binary
upload uses formal intake so immutable artifact revisions and source locators survive into RAG.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from owcopilot.app.actions import (
    add_reference_action,
    list_references_action,
    remove_reference_action,
    search_references_action,
)
from owcopilot.content.models import ContentBundle, Entity, EntityType
from owcopilot.content.store import ContentStore
from owcopilot.inspiration.store import ReferenceStore, decode_reference_bytes

_DOCX_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_PACKAGE_RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_OFFICE_RELS_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "content"
    ContentStore(root).save(ContentBundle())
    return root


def _minimal_docx(paragraphs: list[str]) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    document = (
        f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{_DOCX_NS}"><w:body>'
        f"{body}</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            f'<Types xmlns="{_CONTENT_TYPES_NS}">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            f'<Relationships xmlns="{_PACKAGE_RELS_NS}">'
            f'<Relationship Id="rId1" Type="{_OFFICE_RELS_NS}/officeDocument" '
            'Target="word/document.xml"/>'
            "</Relationships>",
        )
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


# --- ReferenceStore.remove --------------------------------------------------------------------


def test_store_remove_deletes_the_metadata_and_raw_text_files(tmp_path: Path) -> None:
    root = _project(tmp_path)
    store = ReferenceStore(root)
    added = store.add_text(title="盐道笔记", text="舰队与商会争夺盐道控制权。")

    meta_path = store.sources_dir / f"{added.source.id}.json"
    raw_path = store.raw_dir / f"{added.source.id}.txt"
    assert meta_path.exists() and raw_path.exists()

    store.remove(added.source.id)
    assert not meta_path.exists()
    assert not raw_path.exists()


def test_store_remove_unknown_id_raises_keyerror(tmp_path: Path) -> None:
    root = _project(tmp_path)
    store = ReferenceStore(root)
    with pytest.raises(KeyError):
        store.remove("ref_does_not_exist")


# --- remove_reference_action -------------------------------------------------------------------


def test_remove_reference_action_prunes_index_and_disk(tmp_path: Path) -> None:
    root = _project(tmp_path)
    added = add_reference_action(root, title="枯井残卷", text="枯叶林深处的枯井埋着一卷古书。")
    source_id = added["source"]["id"]
    assert list_references_action(root)["count"] == 1

    result = remove_reference_action(root, source_id=source_id)
    assert result["removed"] == source_id
    assert result["dangling_ref_warnings"] == []

    assert list_references_action(root)["count"] == 0
    hits = search_references_action(root, query="枯井 古书")["hits"]
    assert hits == []


def test_remove_reference_action_unknown_id_gives_guided_chinese_error(tmp_path: Path) -> None:
    root = _project(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        remove_reference_action(root, source_id="ref_ghost")
    message = str(excinfo.value)
    assert "不存在" in message
    assert "灵感库" in message


def test_remove_reference_action_warns_but_still_deletes_when_canon_cites_it(
    tmp_path: Path,
) -> None:
    """Deletion is non-blocking: a canon object's stale citation is a warning, not a refusal."""
    root = _project(tmp_path)
    added = add_reference_action(
        root, title="蒸汽巨树", text="边境群岛靠一棵蒸汽巨树供能，真相被三股势力封锁。"
    )
    source_id = added["source"]["id"]
    cited_chunk = added["chunks"][0]["id"]

    ContentStore(root).save(
        ContentBundle(
            entities={
                "npc_grounded": Entity(
                    id="npc_grounded",
                    name="接地角色",
                    type=EntityType.NPC,
                    description="由这份素材启发生成。",
                    metadata={"inspiration_refs": cited_chunk},
                )
            }
        )
    )

    result = remove_reference_action(root, source_id=source_id)
    assert result["removed"] == source_id
    assert result["dangling_ref_warnings"] == ["entity:npc_grounded"]
    # non-blocking: the source is gone regardless of the warning
    assert list_references_action(root)["count"] == 0


# --- decode_reference_bytes: real .docx round trip ----------------------------------------------


def test_decode_reference_bytes_reads_docx_paragraphs() -> None:
    data = _minimal_docx(["第一段：舰队与商会争夺盐道。", "第二段：谁掌握盐，谁就掌握王座。"])
    text = decode_reference_bytes(data, "盐道笔记.docx")
    assert "舰队与商会争夺盐道" in text
    assert "谁掌握盐" in text


def test_uploaded_docx_is_chunked_and_searchable_end_to_end(tmp_path: Path) -> None:
    """The full path a real upload takes: bytes -> decode_reference_bytes -> add_reference_action
    -> indexed -> searchable. decode_reference_bytes previously had zero callers in the repo."""
    root = _project(tmp_path)
    data = _minimal_docx(
        ["雾铃渡口的领航员替商会押运盐引。", "守夜人在潮汐钟响前与领航员交接账册。"]
    )
    text = decode_reference_bytes(data, "渡口纪事.docx")

    added = add_reference_action(
        root, title="渡口纪事", text=text, original_filename="渡口纪事.docx"
    )
    assert added["source"]["chunk_count"] >= 1
    assert added["indexed_count"] >= 1

    hits = search_references_action(root, query="领航员 盐引")["hits"]
    assert hits
    assert any("领航员" in hit["body"] for hit in hits)
