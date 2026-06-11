"""Manuscript extraction: chunking, docx decode, offline pipeline, gaps, review landing."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from owcopilot.app.actions import (
    decide_review_action,
    fill_extraction_gaps_action,
    run_extraction_action,
    submit_extraction_action,
)
from owcopilot.content.models import ContentBundle
from owcopilot.content.store import ContentStore
from owcopilot.extraction import (
    ExtractionService,
    OfflineExtractionProvider,
    OfflineGapFillProvider,
    apply_gap_answers,
    chunk_text,
    decode_document_bytes,
    quests_from_beats,
)
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.router import StaticRouter

_MANUSCRIPT = (
    "沈青澜说道：雾隐城的灯不该是绿的。沈青澜与陆惊鸿前往枯叶林。\n\n"
    "角色：顾长风\n势力：天机阁\n陆惊鸿说：「玄武之钥」必须找回。"
)


def _gateway(provider, task: str) -> LLMGateway:
    return LLMGateway(providers={"cheap": provider}, router=StaticRouter(mapping={task: "cheap"}))


def _service() -> ExtractionService:
    return ExtractionService(
        gateway=_gateway(OfflineExtractionProvider(), "extract_lore"), bundle=ContentBundle()
    )


def test_chunk_text_splits_long_paragraphs_with_overlap() -> None:
    text = "甲" * 9000
    chunks = chunk_text(text, max_chars=3500, overlap_chars=200)
    assert len(chunks) >= 3
    assert all(len(chunk) <= 3500 for chunk in chunks)


def test_decode_document_bytes_reads_docx_paragraphs() -> None:
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    document = (
        f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{ns}"><w:body>'
        "<w:p><w:r><w:t>第一段：沈青澜抵达雾隐城。</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>第二段：灯是绿的。</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document)
    text = decode_document_bytes(buffer.getvalue(), "章节.docx")
    assert "第一段：沈青澜抵达雾隐城。" in text
    assert "第二段" in text


def test_offline_extraction_builds_entities_relations_beats_and_gaps() -> None:
    draft = _service().extract(title="第一章", text=_MANUSCRIPT)
    names = {entity.name for entity in draft.bundle.entities.values()}
    assert {"沈青澜", "顾长风", "天机阁"} <= names
    assert any(rel.kind == "认识" for rel in draft.bundle.relations)
    assert draft.plot_beats and draft.plot_beats[0].order == 1
    assert any(gap.field == "description" for gap in draft.gaps)
    assert draft.stats["entities"] == len(draft.bundle.entities)


def test_fill_gaps_then_apply_answers_resolves_descriptions() -> None:
    draft = _service().extract(title="第一章", text=_MANUSCRIPT)
    fill_service = ExtractionService(
        gateway=_gateway(OfflineGapFillProvider(), "extract_fill"), bundle=ContentBundle()
    )
    draft = fill_service.fill_gaps(draft)
    assert all(gap.suggestion for gap in draft.gaps)
    answers = {gap.ref: gap.suggestion for gap in draft.gaps}
    draft = apply_gap_answers(draft, answers)
    assert draft.gaps == []
    assert all(len(entity.description) >= 4 for entity in draft.bundle.entities.values())


def test_quests_from_beats_builds_timeline_ordered_skeletons() -> None:
    draft = _service().extract(title="第一章", text=_MANUSCRIPT)
    quests = quests_from_beats(draft)
    assert quests
    first = next(iter(quests.values()))
    assert first.timeline_order == 1
    assert first.localization_keys


def test_extraction_actions_round_trip_lands_via_review(tmp_path: Path) -> None:
    root = tmp_path / "content"
    ContentStore(root).save(ContentBundle())
    result = run_extraction_action(root, title="第一章", text=_MANUSCRIPT)
    # offline providers report pseudo-token estimates, so the budget is tiny but nonzero
    assert result["cost_budget"]["used_usd"] < 0.01
    assert result["cost_budget"]["over_budget"] is False
    filled = fill_extraction_gaps_action(root, draft=result["draft"])
    answers = {gap["ref"]: gap["suggestion"] for gap in filled["draft"]["gaps"]}
    submitted = submit_extraction_action(
        root, draft=filled["draft"], answers=answers, include_beats_as_quests=True
    )
    assert submitted["open_gaps"] == 0
    decided = decide_review_action(
        root, item_id=submitted["review_item_id"], decision="accepted", operator="tester"
    )
    assert decided["written_ref"].startswith("import_draft:")
    assert decided["post_audit_open_errors"] == 0
    reloaded = ContentStore(root).load()
    assert reloaded.entities and reloaded.quests
    assert all(e.review_status.value == "approved" for e in reloaded.entities.values())
