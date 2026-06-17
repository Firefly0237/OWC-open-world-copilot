"""Manuscript extraction: chunking, docx decode, offline pipeline, gaps, review landing."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

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
    parse_extraction_payload,
    plan_coverage,
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


def test_plan_coverage_short_doc_is_full_fine_granularity() -> None:
    chunks, report = plan_coverage(_MANUSCRIPT)
    assert report.granularity == "full"
    assert report.covered_chars == report.total_chars
    assert report.chunk_count == len(chunks)
    assert report.complete is True
    assert report.language == "中文"


def test_plan_coverage_long_doc_coarsens_but_covers_everything() -> None:
    # A doc whose fine-grained chunking exceeds the budget must still be covered 100% by
    # enlarging chunk size — never silently truncated.
    paragraphs = "\n\n".join(f"第{i}段：沈青澜在雾隐城走了很久很久。" * 30 for i in range(400))
    chunks, report = plan_coverage(paragraphs, budget_chunks=48, base_chunk_chars=3500)
    assert report.granularity == "coarsened"
    assert len(chunks) <= 48
    assert report.covered_chars == report.total_chars  # whole document covered
    assert report.chunk_chars > 3500  # granularity was coarsened to fit the budget
    assert report.complete is True


def test_plan_coverage_enormous_doc_reports_partial_honestly() -> None:
    # Larger than the entire budget (budget * max_chunk_chars): the head is read and the tail is
    # reported as uncovered rather than dropped in silence.
    huge = "甲" * 5_000_000
    chunks, report = plan_coverage(huge, budget_chunks=8, max_chunk_chars=16000)
    assert report.granularity == "partial"
    assert report.complete is False
    assert len(chunks) == 8
    assert 0 < report.covered_chars < report.total_chars
    assert "未在本次提炼" in report.note


def test_extraction_attaches_coverage_report() -> None:
    draft = _service().extract(title="第一章", text=_MANUSCRIPT)
    assert draft.coverage is not None
    assert draft.coverage.granularity == "full"
    assert draft.coverage.language == "中文"


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


class _HallucinatingProvider:
    """Extracts one name present in the source and one that is not (a model inference/invention)."""

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        import json

        payload = {
            "characters": [
                {"name": "林潮生", "description": "雾铃渡口的领航员"},
                {"name": "凭空捏造者", "description": "原文从未出现的名字"},
            ]
        }
        text = json.dumps(payload, ensure_ascii=False)
        return text, 10, 10


def test_parse_extraction_payload_tolerates_prose_and_fences() -> None:
    # Real models wrap JSON in prose or fences; a strict json.loads would crash the whole run.
    for raw in (
        'Here is the extraction: {"characters": [{"name": "X"}]}',
        '{"characters": [{"name": "X"}]} -- hope this helps!',
        '```json\n{"characters": [{"name": "X"}]}\n```',
    ):
        assert parse_extraction_payload(raw)["characters"][0]["name"] == "X"


class _ChunkFlakyProvider:
    """Returns valid JSON for every chunk except chunk 2, which never yields parseable JSON."""

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        if "[chunk 2/" in user:
            return "Sorry, I can't produce that.", 5, 5
        return '{"characters": [{"name": "沈青澜"}]}', 5, 5


def test_extraction_skips_unparseable_chunk_without_crashing() -> None:
    # One unparseable chunk must not abort the run or be dropped silently: the others land and
    # the failed chunk is reported in the coverage report.
    text = "\n\n".join(f"第{i}段：沈青澜在雾隐城走了很久。" * 50 for i in range(60))
    service = ExtractionService(
        gateway=_gateway(_ChunkFlakyProvider(), "extract_lore"), bundle=ContentBundle()
    )
    draft = service.extract(title="T", text=text)
    assert draft.bundle.entities  # other chunks still produced content
    assert draft.coverage is not None
    assert draft.coverage.failed_chunks == [2]
    assert "无法解析" in draft.coverage.note


class _GleanProvider:
    """First pass finds 沈青澜; a gleaning pass recovers the missed faction 苍狼帮."""

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        import json

        from owcopilot.extraction.service import EXTRACTION_GLEAN_MARKER

        if EXTRACTION_GLEAN_MARKER in system:
            if "苍狼帮" in user:  # already known -> nothing new, gleaning stops
                return json.dumps({"factions": []}, ensure_ascii=False), 5, 5
            return json.dumps({"factions": [{"name": "苍狼帮"}]}, ensure_ascii=False), 5, 5
        return json.dumps({"characters": [{"name": "沈青澜"}]}, ensure_ascii=False), 5, 5


def test_gleaning_recovers_entities_a_single_pass_missed() -> None:
    service = ExtractionService(
        gateway=_gateway(_GleanProvider(), "extract_lore"), bundle=ContentBundle()
    )
    with_glean = {
        e.name for e in service.extract(title="T", text="沈青澜守夜。").bundle.entities.values()
    }
    without = {
        e.name
        for e in service.extract(
            title="T", text="沈青澜守夜。", glean_rounds=0
        ).bundle.entities.values()
    }
    assert "苍狼帮" in with_glean and "苍狼帮" not in without


def test_decode_document_bytes_reads_epub_in_spine_order() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "META-INF/container.xml",
            "<container><rootfiles><rootfile "
            'full-path="OEBPS/content.opf"/></rootfiles></container>',
        )
        archive.writestr(
            "OEBPS/content.opf",
            '<package xmlns="http://www.idpf.org/2007/opf"><manifest>'
            '<item id="c1" href="c1.xhtml"/><item id="c2" href="c2.xhtml"/></manifest>'
            '<spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>',
        )
        archive.writestr("OEBPS/c1.xhtml", "<html><body><p>第一章：沈青澜</p></body></html>")
        archive.writestr("OEBPS/c2.xhtml", "<html><body><p>第二章：陆惊鸿</p></body></html>")
    text = decode_document_bytes(buffer.getvalue(), "book.epub")
    assert "沈青澜" in text and "陆惊鸿" in text
    assert text.find("第一章") < text.find("第二章")


def test_decode_document_bytes_reads_pdf_text() -> None:
    canvas = pytest.importorskip("reportlab.pdfgen.canvas")
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.drawString(72, 720, "Aldric the caravan master guards Northwatch")
    pdf.showPage()
    pdf.save()
    text = decode_document_bytes(buffer.getvalue(), "novel.pdf")
    assert "Aldric" in text and "Northwatch" in text


def test_corrupt_binary_documents_raise_cleanly() -> None:
    for filename in ("x.pdf", "x.epub", "x.docx"):
        with pytest.raises(ValueError):
            decode_document_bytes(b"this is not a real binary document", filename)


class _AliasAndRelationProvider:
    """Emits the same person under two names (linked by an alias) plus a described relation."""

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        import json

        payload = {
            "characters": [
                {"name": "李白", "description": "唐代诗人，诗仙", "aliases": ["李太白"]},
                {"name": "李太白", "description": "字太白"},
                {"name": "高力士", "description": "宦官"},
            ],
            "relations": [
                {"source": "李太白", "target": "高力士", "kind": "敌对", "description": "脱靴结怨"}
            ],
        }
        return json.dumps(payload, ensure_ascii=False), 5, 5


def test_resolve_aliases_merges_same_person_and_remaps_relations() -> None:
    service = ExtractionService(
        gateway=_gateway(_AliasAndRelationProvider(), "extract_lore"), bundle=ContentBundle()
    )
    draft = service.extract(title="T", text="李白与高力士的故事。", glean_rounds=0)
    names = {e.name for e in draft.bundle.entities.values()}
    assert names == {"李白", "高力士"}  # 李太白 merged into 李白, not a second entity
    libai = next(e for e in draft.bundle.entities.values() if e.name == "李白")
    assert "李太白" in libai.aliases
    # the relation named the merged-away alias; it must be remapped onto the surviving entity
    assert len(draft.bundle.relations) == 1
    relation = draft.bundle.relations[0]
    assert relation.source == libai.id
    assert relation.metadata.get("description") == "脱靴结怨"


def test_faithfulness_flags_names_absent_from_source() -> None:
    """An extracted name that appears nowhere in the source is flagged unsupported (for human
    verification) rather than silently trusted — the round-26 'surface, don't mask' discipline."""
    service = ExtractionService(
        gateway=_gateway(_HallucinatingProvider(), "extract_lore"), bundle=ContentBundle()
    )
    draft = service.extract(title="测试", text="林潮生独自站在雾铃渡口，听着错乱的钟声。")
    flagged = {item.name for item in draft.unsupported}
    assert "凭空捏造者" in flagged  # not in the source → flagged
    assert "林潮生" not in flagged  # present in the source → trusted
    assert draft.stats["unsupported"] == 1
    invented = next(e for e in draft.bundle.entities.values() if e.name == "凭空捏造者")
    assert invented.metadata.get("unsupported_in_source") is True


