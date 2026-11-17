from __future__ import annotations

import hashlib
import io
import json
from dataclasses import replace
from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic import ValidationError
from pypdf import PdfReader, PdfWriter

pytest.importorskip("pypdfium2")
pytest.importorskip("pdfplumber")

from owcopilot.contracts import OwnershipScope
from owcopilot.inspiration import Provenance, ReferenceContextBuilder, ReferenceStore
from owcopilot.intake import (
    PDF_MIME,
    IntakePolicy,
    IntakeValidationError,
    PdfTextLineLocator,
    TeamIntakeService,
    ValidatedUpload,
    parse_source_segments,
    resolve_source_locator,
    validate_upload_chunks,
    verify_source_segment,
)
from owcopilot.llm.cache import HashingEmbedder
from owcopilot.retrieval import ChunkingPolicy
from owcopilot.storage import SQLiteStore
from owcopilot.teamdb import TeamIntakeRepository, TeamIntegrityError

ARTIFACT_VERSION_ID = "11111111-1111-4111-8111-111111111111"
TENANT_ID = "22222222-2222-4222-8222-222222222222"
CONTAINER_ID = "33333333-3333-4333-8333-333333333333"
ACTOR_ID = "44444444-4444-4444-8444-444444444444"


def _pdf_literal(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return escaped.encode("ascii")


def _pdf_bytes(
    pages: tuple[tuple[str, ...], ...],
    *,
    width: int = 612,
    height: int = 792,
) -> bytes:
    """Build a small, standards-shaped PDF fixture with exact xref offsets."""

    if not pages:
        raise ValueError("PDF fixture needs at least one page")
    page_ids = tuple(3 + index * 2 for index in range(len(pages)))
    content_ids = tuple(value + 1 for value in page_ids)
    font_id = 3 + len(pages) * 2
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            b"<< /Type /Pages /Kids ["
            + b" ".join(f"{value} 0 R".encode("ascii") for value in page_ids)
            + f"] /Count {len(pages)} >>".encode("ascii")
        ),
    ]
    for page_id, content_id, lines in zip(page_ids, content_ids, pages, strict=True):
        del page_id  # its position in ``objects`` already determines the object id
        commands: list[bytes] = []
        if lines:
            commands.extend(
                [
                    b"BT",
                    b"/F1 12 Tf",
                    f"72 {height - 72} Td".encode("ascii"),
                ]
            )
            for line_index, line in enumerate(lines):
                if line_index:
                    commands.append(b"0 -24 Td")
                commands.append(b"(" + _pdf_literal(line) + b") Tj")
            commands.append(b"ET")
        stream = b"\n".join(commands) + (b"\n" if commands else b"")
        page = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] ".encode(
                "ascii"
            )
            + f"/Resources << /Font << /F1 {font_id} 0 R >> >> ".encode("ascii")
            + f"/Contents {content_id} 0 R >>".encode("ascii")
        )
        objects.extend(
            [
                page,
                b"<< /Length "
                + str(len(stream)).encode("ascii")
                + b" >>\nstream\n"
                + stream
                + b"endstream",
            ]
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_id, payload in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{object_id} 0 obj\n".encode("ascii"))
        output.extend(payload)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(output)


@pytest.fixture
def born_digital_pdf() -> bytes:
    return _pdf_bytes(
        (
            ("Quest condition: recover the sigil.", "Return it to the archivist."),
            ("Completion unlocks the north gate.",),
        )
    )


def _validated_pdf(data: bytes, *, policy: IntakePolicy | None = None) -> ValidatedUpload:
    return validate_upload_chunks(
        (data[:17], data[17:]),
        original_filename="quest.PDF",
        mime_type=PDF_MIME,
        policy=policy or IntakePolicy(),
    )


def test_born_digital_pdf_preserves_line_order_bbox_backend_and_round_trip(
    born_digital_pdf: bytes,
) -> None:
    upload = _validated_pdf(born_digital_pdf)
    segments = parse_source_segments(upload, artifact_version_id=ARTIFACT_VERSION_ID)

    assert upload.content_hash == hashlib.sha256(born_digital_pdf).hexdigest()
    assert [segment.text for segment in segments] == [
        "Quest condition: recover the sigil.",
        "Return it to the archivist.",
        "Completion unlocks the north gate.",
    ]
    locators = [cast(PdfTextLineLocator, segment.locator) for segment in segments]
    assert [(item.page_index, item.line_index) for item in locators] == [(0, 0), (0, 1), (1, 0)]
    assert all(item.coordinate_space == "pdf_points_top_left_display" for item in locators)
    assert all(item.extraction_mode == "born_digital_text_line_baseline" for item in locators)
    assert all(item.validation_backend == "pypdfium2" for item in locators)
    assert all(item.extraction_backend == "pdfplumber" for item in locators)
    assert all(
        item.validation_backend_version and item.extraction_backend_version for item in locators
    )
    assert all(item.page_width == 612.0 and item.page_height == 792.0 for item in locators)
    assert all(
        0.0 <= item.x0 < item.x1 <= item.page_width
        and 0.0 <= item.top < item.bottom <= item.page_height
        for item in locators
    )
    assert [resolve_source_locator(upload, item.locator) for item in segments] == [
        item.text for item in segments
    ]
    assert [verify_source_segment(upload, item) for item in segments] == list(segments)
    assert [item.segment_id for item in segments] == [
        item.segment_id
        for item in parse_source_segments(upload, artifact_version_id=ARTIFACT_VERSION_ID)
    ]


def test_born_digital_pdf_flows_into_reference_rag_with_locator_round_trip(
    born_digital_pdf: bytes, tmp_path: Any
) -> None:
    upload = _validated_pdf(born_digital_pdf)
    segments = parse_source_segments(upload, artifact_version_id=ARTIFACT_VERSION_ID)
    reference_store = ReferenceStore(tmp_path / "content")
    added = reference_store.add_source_segments(
        title="Quest PDF",
        segments=segments,
        policy=ChunkingPolicy(
            version="pdf-structure-demo-v1",
            strategy="structure",
            min_tokens=1,
            target_tokens=24,
            max_tokens=48,
            overlap_tokens=0,
        ),
        original_filename="quest.pdf",
        provenance=Provenance.OWN_IP,
        token_counter=lambda text: len(text.split()),
        token_counter_id="fixture-word-count-v1",
    )
    sqlite_store = SQLiteStore(tmp_path / "runtime.sqlite")
    try:
        reference_store.sync_index(sqlite_store)
        pack = ReferenceContextBuilder(sqlite_store, embedder=HashingEmbedder()).build(
            "north gate", limit=3
        )
    finally:
        sqlite_store.close()

    assert pack.hits
    hit = pack.hits[0]
    assert hit.metadata["artifact_version_id"] == ARTIFACT_VERSION_ID
    assert hit.metadata["chunk_strategy"] == "structure"
    restored_locators = [
        PdfTextLineLocator.model_validate(locator)
        for locator in json.loads(hit.metadata["locators_json"])
    ]
    assert [resolve_source_locator(upload, locator) for locator in restored_locators] == [
        "Quest condition: recover the sigil.",
        "Return it to the archivist.",
        "Completion unlocks the north gate.",
    ]
    assert added.source.metadata["retrieval_document_schema"] == "retrieval-document-v1"


def test_parser_derived_locator_version_drift_fails_closed(born_digital_pdf: bytes) -> None:
    upload = _validated_pdf(born_digital_pdf)
    segment = parse_source_segments(upload, artifact_version_id=ARTIFACT_VERSION_ID)[0]
    locator = cast(PdfTextLineLocator, segment.locator)
    stale = PdfTextLineLocator.model_validate(
        {**locator.model_dump(), "extraction_backend_version": "0.0-stale"}
    )

    with pytest.raises(IntakeValidationError) as captured:
        resolve_source_locator(upload, stale)
    assert captured.value.code == "LOCATOR_NOT_FOUND"


@pytest.mark.parametrize(
    "overrides",
    [
        {"page_index": -1},
        {"x0": float("nan")},
        {"x1": 700.0},
        {"bottom": 10.0, "top": 10.0},
        {"coordinate_space": "pdf_bottom_left"},
    ],
)
def test_pdf_locator_rejects_invalid_or_ambiguous_coordinates(
    born_digital_pdf: bytes,
    overrides: dict[str, object],
) -> None:
    locator = cast(
        PdfTextLineLocator,
        parse_source_segments(
            _validated_pdf(born_digital_pdf),
            artifact_version_id=ARTIFACT_VERSION_ID,
        )[0].locator,
    )
    with pytest.raises(ValidationError):
        PdfTextLineLocator.model_validate({**locator.model_dump(), **overrides})


def test_pdf_magic_corruption_and_encryption_are_distinct(born_digital_pdf: bytes) -> None:
    for payload, expected in (
        (b"not a pdf", "PDF_MAGIC_MISMATCH"),
        (b"%PDF-1.7\nnot a document", "CORRUPT_PDF"),
    ):
        upload = _validated_pdf(payload)
        with pytest.raises(IntakeValidationError) as captured:
            parse_source_segments(upload, artifact_version_id=ARTIFACT_VERSION_ID)
        assert captured.value.code == expected

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(born_digital_pdf)))
    writer.encrypt("fixture-password")
    encrypted = io.BytesIO()
    writer.write(encrypted)
    upload = _validated_pdf(encrypted.getvalue())
    with pytest.raises(IntakeValidationError) as protected:
        parse_source_segments(upload, artifact_version_id=ARTIFACT_VERSION_ID)
    assert protected.value.code == "PDF_ENCRYPTED"


