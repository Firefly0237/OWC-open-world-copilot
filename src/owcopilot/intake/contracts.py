"""Immutable N2.1 intake, locator, and derived-triage contracts."""

from __future__ import annotations

import hashlib
import math
import re
from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, ValidationInfo, field_validator, model_validator

from ..contracts._base import _StrictFrozenModel
from ..contracts.identity import ArtifactIdentity

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", flags=re.ASCII)
_A1_RE = re.compile(r"[A-Z]{1,3}[1-9][0-9]*\Z", flags=re.ASCII)
PDF_PROCESSING_FAILURE_CODES = frozenset(
    {
        "CORRUPT_PDF",
        "PDF_LAYOUT_BACKEND_UNAVAILABLE",
        "PDF_MAGIC_MISMATCH",
        "PDF_PAGE_DIMENSION_EXCEEDED",
        "PDF_PAGE_LIMIT_EXCEEDED",
        "PDF_PROCESSING_FAILED",
        "PDF_TEXT_LIMIT_EXCEEDED",
        "PDF_VALIDATION_BACKEND_UNAVAILABLE",
        "PDF_ENCRYPTED",
        "UNLOCATABLE_PDF_TEXT",
    }
)


def _exact_text(value: object, *, label: str, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} must be a string without coercion")
    if not allow_empty and not value:
        raise ValueError(f"{label} must not be empty")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} contains a lone Unicode surrogate") from exc
    return value