def _bundle_with_relation(kind: str = "member_of") -> ContentBundle:
    from owcopilot.content.models import Entity, EntityType, Relation

    bundle = ContentBundle()
    bundle.entities["a"] = Entity(id="a", name="林潮生", type=EntityType.NPC)
    bundle.entities["b"] = Entity(id="b", name="苍狼帮", type=EntityType.FACTION)
    bundle.relations.append(Relation(source="a", target="b", kind=kind))
    return bundle


def test_faithfulness_flags_relation_not_co_occurring_in_source() -> None:
    """Two real entities the manuscript never mentions together = an invented link. The old
    name-only check waved this through (both names present); the relation tier now catches it."""
    from owcopilot.extraction.faithfulness import check_deterministic

    bundle = _bundle_with_relation()
    source = "林潮生独自站在雾铃渡口。" + "一些无关的描写。" * 60 + "苍狼帮在很远的北方崛起。"
    flagged = {item.ref: item for item in check_deterministic(bundle, source)}
    assert "relation:a|member_of|b" in flagged
    assert flagged["relation:a|member_of|b"].reason == "relation_not_in_source"


def test_faithfulness_keeps_relation_when_endpoints_co_occur() -> None:
    from owcopilot.extraction.faithfulness import check_deterministic

    bundle = _bundle_with_relation()
    flagged = {item.ref for item in check_deterministic(bundle, "林潮生本是苍狼帮的人。")}
    assert "relation:a|member_of|b" not in flagged