def test_blank_or_partially_textless_pdf_requires_ocr_without_fake_success() -> None:
    for pages in (((),), (("born digital",), ())):
        upload = _validated_pdf(_pdf_bytes(pages))
        assert upload.extension == ".pdf"
        with pytest.raises(IntakeValidationError) as captured:
            parse_source_segments(
                upload,
                artifact_version_id=ARTIFACT_VERSION_ID,
            )
        assert captured.value.code == "PDF_OCR_REQUIRED"


@pytest.mark.parametrize(
    ("data", "policy"),
    [
        (
            _pdf_bytes((("page one",), ("page two",))),
            IntakePolicy(max_pdf_pages=1),
        ),
        (
            _pdf_bytes((("oversized page",),), width=612, height=792),
            IntakePolicy(max_pdf_page_dimension_points=700),
        ),
        (
            _pdf_bytes((("too many characters",),)),
            IntakePolicy(max_pdf_text_characters=5),
        ),
        (
            _pdf_bytes((("line one", "line two"),)),
            IntakePolicy(max_pdf_text_lines=1),
        ),
        (
            _pdf_bytes((("six bytes",),)),
            IntakePolicy(max_pdf_text_bytes=5),
        ),
    ],
)
def test_pdf_resource_limits_cover_pages_dimensions_lines_chars_and_bytes(
    data: bytes,
    policy: IntakePolicy,
) -> None:
    upload = _validated_pdf(data, policy=policy)
    with pytest.raises(IntakeValidationError) as captured:
        parse_source_segments(
            upload,
            artifact_version_id=ARTIFACT_VERSION_ID,
            policy=policy,
        )
    assert captured.value.status_code == 413
    assert captured.value.code in {
        "PDF_PAGE_LIMIT_EXCEEDED",
        "PDF_PAGE_DIMENSION_EXCEEDED",
        "PDF_TEXT_LIMIT_EXCEEDED",
    }