def _uuid(value: object, *, label: str) -> str:
    text = _exact_text(value, label=label)
    try:
        parsed = UUID(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical UUID string") from exc
    if parsed.int == 0 or str(parsed) != text:
        raise ValueError(f"{label} must be a canonical non-nil UUID string")
    return text


def _sha256(value: object, *, label: str) -> str:
    text = _exact_text(value, label=label)
    if _SHA256_RE.fullmatch(text) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return text


def _nonnegative_int(value: object, *, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer without coercion")
    return value


class DocxParagraphLocator(_StrictFrozenModel):
    """A zero-based paragraph locator in the exact DOCX artifact version."""

    kind: Literal["docx_paragraph"] = "docx_paragraph"
    paragraph_index: int

    @field_validator("paragraph_index", mode="before")
    @classmethod
    def validate_index(cls, value: object) -> int:
        return _nonnegative_int(value, label="paragraph_index")


class DocxTableCellLocator(_StrictFrozenModel):
    """A zero-based DOCX table/row/column locator."""

    kind: Literal["docx_table_cell"] = "docx_table_cell"
    table_index: int
    row_index: int
    column_index: int

    @field_validator("table_index", "row_index", "column_index", mode="before")
    @classmethod
    def validate_indices(cls, value: object, info: ValidationInfo) -> int:
        return _nonnegative_int(value, label=info.field_name or "locator index")


class XlsxCellLocator(_StrictFrozenModel):
    """A one-based worksheet cell locator with a redundant checked A1 reference."""

    kind: Literal["xlsx_cell"] = "xlsx_cell"
    sheet_name: str
    row_index: int
    column_index: int
    cell_reference: str

    @field_validator("sheet_name", "cell_reference", mode="before")
    @classmethod
    def validate_text(cls, value: object, info: ValidationInfo) -> str:
        return _exact_text(value, label=info.field_name or "locator text")

    @field_validator("row_index", "column_index", mode="before")
    @classmethod
    def validate_indices(cls, value: object, info: ValidationInfo) -> int:
        observed = _nonnegative_int(value, label=info.field_name or "locator index")
        if observed == 0:
            raise ValueError(f"{info.field_name} must use one-based XLSX coordinates")
        limit = 1_048_576 if info.field_name == "row_index" else 16_384
        if observed > limit:
            raise ValueError(f"{info.field_name} exceeds the XLSX coordinate limit")
        return observed

    @field_validator("cell_reference")
    @classmethod
    def validate_a1_reference(cls, value: str) -> str:
        if _A1_RE.fullmatch(value) is None:
            raise ValueError("cell_reference must be an uppercase A1 coordinate")
        return value

    @model_validator(mode="after")
    def validate_redundant_coordinate(self) -> Self:
        column = self.column_index
        letters = ""
        while column:
            column, remainder = divmod(column - 1, 26)
            letters = chr(ord("A") + remainder) + letters
        expected = f"{letters}{self.row_index}"
        if self.cell_reference != expected:
            raise ValueError("cell_reference must exactly match row_index and column_index")
        return self


class PdfTextLineLocator(_StrictFrozenModel):
    """A born-digital PDF text line in displayed-page coordinates.

    ``page_index`` and ``line_index`` are zero based.  The bbox and page dimensions
    use PDF points (1/72 inch), with the origin at the top-left of the effective
    displayed page, x increasing right and y increasing down.  Backend provenance is
    part of the locator because a later rich-layout candidate must not silently
    reinterpret coordinates produced by this deliberately narrower baseline.
    """

    kind: Literal["pdf_text_line"] = "pdf_text_line"
    page_index: int
    line_index: int
    x0: float
    top: float
    x1: float
    bottom: float
    page_width: float
    page_height: float
    coordinate_space: Literal["pdf_points_top_left_display"] = (
        "pdf_points_top_left_display"
    )
    extraction_mode: Literal["born_digital_text_line_baseline"] = (
        "born_digital_text_line_baseline"
    )
    validation_backend: Literal["pypdfium2"] = "pypdfium2"
    validation_backend_version: str
    extraction_backend: Literal["pdfplumber"] = "pdfplumber"
    extraction_backend_version: str

    @field_validator("page_index", "line_index", mode="before")
    @classmethod
    def validate_indices(cls, value: object, info: ValidationInfo) -> int:
        return _nonnegative_int(value, label=info.field_name or "PDF locator index")

    @field_validator(
        "x0",
        "top",
        "x1",
        "bottom",
        "page_width",
        "page_height",
        mode="before",
    )
    @classmethod
    def validate_points(cls, value: object, info: ValidationInfo) -> float:
        if type(value) is not float or not math.isfinite(value):
            raise ValueError(f"{info.field_name} must be a finite float without coercion")
        return value

    @field_validator("validation_backend_version", "extraction_backend_version", mode="before")
    @classmethod
    def validate_backend_versions(cls, value: object, info: ValidationInfo) -> str:
        version = _exact_text(value, label=info.field_name or "PDF backend version")
        if not version.isascii() or len(version) > 128:
            raise ValueError(f"{info.field_name} must be a short ASCII package version")
        return version

    @model_validator(mode="after")
    def validate_display_bbox(self) -> Self:
        if self.page_width <= 0.0 or self.page_height <= 0.0:
            raise ValueError("PDF page dimensions must be positive")
        if self.x0 < 0.0 or self.top < 0.0:
            raise ValueError("PDF bbox coordinates must be non-negative")
        if self.x1 <= self.x0 or self.bottom <= self.top:
            raise ValueError("PDF bbox must have positive width and height")
        if self.x1 > self.page_width or self.bottom > self.page_height:
            raise ValueError("PDF bbox must stay within the displayed page dimensions")
        return self

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        """Return ``(x0, top, x1, bottom)`` in ``coordinate_space``."""

        return (self.x0, self.top, self.x1, self.bottom)


SourceLocator = Annotated[
    DocxParagraphLocator | DocxTableCellLocator | PdfTextLineLocator | XlsxCellLocator,
    Field(discriminator="kind"),
]


class SourceSegment(_StrictFrozenModel):
    """One immutable, untrusted text segment with an exact source locator."""

    segment_id: str
    artifact_version_id: str
    ordinal: int
    locator: SourceLocator
    text: str
    text_hash: str
    untrusted_input: Literal[True] = True

    @field_validator("segment_id", "artifact_version_id", mode="before")
    @classmethod
    def validate_ids(cls, value: object, info: ValidationInfo) -> str:
        return _uuid(value, label=info.field_name or "identifier")

    @field_validator("ordinal", mode="before")
    @classmethod
    def validate_ordinal(cls, value: object) -> int:
        return _nonnegative_int(value, label="ordinal")

    @field_validator("text", mode="before")
    @classmethod
    def validate_text(cls, value: object) -> str:
        return _exact_text(value, label="text")

    @field_validator("text_hash", mode="before")
    @classmethod
    def validate_text_hash(cls, value: object) -> str:
        return _sha256(value, label="text_hash")

    @model_validator(mode="after")
    def validate_derived_hash(self) -> Self:
        expected = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        if self.text_hash != expected:
            raise ValueError("text_hash does not match the exact UTF-8 segment text")
        return self

    @classmethod
    def from_text(
        cls,
        *,
        segment_id: str,
        artifact_version_id: str,
        ordinal: int,
        locator: SourceLocator,
        text: str,
    ) -> Self:
        checked_text = _exact_text(text, label="text")
        return cls(
            segment_id=segment_id,
            artifact_version_id=artifact_version_id,
            ordinal=ordinal,
            locator=locator,
            text=checked_text,
            text_hash=hashlib.sha256(checked_text.encode("utf-8")).hexdigest(),
            untrusted_input=True,
        )


class IntakeRun(_StrictFrozenModel):
    """A durable snapshot of one server-owned intake run state."""

    run_id: str
    artifact_version_id: str
    artifact: ArtifactIdentity
    original_filename: str
    extension: Literal[".docx", ".pdf", ".xlsx"]
    byte_size: int
    status: Literal[
        "UPLOADED",
        "SEGMENTED",
        "OCR_REQUIRED",
        "PROCESSING_FAILED",
        "AWAITING_HUMAN",
        "REJECTED",
    ]
    created_by: str
    started_at: datetime
    finished_at: datetime | None = None
    rejection_code: str | None = None
    processing_code: Literal[
        "PDF_OCR_REQUIRED",
        "CORRUPT_PDF",
        "PDF_LAYOUT_BACKEND_UNAVAILABLE",
        "PDF_MAGIC_MISMATCH",
        "PDF_PAGE_DIMENSION_EXCEEDED",
        "PDF_PAGE_LIMIT_EXCEEDED",
        "PDF_PROCESSING_FAILED",
        "PDF_TEXT_LIMIT_EXCEEDED",
        "PDF_VALIDATION_BACKEND_UNAVAILABLE",
        "PDF_ENCRYPTED",
        "UNLOCATABLE_PDF_TEXT",
    ] | None = None

    @field_validator("run_id", "artifact_version_id", "created_by", mode="before")
    @classmethod
    def validate_ids(cls, value: object, info: ValidationInfo) -> str:
        return _uuid(value, label=info.field_name or "identifier")

    @field_validator("original_filename", mode="before")
    @classmethod
    def validate_filename(cls, value: object) -> str:
        text = _exact_text(value, label="original_filename")
        if "/" in text or "\\" in text or text in {".", ".."}:
            raise ValueError("original_filename must be a basename")
        return text

    @field_validator("byte_size", mode="before")
    @classmethod
    def validate_byte_size(cls, value: object) -> int:
        observed = _nonnegative_int(value, label="byte_size")
        if observed == 0:
            raise ValueError("byte_size must be positive")
        return observed

    @field_validator("rejection_code", mode="before")
    @classmethod
    def validate_rejection_code(cls, value: object) -> str | None:
        if value is None:
            return None
        return _exact_text(value, label="rejection_code")

    @field_validator("started_at", "finished_at")
    @classmethod
    def validate_utc_times(cls, value: datetime | None, info: ValidationInfo) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError(f"{info.field_name} must be timezone-aware UTC")
        return value

    @model_validator(mode="after")
    def validate_state_shape(self) -> Self:
        rejected = self.status == "REJECTED"
        if rejected != (self.rejection_code is not None):
            raise ValueError("only REJECTED runs carry rejection_code")
        requires_ocr = self.status == "OCR_REQUIRED"
        if requires_ocr != (self.processing_code == "PDF_OCR_REQUIRED"):
            raise ValueError("only OCR_REQUIRED runs carry PDF_OCR_REQUIRED processing_code")
        processing_failed = self.status == "PROCESSING_FAILED"
        carries_failure = self.processing_code in PDF_PROCESSING_FAILURE_CODES
        if processing_failed != carries_failure:
            raise ValueError("only PROCESSING_FAILED runs carry a PDF processing failure code")
        terminal = self.status in {"AWAITING_HUMAN", "REJECTED"}
        if terminal != (self.finished_at is not None):
            raise ValueError("only terminal intake states carry finished_at")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
        if self.artifact.artifact_id != self.artifact_version_id:
            raise ValueError("artifact identity must bind the exact artifact_version_id")
        if not self.original_filename.casefold().endswith(self.extension):
            raise ValueError("original_filename extension differs from the accepted extension")
        return self


class TriageEvidenceSpan(_StrictFrozenModel):
    """A verbatim half-open character span within one SourceSegment."""

    start: int
    end: int
    quote: str

    @field_validator("start", "end", mode="before")
    @classmethod
    def validate_offsets(cls, value: object, info: ValidationInfo) -> int:
        return _nonnegative_int(value, label=info.field_name or "span offset")

    @field_validator("quote", mode="before")
    @classmethod
    def validate_quote(cls, value: object) -> str:
        return _exact_text(value, label="quote")

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if self.end <= self.start:
            raise ValueError("triage evidence span must be non-empty")
        return self


class TriageVerdict(_StrictFrozenModel):
    """A validated derivation that never replaces its separately persisted raw output."""

    verdict_id: str
    raw_output_id: str
    segment_id: str
    content_role: Literal["CANON_CANDIDATE", "REFERENCE", "MIXED", "UNKNOWN"]
    confidence: float
    policy_threshold: float
    evidence_spans: tuple[TriageEvidenceSpan, ...] = ()
    abstain: bool
    abstain_reason: str | None = None
    derivation_policy_version: str

    @field_validator("verdict_id", "raw_output_id", "segment_id", mode="before")
    @classmethod
    def validate_ids(cls, value: object, info: ValidationInfo) -> str:
        return _uuid(value, label=info.field_name or "identifier")

    @field_validator("confidence", "policy_threshold", mode="before")
    @classmethod
    def validate_probability(cls, value: object, info: ValidationInfo) -> float:
        if type(value) is not float or not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError(f"{info.field_name} must be a finite float in [0, 1]")
        return value

    @field_validator("abstain", mode="before")
    @classmethod
    def validate_abstain(cls, value: object) -> bool:
        if type(value) is not bool:
            raise ValueError("abstain must be a boolean without coercion")
        return value

    @field_validator("abstain_reason", mode="before")
    @classmethod
    def validate_reason(cls, value: object) -> str | None:
        if value is None:
            return None
        return _exact_text(value, label="abstain_reason")

    @field_validator("derivation_policy_version", mode="before")
    @classmethod
    def validate_policy_version(cls, value: object) -> str:
        return _exact_text(value, label="derivation_policy_version")

    @model_validator(mode="after")
    def validate_abstention_policy(self) -> Self:
        must_abstain = self.content_role == "UNKNOWN" or self.confidence < self.policy_threshold
        if must_abstain and not self.abstain:
            raise ValueError("UNKNOWN and below-threshold verdicts must abstain")
        if self.abstain != (self.abstain_reason is not None):
            raise ValueError("abstain_reason is present exactly when abstain is true")
        if not self.abstain and not self.evidence_spans:
            raise ValueError("a non-abstained verdict requires verbatim evidence spans")
        return self


def validate_verdict_against_segment(
    verdict: TriageVerdict,
    segment: SourceSegment,
) -> TriageVerdict:
    """Bind every claimed evidence span to the exact immutable segment text."""

    checked_verdict = TriageVerdict.model_validate(verdict)
    checked_segment = SourceSegment.model_validate(segment)
    if checked_verdict.segment_id != checked_segment.segment_id:
        raise ValueError("triage verdict and source segment identifiers disagree")
    for evidence in checked_verdict.evidence_spans:
        if evidence.end > len(checked_segment.text):
            raise ValueError("triage evidence span exceeds the source segment")
        if checked_segment.text[evidence.start : evidence.end] != evidence.quote:
            raise ValueError("triage evidence quote does not match the source segment")
    return checked_verdict


__all__ = [
    "DocxParagraphLocator",
    "DocxTableCellLocator",
    "IntakeRun",
    "PDF_PROCESSING_FAILURE_CODES",
    "PdfTextLineLocator",
    "SourceLocator",
    "SourceSegment",
    "TriageEvidenceSpan",
    "TriageVerdict",
    "XlsxCellLocator",
    "validate_verdict_against_segment",
]
