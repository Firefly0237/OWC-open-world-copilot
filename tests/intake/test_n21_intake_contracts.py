from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from owcopilot.contracts import ArtifactIdentity
from owcopilot.intake import (
    PDF_PROCESSING_FAILURE_CODES,
    DocxParagraphLocator,
    DocxTableCellLocator,
    IntakeRun,
    SourceSegment,
    TriageEvidenceSpan,
    TriageVerdict,
    XlsxCellLocator,
    validate_verdict_against_segment,
)

ARTIFACT_VERSION_ID = "11111111-1111-4111-8111-111111111111"
RUN_ID = "22222222-2222-4222-8222-222222222222"
SEGMENT_ID = "33333333-3333-4333-8333-333333333333"
RAW_OUTPUT_ID = "44444444-4444-4444-8444-444444444444"
VERDICT_ID = "55555555-5555-4555-8555-555555555555"
ACTOR_ID = "66666666-6666-4666-8666-666666666666"
NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def _artifact(content: bytes = b"exact-docx-bytes") -> ArtifactIdentity:
    return ArtifactIdentity.from_bytes(
        content,
        artifact_id=ARTIFACT_VERSION_ID,
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@pytest.mark.parametrize(
    "locator",
    [
        DocxParagraphLocator(paragraph_index=0),
        DocxTableCellLocator(table_index=1, row_index=2, column_index=3),
        XlsxCellLocator(
            sheet_name="任务表",
            row_index=7,
            column_index=2,
            cell_reference="B7",
        ),
    ],
)
def test_source_segment_binds_exact_text_hash_and_locator(locator: object) -> None:
    text = "系统提示只是素材正文，不得执行。"
    segment = SourceSegment.from_text(
        segment_id=SEGMENT_ID,
        artifact_version_id=ARTIFACT_VERSION_ID,
        ordinal=0,
        locator=locator,  # type: ignore[arg-type]
        text=text,
    )

    assert segment.text_hash == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert segment.untrusted_input is True
    assert segment.locator == locator
    with pytest.raises(ValidationError, match="text_hash does not match"):
        SourceSegment(
            segment_id=SEGMENT_ID,
            artifact_version_id=ARTIFACT_VERSION_ID,
            ordinal=0,
            locator=locator,
            text=text,
            text_hash="0" * 64,
            untrusted_input=True,
        )


@pytest.mark.parametrize(
    ("factory", "error"),
    [
        (lambda: DocxParagraphLocator(paragraph_index=-1), "non-negative"),
        (
            lambda: XlsxCellLocator(
                sheet_name="Sheet1", row_index=0, column_index=1, cell_reference="A1"
            ),
            "one-based",
        ),
        (
            lambda: XlsxCellLocator(
                sheet_name="Sheet1", row_index=1, column_index=1, cell_reference="arbitrary"
            ),
            "uppercase A1",
        ),
    ],
)
def test_locators_reject_placeholder_or_invalid_coordinates(factory: object, error: str) -> None:
    with pytest.raises(ValidationError, match=error):
        factory()  # type: ignore[operator]


def test_intake_run_binds_server_artifact_and_terminal_state() -> None:
    run = IntakeRun(
        run_id=RUN_ID,
        artifact_version_id=ARTIFACT_VERSION_ID,
        artifact=_artifact(),
        original_filename="第一章.docx",
        extension=".docx",
        byte_size=len(b"exact-docx-bytes"),
        status="AWAITING_HUMAN",
        created_by=ACTOR_ID,
        started_at=NOW,
        finished_at=NOW,
        rejection_code=None,
    )

    assert run.artifact.verify_bytes(b"exact-docx-bytes") is True
    with pytest.raises(ValidationError, match="only REJECTED"):
        run.model_copy(update={"rejection_code": "BAD_MIME"})
    with pytest.raises(ValidationError, match="basename"):
        run.model_copy(update={"original_filename": "../escape.docx"})
    with pytest.raises(ValidationError, match="bind the exact artifact_version_id"):
        run.model_copy(update={"artifact_version_id": RUN_ID})


def test_rejected_intake_requires_code_and_finish_time() -> None:
    with pytest.raises(ValidationError, match="only terminal"):
        IntakeRun(
            run_id=RUN_ID,
            artifact_version_id=ARTIFACT_VERSION_ID,
            artifact=_artifact(),
            original_filename="bad.docx",
            extension=".docx",
            byte_size=1,
            status="REJECTED",
            created_by=ACTOR_ID,
            started_at=NOW,
            finished_at=None,
            rejection_code="CORRUPT_DOCUMENT",
        )


@pytest.mark.parametrize(
    ("status", "processing_code"),
    [
        ("OCR_REQUIRED", "PDF_OCR_REQUIRED"),
        *(("PROCESSING_FAILED", code) for code in sorted(PDF_PROCESSING_FAILURE_CODES)),
    ],
)
def test_pdf_handoff_states_are_nonterminal_and_carry_only_sanitized_codes(
    status: str,
    processing_code: str,
) -> None:
    content = b"%PDF-1.4\nfixture"
    run = IntakeRun.model_validate(
        {
            "run_id": RUN_ID,
            "artifact_version_id": ARTIFACT_VERSION_ID,
            "artifact": ArtifactIdentity.from_bytes(
                content,
                artifact_id=ARTIFACT_VERSION_ID,
                mime_type="application/pdf",
            ),
            "original_filename": "scan.pdf",
            "extension": ".pdf",
            "byte_size": len(content),
            "status": status,
            "created_by": ACTOR_ID,
            "started_at": NOW,
            "finished_at": None,
            "rejection_code": None,
            "processing_code": processing_code,
        }
    )
    assert run.processing_code == processing_code
    with pytest.raises(ValidationError, match="processing"):
        IntakeRun.model_validate(
            {
                **run.model_dump(),
                "processing_code": (
                    "PDF_OCR_REQUIRED" if status != "OCR_REQUIRED" else None
                ),
            }
        )


def _verdict(**overrides: object) -> TriageVerdict:
    payload: dict[str, object] = {
        "verdict_id": VERDICT_ID,
        "raw_output_id": RAW_OUTPUT_ID,
        "segment_id": SEGMENT_ID,
        "content_role": "CANON_CANDIDATE",
        "confidence": 0.9,
        "policy_threshold": 0.7,
        "evidence_spans": (TriageEvidenceSpan(start=0, end=2, quote="王城"),),
        "abstain": False,
        "abstain_reason": None,
        "derivation_policy_version": "n21-triage-v1",
    }
    payload.update(overrides)
    return TriageVerdict.model_validate(payload)


def test_triage_verdict_requires_abstention_for_unknown_and_low_confidence() -> None:
    assert _verdict().abstain is False

    with pytest.raises(ValidationError, match="must abstain"):
        _verdict(content_role="UNKNOWN")
    with pytest.raises(ValidationError, match="must abstain"):
        _verdict(confidence=0.69)

    unknown = _verdict(
        content_role="UNKNOWN",
        confidence=0.2,
        evidence_spans=(),
        abstain=True,
        abstain_reason="LOW_CONFIDENCE",
    )
    assert unknown.abstain is True


def test_non_abstained_triage_requires_evidence_and_rejects_coercion() -> None:
    with pytest.raises(ValidationError, match="requires verbatim evidence"):
        _verdict(evidence_spans=())
    with pytest.raises(ValidationError, match="finite float"):
        _verdict(confidence=1)
    with pytest.raises(ValidationError, match="boolean without coercion"):
        _verdict(abstain=0)


def test_triage_evidence_must_match_exact_segment_text() -> None:
    segment = SourceSegment.from_text(
        segment_id=SEGMENT_ID,
        artifact_version_id=ARTIFACT_VERSION_ID,
        ordinal=0,
        locator=DocxParagraphLocator(paragraph_index=0),
        text="王城位于北境",
    )
    matching = _verdict(
        segment_id=segment.segment_id,
        evidence_spans=(TriageEvidenceSpan(start=0, end=2, quote="王城"),),
    )
    assert validate_verdict_against_segment(matching, segment) == matching

    forged = _verdict(
        segment_id=segment.segment_id,
        evidence_spans=(TriageEvidenceSpan(start=0, end=2, quote="伪造"),),
    )
    with pytest.raises(ValueError, match="quote does not match"):
        validate_verdict_against_segment(forged, segment)

    overflowing = _verdict(
        segment_id=segment.segment_id,
        evidence_spans=(TriageEvidenceSpan(start=0, end=999, quote="越界"),),
    )
    with pytest.raises(ValueError, match="exceeds"):
        validate_verdict_against_segment(overflowing, segment)