class _RecordingRepository:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []
        self.status = "UPLOADED"
        self.processing_code: str | None = None

    def _record(self, name: str, values: dict[str, object]) -> None:
        self.events.append((name, values))

    def create_uploaded_run(self, _scope: OwnershipScope, **values: object) -> None:
        self._record("create", dict(values))

    def create_replacement_uploaded_run(
        self, _scope: OwnershipScope, **values: object
    ) -> None:
        self._record("replace", dict(values))

    def store_segments(self, _scope: OwnershipScope, **values: object) -> None:
        self._record("segments", dict(values))

    def store_raw_triage_output(self, _scope: OwnershipScope, **values: object) -> None:
        self._record("raw", dict(values))

    def store_triage_verdict(self, _scope: OwnershipScope, **values: object) -> None:
        self._record("verdict", dict(values))

    def mark_awaiting_human(self, _scope: OwnershipScope, **values: object) -> None:
        self.status = "AWAITING_HUMAN"
        self._record("awaiting", dict(values))

    def mark_ocr_required(self, _scope: OwnershipScope, **values: object) -> None:
        self.status = "OCR_REQUIRED"
        self.processing_code = "PDF_OCR_REQUIRED"
        self._record("ocr_required", dict(values))

    def mark_processing_failed(self, _scope: OwnershipScope, **values: object) -> None:
        self.status = "PROCESSING_FAILED"
        self.processing_code = cast(str, values["processing_code"])
        self._record("processing_failed", dict(values))

    def read_intake_run(self, _scope: OwnershipScope, **values: object) -> dict[str, object]:
        self._record("read", dict(values))
        return {
            "run_id": values["run_id"],
            "status": self.status,
            "processing_code": self.processing_code,
            "artifact": {"immutable_bytes_retained": True},
        }


def test_pdf_uses_the_real_service_and_repository_contract(born_digital_pdf: bytes) -> None:
    repository = _RecordingRepository()
    service = TeamIntakeService(cast(Any, repository), intake_policy=IntakePolicy())
    result = service.create_intake_run(
        OwnershipScope(tenant_id=TENANT_ID, container_id=CONTAINER_ID),
        actor_subject=ACTOR_ID,
        original_filename="quest.pdf",
        mime_type=PDF_MIME,
        chunks=(born_digital_pdf,),
    )

    assert result["status"] == "AWAITING_HUMAN"
    segment_event = next(values for name, values in repository.events if name == "segments")
    segments = segment_event["segments"]
    assert isinstance(segments, tuple)
    assert {item.locator.kind for item in segments} == {"pdf_text_line"}