class _VerdictProvider:
    def __init__(self, supported: bool) -> None:
        self.supported = supported

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        import json

        body = json.dumps(
            {"verdicts": [{"ref": "relation:a|member_of|b", "supported": self.supported}]}
        )
        return (body, 10, 10)


class _GarbageVerdictProvider:
    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        return ("这不是 JSON", 5, 5)


def test_llm_entailment_flags_unsupported_relation() -> None:
    """The RAGAS-style entailment tier flags a relation the judge rules unsupported by source."""
    from owcopilot.extraction.faithfulness import llm_unsupported

    bundle = _bundle_with_relation()
    gateway = _gateway(_VerdictProvider(supported=False), "verify_faithfulness")
    extra = llm_unsupported(bundle, "林潮生本是苍狼帮的人。", gateway, already_flagged=set())
    assert len(extra) == 1
    assert extra[0].ref == "relation:a|member_of|b"
    assert extra[0].reason == "relation_contradicted"
    assert extra[0].source_check == "llm"


def test_llm_entailment_supported_relation_not_flagged() -> None:
    from owcopilot.extraction.faithfulness import llm_unsupported

    bundle = _bundle_with_relation()
    gateway = _gateway(_VerdictProvider(supported=True), "verify_faithfulness")
    assert llm_unsupported(bundle, "林潮生本是苍狼帮的人。", gateway, already_flagged=set()) == []


def test_llm_entailment_parse_failure_fabricates_nothing() -> None:
    """A broken judge reply must never invent an 'unsupported' flag against grounded content."""
    from owcopilot.extraction.faithfulness import llm_unsupported

    bundle = _bundle_with_relation()
    gateway = _gateway(_GarbageVerdictProvider(), "verify_faithfulness")
    assert llm_unsupported(bundle, "林潮生本是苍狼帮的人。", gateway, already_flagged=set()) == []
