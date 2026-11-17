"""Fail-closed OOXML upload validation and locator-preserving segmentation."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import io
import json
import math
import os
import posixpath
import re
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, BinaryIO, Final
from uuid import NAMESPACE_URL, UUID, uuid5
from xml.etree import ElementTree

from .contracts import (
    DocxParagraphLocator,
    DocxTableCellLocator,
    PdfTextLineLocator,
    SourceLocator,
    SourceSegment,
    XlsxCellLocator,
)

DOCX_MIME: Final = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF_MIME: Final = "application/pdf"
XLSX_MIME: Final = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_DOCX_MAIN_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
_XLSX_MAIN_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
_CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CELL_RE = re.compile(r"([A-Z]{1,3})([1-9][0-9]*)\Z", flags=re.ASCII)
_AMBIGUOUS_DOCX_TABLE_TAGS: Final = frozenset(
    {
        f"{{{_WORD_NS}}}gridAfter",
        f"{{{_WORD_NS}}}gridBefore",
        f"{{{_WORD_NS}}}gridSpan",
        f"{{{_WORD_NS}}}hMerge",
        f"{{{_WORD_NS}}}vMerge",
    }
)
_PDF_HEADER_RE = re.compile(br"%PDF-(?:1\.[0-7]|2\.0)(?=[\x00\t\n\f\r ])")

_SUPPORTED: Final = {
    ".docx": (DOCX_MIME, "word/document.xml", _DOCX_MAIN_TYPE),
    ".pdf": (PDF_MIME, None, None),
    ".xlsx": (XLSX_MIME, "xl/workbook.xml", _XLSX_MAIN_TYPE),
}
_DEFAULT_MAX_UPLOAD_BYTES = 25 * 1024 * 1024
_DEFAULT_MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
_DEFAULT_MAX_ZIP_ENTRIES = 10_000
_DEFAULT_READ_CHUNK_BYTES = 64 * 1024
_DEFAULT_MAX_PDF_PAGES = 500
_DEFAULT_MAX_PDF_PAGE_DIMENSION_POINTS = 20_000
_DEFAULT_MAX_PDF_TEXT_LINES = 100_000
_DEFAULT_MAX_PDF_TEXT_CHARACTERS = 5_000_000
_DEFAULT_MAX_PDF_TEXT_BYTES = 20_000_000


class IntakeValidationError(ValueError):
    """A stable, non-reflective rejection at the trusted upload boundary."""

    def __init__(self, code: str, message: str, *, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _positive_int(value: object, *, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _environment_limit(
    environ: Mapping[str, str],
    name: str,
    default: int,
) -> int:
    raw = environ.get(name)
    if raw is None:
        return default
    if not raw or not raw.isascii() or not raw.isdecimal():
        raise ValueError(f"{name} must be a positive base-10 integer")
    return _positive_int(int(raw), label=name)


@dataclass(frozen=True, slots=True)
class IntakePolicy:
    """Startup-frozen resource bounds for one intake service instance."""

    max_upload_bytes: int = _DEFAULT_MAX_UPLOAD_BYTES
    max_uncompressed_bytes: int = _DEFAULT_MAX_UNCOMPRESSED_BYTES
    max_zip_entries: int = _DEFAULT_MAX_ZIP_ENTRIES
    read_chunk_bytes: int = _DEFAULT_READ_CHUNK_BYTES
    max_pdf_pages: int = _DEFAULT_MAX_PDF_PAGES
    max_pdf_page_dimension_points: int = _DEFAULT_MAX_PDF_PAGE_DIMENSION_POINTS
    max_pdf_text_lines: int = _DEFAULT_MAX_PDF_TEXT_LINES
    max_pdf_text_characters: int = _DEFAULT_MAX_PDF_TEXT_CHARACTERS
    max_pdf_text_bytes: int = _DEFAULT_MAX_PDF_TEXT_BYTES

    def __post_init__(self) -> None:
        _positive_int(self.max_upload_bytes, label="max_upload_bytes")
        _positive_int(self.max_uncompressed_bytes, label="max_uncompressed_bytes")
        _positive_int(self.max_zip_entries, label="max_zip_entries")
        _positive_int(self.read_chunk_bytes, label="read_chunk_bytes")
        _positive_int(self.max_pdf_pages, label="max_pdf_pages")
        _positive_int(
            self.max_pdf_page_dimension_points,
            label="max_pdf_page_dimension_points",
        )
        _positive_int(self.max_pdf_text_lines, label="max_pdf_text_lines")
        _positive_int(self.max_pdf_text_characters, label="max_pdf_text_characters")
        _positive_int(self.max_pdf_text_bytes, label="max_pdf_text_bytes")
        if self.max_uncompressed_bytes < self.max_upload_bytes:
            raise ValueError("max_uncompressed_bytes cannot be below max_upload_bytes")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> IntakePolicy:
        source = os.environ if environ is None else environ
        return cls(
            max_upload_bytes=_environment_limit(
                source, "OWCOPILOT_INTAKE_MAX_BYTES", _DEFAULT_MAX_UPLOAD_BYTES
            ),
            max_uncompressed_bytes=_environment_limit(
                source,
                "OWCOPILOT_INTAKE_MAX_UNCOMPRESSED_BYTES",
                _DEFAULT_MAX_UNCOMPRESSED_BYTES,
            ),
            max_zip_entries=_environment_limit(
                source, "OWCOPILOT_INTAKE_MAX_ZIP_ENTRIES", _DEFAULT_MAX_ZIP_ENTRIES
            ),
            read_chunk_bytes=_environment_limit(
                source, "OWCOPILOT_INTAKE_READ_CHUNK_BYTES", _DEFAULT_READ_CHUNK_BYTES
            ),
            max_pdf_pages=_environment_limit(
                source, "OWCOPILOT_INTAKE_MAX_PDF_PAGES", _DEFAULT_MAX_PDF_PAGES
            ),
            max_pdf_page_dimension_points=_environment_limit(
                source,
                "OWCOPILOT_INTAKE_MAX_PDF_PAGE_DIMENSION_POINTS",
                _DEFAULT_MAX_PDF_PAGE_DIMENSION_POINTS,
            ),
            max_pdf_text_lines=_environment_limit(
                source,
                "OWCOPILOT_INTAKE_MAX_PDF_TEXT_LINES",
                _DEFAULT_MAX_PDF_TEXT_LINES,
            ),
            max_pdf_text_characters=_environment_limit(
                source,
                "OWCOPILOT_INTAKE_MAX_PDF_TEXT_CHARACTERS",
                _DEFAULT_MAX_PDF_TEXT_CHARACTERS,
            ),
            max_pdf_text_bytes=_environment_limit(
                source,
                "OWCOPILOT_INTAKE_MAX_PDF_TEXT_BYTES",
                _DEFAULT_MAX_PDF_TEXT_BYTES,
            ),
        )


@dataclass(frozen=True, slots=True)
class ValidatedUpload:
    original_filename: str
    extension: str
    mime_type: str
    byte_size: int
    content_hash: str
    content_bytes: bytes


class UploadAccumulator:
    """Incrementally enforce the byte ceiling and compute the immutable identity hash."""

    def __init__(self, policy: IntakePolicy) -> None:
        self._policy = policy
        self._buffer = bytearray()
        self._hasher = hashlib.sha256()
        self._sealed = False

    @property
    def byte_size(self) -> int:
        return len(self._buffer)

    def feed(self, chunk: bytes) -> None:
        if self._sealed:
            raise RuntimeError("upload accumulator is already sealed")
        if type(chunk) is not bytes:
            raise TypeError("upload chunks must be exact bytes")
        if not chunk:
            return
        next_size = len(self._buffer) + len(chunk)
        if next_size > self._policy.max_upload_bytes:
            raise IntakeValidationError(
                "UPLOAD_TOO_LARGE",
                "upload exceeds the configured byte limit",
                status_code=413,
            )
        self._buffer.extend(chunk)
        self._hasher.update(chunk)

    def finish(self, *, original_filename: str, mime_type: str) -> ValidatedUpload:
        if self._sealed:
            raise RuntimeError("upload accumulator is already sealed")
        self._sealed = True
        content = bytes(self._buffer)
        self._buffer.clear()
        if not content:
            raise IntakeValidationError("EMPTY_UPLOAD", "upload body is empty")
        filename, extension, accepted_mime = _validate_declared_type(
            original_filename=original_filename,
            mime_type=mime_type,
        )
        # PDF parsing is deliberately deferred until after the immutable artifact/run
        # write.  This keeps corrupt, encrypted, scanned, and backend-unavailable
        # inputs queryable as explicit processing states instead of losing the bytes.
        if extension != ".pdf":
            _validate_ooxml_package(content, extension=extension, policy=self._policy)
        return ValidatedUpload(
            original_filename=filename,
            extension=extension,
            mime_type=accepted_mime,
            byte_size=len(content),
            content_hash=self._hasher.hexdigest(),
            content_bytes=content,
        )


def read_upload(
    stream: BinaryIO,
    *,
    original_filename: str,
    mime_type: str,
    policy: IntakePolicy,
) -> ValidatedUpload:
    accumulator = UploadAccumulator(policy)
    while True:
        chunk = stream.read(policy.read_chunk_bytes)
        if not chunk:
            break
        accumulator.feed(chunk)
    return accumulator.finish(original_filename=original_filename, mime_type=mime_type)


def validate_upload_chunks(
    chunks: Iterable[bytes],
    *,
    original_filename: str,
    mime_type: str,
    policy: IntakePolicy,
) -> ValidatedUpload:
    accumulator = UploadAccumulator(policy)
    for chunk in chunks:
        accumulator.feed(chunk)
    return accumulator.finish(original_filename=original_filename, mime_type=mime_type)


def parse_source_segments(
    upload: ValidatedUpload,
    *,
    artifact_version_id: str,
    policy: IntakePolicy | None = None,
) -> tuple[SourceSegment, ...]:
    _canonical_uuid(artifact_version_id, label="artifact_version_id")
    if hashlib.sha256(upload.content_bytes).hexdigest() != upload.content_hash:
        raise IntakeValidationError(
            "ARTIFACT_HASH_MISMATCH",
            "validated artifact bytes no longer match their immutable hash",
        )
    if upload.extension == ".docx":
        located = _docx_text_locations(upload.content_bytes)
    elif upload.extension == ".pdf":
        located = _pdf_text_locations(upload.content_bytes, policy=policy or IntakePolicy())
    elif upload.extension == ".xlsx":
        located = _xlsx_text_locations(upload.content_bytes)
    else:  # A ValidatedUpload is public, so never trust a hand-constructed instance.
        raise IntakeValidationError(
            "UNSUPPORTED_EXTENSION", "file extension is not supported", status_code=415
        )

    segments = tuple(
        SourceSegment.from_text(
            segment_id=_segment_id(
                artifact_version_id=artifact_version_id,
                ordinal=ordinal,
                locator=locator,
            ),
            artifact_version_id=artifact_version_id,
            ordinal=ordinal,
            locator=locator,
            text=text,
        )
        for ordinal, (locator, text) in enumerate(located)
    )
    if not segments:
        raise IntakeValidationError("NO_TEXT_SEGMENTS", "document contains no locatable text")
    return segments


def resolve_source_locator(
    upload: ValidatedUpload,
    locator: SourceLocator,
    *,
    policy: IntakePolicy | None = None,
) -> str:
    """Resolve one frozen locator against the exact validated artifact bytes."""

    if hashlib.sha256(upload.content_bytes).hexdigest() != upload.content_hash:
        raise IntakeValidationError(
            "ARTIFACT_HASH_MISMATCH",
            "validated artifact bytes no longer match their immutable hash",
        )
    if upload.extension == ".docx":
        located = _docx_text_locations(upload.content_bytes)
    elif upload.extension == ".pdf":
        located = _pdf_text_locations(upload.content_bytes, policy=policy or IntakePolicy())
    elif upload.extension == ".xlsx":
        located = _xlsx_text_locations(upload.content_bytes)
    else:
        raise IntakeValidationError(
            "UNSUPPORTED_EXTENSION", "file extension is not supported", status_code=415
        )
    for observed_locator, text in located:
        if observed_locator == locator:
            return text
    raise IntakeValidationError(
        "LOCATOR_NOT_FOUND",
        "source locator does not resolve within the immutable artifact version",
    )


def verify_source_segment(
    upload: ValidatedUpload,
    segment: SourceSegment,
    *,
    policy: IntakePolicy | None = None,
) -> SourceSegment:
    """Re-resolve a segment and reject any locator, text, or text-hash drift."""

    checked = SourceSegment.model_validate(segment)
    resolved = resolve_source_locator(upload, checked.locator, policy=policy)
    if resolved != checked.text:
        raise IntakeValidationError(
            "SEGMENT_TEXT_MISMATCH",
            "source segment text differs from the locator-resolved artifact text",
        )
    if hashlib.sha256(resolved.encode("utf-8")).hexdigest() != checked.text_hash:
        raise IntakeValidationError(
            "SEGMENT_HASH_MISMATCH",
            "source segment hash differs from the locator-resolved artifact text",
        )
    return checked


def _canonical_uuid(value: object, *, label: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} must be a canonical UUID string")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical UUID string") from exc
    if parsed.int == 0 or str(parsed) != value:
        raise ValueError(f"{label} must be a canonical non-nil UUID string")
    return value


def _validate_declared_type(
    *, original_filename: object, mime_type: object
) -> tuple[str, str, str]:
    if type(original_filename) is not str or not original_filename:
        raise IntakeValidationError(
            "INVALID_FILENAME", "upload filename must be a non-empty basename"
        )
    try:
        original_filename.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise IntakeValidationError(
            "INVALID_FILENAME", "upload filename is not valid UTF-8"
        ) from exc
    if (
        original_filename in {".", ".."}
        or "/" in original_filename
        or "\\" in original_filename
        or "\x00" in original_filename
    ):
        raise IntakeValidationError("INVALID_FILENAME", "upload filename must be a basename")
    extension = PurePosixPath(original_filename).suffix.casefold()
    supported = _SUPPORTED.get(extension)
    if supported is None:
        raise IntakeValidationError(
            "UNSUPPORTED_EXTENSION", "file extension is not supported", status_code=415
        )
    if type(mime_type) is not str or mime_type != supported[0]:
        raise IntakeValidationError(
            "MIME_EXTENSION_MISMATCH",
            "declared MIME type does not match the file extension",
            status_code=415,
        )
    return original_filename, extension, supported[0]


def _safe_member_name(name: str) -> bool:
    if not name or "\\" in name or "\x00" in name or name.startswith("/"):
        return False
    path = PurePosixPath(name)
    return all(part not in {"", ".", ".."} for part in path.parts)


@dataclass(frozen=True, slots=True)
class _PdfPageMetric:
    width: float
    height: float
    text_characters: int


def _load_pdfium_backend() -> tuple[Any, str]:
    """Load only the structural PDF safety backend."""

    try:
        pdfium = importlib.import_module("pypdfium2")
        pdfium_version = importlib.metadata.version("pypdfium2")
    except (ImportError, OSError, importlib.metadata.PackageNotFoundError) as exc:
        raise IntakeValidationError(
            "PDF_VALIDATION_BACKEND_UNAVAILABLE",
            "PDF structural validation requires the declared PDFium dependency",
            status_code=503,
        ) from exc
    if not callable(getattr(pdfium, "PdfDocument", None)):
        raise IntakeValidationError(
            "PDF_VALIDATION_BACKEND_UNAVAILABLE",
            "PDF structural validation backend is incomplete",
            status_code=503,
        )
    return pdfium, pdfium_version


def _load_pdfplumber_backend() -> tuple[Any, str]:
    """Load the line-layout baseline without pretending it is OCR or rich layout."""

    try:
        pdfplumber = importlib.import_module("pdfplumber")
        pdfplumber_version = importlib.metadata.version("pdfplumber")
    except (ImportError, OSError, importlib.metadata.PackageNotFoundError) as exc:
        raise IntakeValidationError(
            "PDF_LAYOUT_BACKEND_UNAVAILABLE",
            "born-digital PDF segmentation requires the declared layout dependency",
            status_code=503,
        ) from exc
    if not callable(getattr(pdfplumber, "open", None)):
        raise IntakeValidationError(
            "PDF_LAYOUT_BACKEND_UNAVAILABLE",
            "born-digital PDF layout backend is incomplete",
            status_code=503,
        )
    return pdfplumber, pdfplumber_version


def _pdf_number(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise IntakeValidationError("CORRUPT_PDF", f"PDF {label} is not numeric")
    observed = float(value)
    if not math.isfinite(observed):
        raise IntakeValidationError("CORRUPT_PDF", f"PDF {label} is not finite")
    rounded = round(observed, 6)
    return 0.0 if rounded == 0.0 else rounded


def _validate_pdf_magic(content: bytes) -> None:
    if _PDF_HEADER_RE.match(content) is None:
        raise IntakeValidationError(
            "PDF_MAGIC_MISMATCH",
            "upload bytes do not begin with a supported PDF header",
            status_code=415,
        )


def _pdf_page_metrics(
    content: bytes,
    *,
    policy: IntakePolicy,
) -> tuple[tuple[_PdfPageMetric, ...], str]:
    """Use PDFium to fail closed before layout extraction touches page contents."""

    _validate_pdf_magic(content)
    pdfium, pdfium_version = _load_pdfium_backend()
    try:
        document = pdfium.PdfDocument(content)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        if getattr(exc, "err_code", None) == 4:
            raise IntakeValidationError(
                "PDF_ENCRYPTED",
                "encrypted or password-protected PDF files are not accepted",
            ) from exc
        raise IntakeValidationError("CORRUPT_PDF", "upload is not a readable PDF") from exc

    metrics: list[_PdfPageMetric] = []
    total_characters = 0
    try:
        page_count = len(document)
        if type(page_count) is not int or page_count <= 0:
            raise IntakeValidationError("CORRUPT_PDF", "PDF contains no readable pages")
        if page_count > policy.max_pdf_pages:
            raise IntakeValidationError(
                "PDF_PAGE_LIMIT_EXCEEDED",
                "PDF exceeds the configured page limit",
                status_code=413,
            )
        for page_index in range(page_count):
            page = document.get_page(page_index)
            try:
                raw_width, raw_height = page.get_size()
                width = _pdf_number(raw_width, label="page width")
                height = _pdf_number(raw_height, label="page height")
                if width <= 0.0 or height <= 0.0:
                    raise IntakeValidationError(
                        "CORRUPT_PDF", "PDF page dimensions must be positive"
                    )
                if (
                    width > policy.max_pdf_page_dimension_points
                    or height > policy.max_pdf_page_dimension_points
                ):
                    raise IntakeValidationError(
                        "PDF_PAGE_DIMENSION_EXCEEDED",
                        "PDF page exceeds the configured dimension limit",
                        status_code=413,
                    )
                text_page = page.get_textpage()
                try:
                    character_count = text_page.count_chars()
                finally:
                    text_page.close()
                if type(character_count) is not int or character_count < 0:
                    raise IntakeValidationError(
                        "CORRUPT_PDF", "PDF text layer has an invalid character count"
                    )
                total_characters += character_count
                if total_characters > policy.max_pdf_text_characters:
                    raise IntakeValidationError(
                        "PDF_TEXT_LIMIT_EXCEEDED",
                        "PDF extracted text exceeds the configured character limit",
                        status_code=413,
                    )
                metrics.append(
                    _PdfPageMetric(
                        width=width,
                        height=height,
                        text_characters=character_count,
                    )
                )
            finally:
                page.close()
    except IntakeValidationError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise IntakeValidationError("CORRUPT_PDF", "PDF page data cannot be read") from exc
    finally:
        document.close()
    return tuple(metrics), pdfium_version


def _bounded_pdf_point(value: object, *, maximum: float, label: str) -> float:
    observed = _pdf_number(value, label=label)
    tolerance = 0.001
    if observed < -tolerance or observed > maximum + tolerance:
        raise IntakeValidationError(
            "UNLOCATABLE_PDF_TEXT",
            "PDF text line bbox falls outside its displayed page",
        )
    return min(max(observed, 0.0), maximum)


def _pdf_text_locations(
    content: bytes,
    *,
    policy: IntakePolicy,
) -> list[tuple[SourceLocator, str]]:
    """Extract a pinned born-digital text-line baseline with reversible bboxes.

    This is intentionally not OCR and not a rich-layout parser.  Backend versions are
    embedded in each parser-derived locator; resolving an old locator under different
    backend versions therefore fails closed instead of silently changing line order or
    coordinates.
    """

    metrics, pdfium_version = _pdf_page_metrics(content, policy=policy)
    if any(metric.text_characters == 0 for metric in metrics):
        raise IntakeValidationError(
            "PDF_OCR_REQUIRED",
            "one or more PDF pages have no born-digital text layer; OCR is required",
        )
    pdfplumber, pdfplumber_version = _load_pdfplumber_backend()
    located: list[tuple[SourceLocator, str]] = []
    total_lines = 0
    total_characters = 0
    total_text_bytes = 0
    try:
        with pdfplumber.open(
            io.BytesIO(content),
            password=None,
            repair=False,
            unicode_norm=None,
            raise_unicode_errors=True,
        ) as document:
            pages = document.pages
            if len(pages) != len(metrics):
                raise IntakeValidationError(
                    "CORRUPT_PDF", "PDF backends disagree about the page count"
                )
            for page_index, page in enumerate(pages):
                width = _pdf_number(page.width, label="layout page width")
                height = _pdf_number(page.height, label="layout page height")
                if width <= 0.0 or height <= 0.0:
                    raise IntakeValidationError(
                        "CORRUPT_PDF", "PDF layout page dimensions must be positive"
                    )
                if (
                    width > policy.max_pdf_page_dimension_points
                    or height > policy.max_pdf_page_dimension_points
                ):
                    raise IntakeValidationError(
                        "PDF_PAGE_DIMENSION_EXCEEDED",
                        "PDF layout page exceeds the configured dimension limit",
                        status_code=413,
                    )
                raw_lines = page.extract_text_lines(
                    strip=True,
                    return_chars=False,
                    layout=True,
                )
                if not isinstance(raw_lines, list):
                    raise IntakeValidationError(
                        "CORRUPT_PDF", "PDF layout backend returned an invalid line collection"
                    )
                page_line_count = 0
                for line_index, raw_line in enumerate(raw_lines):
                    if not isinstance(raw_line, Mapping):
                        raise IntakeValidationError(
                            "CORRUPT_PDF", "PDF layout backend returned an invalid text line"
                        )
                    text = raw_line.get("text")
                    if type(text) is not str:
                        raise IntakeValidationError(
                            "CORRUPT_PDF", "PDF layout backend returned non-text line content"
                        )
                    if not text:
                        continue
                    try:
                        encoded_text = text.encode("utf-8", errors="strict")
                    except UnicodeEncodeError as exc:
                        raise IntakeValidationError(
                            "CORRUPT_PDF", "PDF text contains an invalid Unicode scalar"
                        ) from exc
                    total_lines += 1
                    total_characters += len(text)
                    total_text_bytes += len(encoded_text)
                    if (
                        total_lines > policy.max_pdf_text_lines
                        or total_characters > policy.max_pdf_text_characters
                        or total_text_bytes > policy.max_pdf_text_bytes
                    ):
                        raise IntakeValidationError(
                            "PDF_TEXT_LIMIT_EXCEEDED",
                            "PDF extracted text exceeds the configured resource limit",
                            status_code=413,
                        )
                    x0 = _bounded_pdf_point(raw_line.get("x0"), maximum=width, label="line x0")
                    top = _bounded_pdf_point(
                        raw_line.get("top"), maximum=height, label="line top"
                    )
                    x1 = _bounded_pdf_point(raw_line.get("x1"), maximum=width, label="line x1")
                    bottom = _bounded_pdf_point(
                        raw_line.get("bottom"), maximum=height, label="line bottom"
                    )
                    if x1 <= x0 or bottom <= top:
                        raise IntakeValidationError(
                            "UNLOCATABLE_PDF_TEXT",
                            "PDF text line has no positive displayed-page bbox",
                        )
                    located.append(
                        (
                            PdfTextLineLocator(
                                page_index=page_index,
                                line_index=line_index,
                                x0=x0,
                                top=top,
                                x1=x1,
                                bottom=bottom,
                                page_width=width,
                                page_height=height,
                                validation_backend="pypdfium2",
                                validation_backend_version=pdfium_version,
                                extraction_backend="pdfplumber",
                                extraction_backend_version=pdfplumber_version,
                            ),
                            text,
                        )
                    )
                    page_line_count += 1
                if page_line_count == 0:
                    raise IntakeValidationError(
                        "PDF_OCR_REQUIRED",
                        "one or more PDF pages have no locatable born-digital text lines; "
                        "OCR is required",
                    )
    except IntakeValidationError:
        raise
    except Exception as exc:  # noqa: BLE001 - third-party parser errors are an input boundary
        raise IntakeValidationError("CORRUPT_PDF", "PDF layout data cannot be read") from exc
    return located


def _validate_ooxml_package(content: bytes, *, extension: str, policy: IntakePolicy) -> None:
    expected = _SUPPORTED[extension]
    if expected[1] is None or expected[2] is None:
        raise RuntimeError("PDF validation must not enter the OOXML package validator")
    try:
        with zipfile.ZipFile(io.BytesIO(content), mode="r") as archive:
            infos = archive.infolist()
            if len(infos) > policy.max_zip_entries:
                raise IntakeValidationError(
                    "PACKAGE_TOO_COMPLEX", "OOXML package contains too many entries"
                )
            names: set[str] = set()
            uncompressed = 0
            for info in infos:
                if not _safe_member_name(info.filename) or info.filename in names:
                    raise IntakeValidationError(
                        "INVALID_PACKAGE_PATH", "OOXML package contains an unsafe or duplicate path"
                    )
                if info.flag_bits & 0x1:
                    raise IntakeValidationError(
                        "ENCRYPTED_PACKAGE", "encrypted OOXML packages are not accepted"
                    )
                names.add(info.filename)
                uncompressed += info.file_size
                if uncompressed > policy.max_uncompressed_bytes:
                    raise IntakeValidationError(
                        "PACKAGE_TOO_LARGE",
                        "OOXML package exceeds the uncompressed byte limit",
                        status_code=413,
                    )
            required = {"[Content_Types].xml", "_rels/.rels", expected[1]}
            if not required <= names:
                raise IntakeValidationError(
                    "PACKAGE_KIND_MISMATCH",
                    "OOXML package contents do not match the declared file type",
                    status_code=415,
                )
            bad_member = archive.testzip()
            if bad_member is not None:
                raise IntakeValidationError("CORRUPT_OOXML", "OOXML package CRC validation failed")
            content_types = _parse_xml(archive.read("[Content_Types].xml"))
            overrides = {
                (item.get("PartName"), item.get("ContentType"))
                for item in content_types.findall(f"{{{_CONTENT_TYPES_NS}}}Override")
            }
            if (f"/{expected[1]}", expected[2]) not in overrides:
                raise IntakeValidationError(
                    "PACKAGE_KIND_MISMATCH",
                    "OOXML content types do not match the declared file type",
                    status_code=415,
                )
            if any(name.casefold().endswith("vbaproject.bin") for name in names):
                raise IntakeValidationError(
                    "MACRO_PACKAGE_NOT_ALLOWED", "macro-enabled OOXML packages are not accepted"
                )
    except IntakeValidationError:
        raise
    except (
        OSError,
        RuntimeError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        ElementTree.ParseError,
    ) as exc:
        raise IntakeValidationError("CORRUPT_OOXML", "upload is not a valid OOXML package") from exc


def _parse_xml(data: bytes) -> ElementTree.Element:
    lowered = data.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise IntakeValidationError(
            "UNSAFE_XML", "OOXML package contains a forbidden XML declaration"
        )
    return ElementTree.fromstring(data)


def _word_text(element: ElementTree.Element) -> str:
    parts: list[str] = []
    for node in element.iter():
        if node.tag == f"{{{_WORD_NS}}}t":
            parts.append(node.text or "")
        elif node.tag == f"{{{_WORD_NS}}}tab":
            parts.append("\t")
        elif node.tag in {f"{{{_WORD_NS}}}br", f"{{{_WORD_NS}}}cr"}:
            parts.append("\n")
    return "".join(parts)


def _docx_text_locations(content: bytes) -> list[tuple[SourceLocator, str]]:
    try:
        with zipfile.ZipFile(io.BytesIO(content), mode="r") as archive:
            root = _parse_xml(archive.read("word/document.xml"))
    except IntakeValidationError:
        raise
    except (KeyError, OSError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise IntakeValidationError("CORRUPT_DOCX", "DOCX document XML cannot be read") from exc
    body = root.find(f"{{{_WORD_NS}}}body")
    if body is None:
        raise IntakeValidationError("CORRUPT_DOCX", "DOCX document has no body")

    located: list[tuple[SourceLocator, str]] = []
    paragraph_index = 0
    table_index = 0
    for child in body:
        if child.tag == f"{{{_WORD_NS}}}p":
            text = _word_text(child)
            if text:
                located.append((DocxParagraphLocator(paragraph_index=paragraph_index), text))
            paragraph_index += 1
            continue
        if child.tag != f"{{{_WORD_NS}}}tbl":
            continue
        if any(node.tag in _AMBIGUOUS_DOCX_TABLE_TAGS for node in child.iter()):
            raise IntakeValidationError(
                "UNSUPPORTED_DOCX_STRUCTURE",
                "merged or offset DOCX table cells cannot be represented by the "
                "frozen locator contract",
            )
        rows = child.findall(f"{{{_WORD_NS}}}tr")
        for row_index, row in enumerate(rows):
            cells = row.findall(f"{{{_WORD_NS}}}tc")
            for column_index, cell in enumerate(cells):
                if cell.find(f".//{{{_WORD_NS}}}tbl") is not None:
                    raise IntakeValidationError(
                        "UNSUPPORTED_DOCX_STRUCTURE",
                        "nested DOCX tables cannot be represented by the frozen locator contract",
                    )
                paragraph_text = [
                    _word_text(paragraph) for paragraph in cell.findall(f"{{{_WORD_NS}}}p")
                ]
                text = "\n".join(item for item in paragraph_text if item)
                if text:
                    located.append(
                        (
                            DocxTableCellLocator(
                                table_index=table_index,
                                row_index=row_index,
                                column_index=column_index,
                            ),
                            text,
                        )
                    )
        table_index += 1
    return located


def _xlsx_text_locations(content: bytes) -> list[tuple[SourceLocator, str]]:
    try:
        with zipfile.ZipFile(io.BytesIO(content), mode="r") as archive:
            workbook = _parse_xml(archive.read("xl/workbook.xml"))
            relationships = _workbook_relationships(
                _parse_xml(archive.read("xl/_rels/workbook.xml.rels"))
            )
            shared_strings = _shared_strings(archive)
            located: list[tuple[SourceLocator, str]] = []
            seen_sheets: set[str] = set()
            sheets = workbook.find(f"{{{_SHEET_NS}}}sheets")
            if sheets is None:
                raise IntakeValidationError("CORRUPT_XLSX", "XLSX workbook has no sheets")
            for sheet in sheets.findall(f"{{{_SHEET_NS}}}sheet"):
                sheet_name = sheet.get("name")
                relationship_id = sheet.get(f"{{{_OFFICE_REL_NS}}}id")
                if not sheet_name or sheet_name in seen_sheets or not relationship_id:
                    raise IntakeValidationError(
                        "CORRUPT_XLSX", "XLSX workbook has an invalid sheet declaration"
                    )
                seen_sheets.add(sheet_name)
                target = relationships.get(relationship_id)
                if target is None:
                    raise IntakeValidationError(
                        "CORRUPT_XLSX", "XLSX sheet relationship is missing"
                    )
                worksheet = _parse_xml(archive.read(target))
                located.extend(_worksheet_locations(worksheet, sheet_name, shared_strings))
            return located
    except IntakeValidationError:
        raise
    except (
        IndexError,
        KeyError,
        OSError,
        ValueError,
        zipfile.BadZipFile,
        ElementTree.ParseError,
    ) as exc:
        raise IntakeValidationError("CORRUPT_XLSX", "XLSX workbook XML cannot be read") from exc


def _workbook_relationships(root: ElementTree.Element) -> dict[str, str]:
    relationships: dict[str, str] = {}
    for item in root.findall(f"{{{_PACKAGE_REL_NS}}}Relationship"):
        relationship_id = item.get("Id")
        target = item.get("Target")
        if (
            not relationship_id
            or not target
            or item.get("TargetMode") == "External"
            or relationship_id in relationships
        ):
            continue
        if target.startswith("/"):
            resolved = posixpath.normpath(target.lstrip("/"))
        else:
            resolved = posixpath.normpath(posixpath.join("xl", target))
        if not resolved.startswith("xl/") or not _safe_member_name(resolved):
            raise IntakeValidationError("CORRUPT_XLSX", "XLSX contains an unsafe sheet target")
        relationships[relationship_id] = resolved
    return relationships


def _shared_strings(archive: zipfile.ZipFile) -> tuple[str, ...]:
    try:
        root = _parse_xml(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return ()
    return tuple(
        _spreadsheet_string_text(item)
        for item in root.findall(f"{{{_SHEET_NS}}}si")
    )


def _spreadsheet_string_text(item: ElementTree.Element) -> str:
    """Return logical cell text without phonetic-display annotations."""

    plain_text = item.findall(f"{{{_SHEET_NS}}}t")
    rich_runs = item.findall(f"{{{_SHEET_NS}}}r")
    if plain_text and rich_runs:
        raise IntakeValidationError(
            "CORRUPT_XLSX", "XLSX string mixes incompatible text encodings"
        )
    if len(plain_text) > 1:
        raise IntakeValidationError(
            "CORRUPT_XLSX", "XLSX string contains ambiguous plain-text nodes"
        )
    if plain_text:
        return plain_text[0].text or ""

    parts: list[str] = []
    for run in rich_runs:
        run_text = run.findall(f"{{{_SHEET_NS}}}t")
        if len(run_text) != 1:
            raise IntakeValidationError(
                "CORRUPT_XLSX", "XLSX rich-text run has no unique text value"
            )
        parts.append(run_text[0].text or "")
    return "".join(parts)


def _worksheet_locations(
    root: ElementTree.Element,
    sheet_name: str,
    shared_strings: tuple[str, ...],
) -> list[tuple[SourceLocator, str]]:
    located: list[tuple[SourceLocator, str]] = []
    seen_cells: set[str] = set()
    if root.find(f"{{{_SHEET_NS}}}mergeCells/{{{_SHEET_NS}}}mergeCell") is not None:
        raise IntakeValidationError(
            "UNSUPPORTED_XLSX_STRUCTURE",
            "merged XLSX cells cannot be represented by a single-cell locator",
        )
    sheet_data = root.find(f"{{{_SHEET_NS}}}sheetData")
    if sheet_data is None:
        return located
    for row in sheet_data.findall(f"{{{_SHEET_NS}}}row"):
        declared_row = row.get("r")
        for cell in row.findall(f"{{{_SHEET_NS}}}c"):
            reference = cell.get("r")
            if not reference or reference in seen_cells:
                raise IntakeValidationError("CORRUPT_XLSX", "XLSX contains an ambiguous cell")
            seen_cells.add(reference)
            row_index, column_index = _cell_indices(reference)
            if declared_row is not None and declared_row != str(row_index):
                raise IntakeValidationError(
                    "CORRUPT_XLSX", "XLSX row and cell coordinates disagree"
                )
            value = _xlsx_cell_text(cell, shared_strings)
            if value is None or value == "":
                continue
            located.append(
                (
                    XlsxCellLocator(
                        sheet_name=sheet_name,
                        row_index=row_index,
                        column_index=column_index,
                        cell_reference=reference,
                    ),
                    value,
                )
            )
    return located


def _cell_indices(reference: str) -> tuple[int, int]:
    match = _CELL_RE.fullmatch(reference)
    if match is None:
        raise IntakeValidationError("CORRUPT_XLSX", "XLSX cell coordinate is invalid")
    letters, row_text = match.groups()
    column = 0
    for character in letters:
        column = column * 26 + (ord(character) - ord("A") + 1)
    row = int(row_text)
    if column > 16_384 or row > 1_048_576:
        raise IntakeValidationError("CORRUPT_XLSX", "XLSX cell coordinate is out of range")
    return row, column


def _xlsx_cell_text(
    cell: ElementTree.Element,
    shared_strings: tuple[str, ...],
) -> str | None:
    formulas = cell.findall(f"{{{_SHEET_NS}}}f")
    if len(formulas) > 1:
        raise IntakeValidationError("CORRUPT_XLSX", "XLSX cell has ambiguous formulas")
    formula = formulas[0] if formulas else None
    if formula is not None:
        unsupported_formula = (
            bool(set(formula.attrib) - {"t"})
            or formula.get("t") not in {None, "normal"}
            or not formula.text
        )
        if unsupported_formula:
            raise IntakeValidationError(
                "UNSUPPORTED_XLSX_STRUCTURE",
                "shared, array, and other multi-cell XLSX formulas are not supported",
            )
        return f"={formula.text}"
    cell_type = cell.get("t")
    if cell_type == "inlineStr":
        inline_strings = cell.findall(f"{{{_SHEET_NS}}}is")
        if len(inline_strings) != 1:
            raise IntakeValidationError(
                "CORRUPT_XLSX", "XLSX inline string has no unique value"
            )
        return _spreadsheet_string_text(inline_strings[0])
    if cell_type not in {None, "b", "d", "e", "n", "s", "str"}:
        raise IntakeValidationError(
            "UNSUPPORTED_XLSX_STRUCTURE", "XLSX cell type is not supported"
        )
    raw = cell.findtext(f"{{{_SHEET_NS}}}v")
    if raw is None:
        return None
    if cell_type == "s":
        index = int(raw)
        if index < 0 or index >= len(shared_strings):
            raise IntakeValidationError("CORRUPT_XLSX", "XLSX shared-string index is out of range")
        return shared_strings[index]
    if cell_type == "b":
        if raw == "1":
            return "TRUE"
        if raw == "0":
            return "FALSE"
        raise IntakeValidationError("CORRUPT_XLSX", "XLSX boolean cell is invalid")
    return raw


def _segment_id(
    *,
    artifact_version_id: str,
    ordinal: int,
    locator: SourceLocator,
) -> str:
    locator_json = json.dumps(
        locator.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return str(
        uuid5(
            NAMESPACE_URL,
            f"owcopilot:n21:{artifact_version_id}:{ordinal}:{locator_json}",
        )
    )


__all__ = [
    "DOCX_MIME",
    "XLSX_MIME",
    "IntakePolicy",
    "IntakeValidationError",
    "UploadAccumulator",
    "ValidatedUpload",
    "parse_source_segments",
    "read_upload",
    "resolve_source_locator",
    "validate_upload_chunks",
    "verify_source_segment",
]