def test_scanned_pdf_is_persisted_before_durable_ocr_handoff() -> None:
    data = _pdf_bytes(((),))
    repository = _RecordingRepository()
    service = TeamIntakeService(cast(Any, repository), intake_policy=IntakePolicy())

    result = service.create_intake_run(
        OwnershipScope(tenant_id=TENANT_ID, container_id=CONTAINER_ID),
        actor_subject=ACTOR_ID,
        original_filename="scan.pdf",
        mime_type=PDF_MIME,
        chunks=(data,),
    )

    assert result["status"] == "OCR_REQUIRED"
    assert result["processing_code"] == "PDF_OCR_REQUIRED"
    assert result["artifact"] == {"immutable_bytes_retained": True}
    assert [name for name, _values in repository.events] == [
        "create",
        "ocr_required",
        "read",
    ]
    upload = cast(ValidatedUpload, repository.events[0][1]["upload"])
    assert upload.content_bytes == data
    assert upload.content_hash == hashlib.sha256(data).hexdigest()


def test_post_persist_pdf_failure_becomes_queryable_instead_of_orphaned() -> None:
    data = _pdf_bytes((("line one", "line two"),))
    repository = _RecordingRepository()
    service = TeamIntakeService(
        cast(Any, repository),
        intake_policy=IntakePolicy(max_pdf_text_lines=1),
    )

    result = service.create_intake_run(
        OwnershipScope(tenant_id=TENANT_ID, container_id=CONTAINER_ID),
        actor_subject=ACTOR_ID,
        original_filename="too-many-lines.pdf",
        mime_type=PDF_MIME,
        chunks=(data,),
    )

    assert result["status"] == "PROCESSING_FAILED"
    assert result["processing_code"] == "PDF_TEXT_LIMIT_EXCEEDED"
    assert [name for name, _values in repository.events] == [
        "create",
        "processing_failed",
        "read",
    ]
    upload = cast(ValidatedUpload, repository.events[0][1]["upload"])
    assert upload.content_bytes == data


@pytest.mark.parametrize(
    ("data", "expected_code"),
    [
        (b"not a pdf", "PDF_MAGIC_MISMATCH"),
        (b"%PDF-1.7\nnot a document", "CORRUPT_PDF"),
    ],
)
def test_invalid_pdf_bytes_are_retained_before_distinct_processing_failure(
    data: bytes,
    expected_code: str,
) -> None:
    repository = _RecordingRepository()
    service = TeamIntakeService(cast(Any, repository), intake_policy=IntakePolicy())

    result = service.create_intake_run(
        OwnershipScope(tenant_id=TENANT_ID, container_id=CONTAINER_ID),
        actor_subject=ACTOR_ID,
        original_filename="invalid.pdf",
        mime_type=PDF_MIME,
        chunks=(data,),
    )

    assert result["status"] == "PROCESSING_FAILED"
    assert result["processing_code"] == expected_code
    assert [name for name, _values in repository.events] == [
        "create",
        "processing_failed",
        "read",
    ]
    upload = cast(ValidatedUpload, repository.events[0][1]["upload"])
    assert upload.content_bytes == data


def test_repository_accepts_only_the_exact_pdf_format_pair(born_digital_pdf: bytes) -> None:
    upload = _validated_pdf(born_digital_pdf)
    assert TeamIntakeRepository._validate_upload(upload) is upload  # noqa: SLF001

    forged = replace(upload, mime_type="application/octet-stream")
    with pytest.raises(TeamIntegrityError, match="extension and MIME"):
        TeamIntakeRepository._validate_upload(forged)  # noqa: SLF001


def test_hand_constructed_pdf_upload_still_revalidates_exact_bytes(born_digital_pdf: bytes) -> None:
    forged = ValidatedUpload(
        original_filename="quest.pdf",
        extension=".pdf",
        mime_type=PDF_MIME,
        byte_size=len(born_digital_pdf),
        content_hash=hashlib.sha256(born_digital_pdf).hexdigest(),
        content_bytes=born_digital_pdf + b"tampered",
    )
    with pytest.raises(IntakeValidationError) as captured:
        parse_source_segments(forged, artifact_version_id=str(uuid4()))
    assert captured.value.code == "ARTIFACT_HASH_MISMATCH"
