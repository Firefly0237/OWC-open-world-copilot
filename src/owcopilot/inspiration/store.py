# 文件真源的灵感参考存储。
"""File-backed inspiration reference store."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..content.documents import binary_document_text
from ..content.encoding import decode_bytes
from ..content.injection import scan_for_injection
from ..content.lang import detect_language
from ..intake.contracts import SourceSegment
from ..llm.cache import Embedder
from ..retrieval.budget import estimate_tokens
from ..retrieval.chunking import (
    ChunkingPolicy,
    RetrievalDocument,
    build_retrieval_document,
)
from ..storage.atomic_file import atomic_write_bytes, atomic_write_text, read_json_file
from ..util import slugify
from .models import (
    REFERENCE_USES,
    USE_INSPIRATION,
    Provenance,
    ReferenceChunk,
    ReferenceIngestResult,
    ReferenceSource,
    parse_provenance,
)

if TYPE_CHECKING:
    from ..intake.parsers import ValidatedUpload


_LOCATABLE_ARTIFACT_EXTENSIONS = frozenset({".docx", ".pdf", ".xlsx"})


class ReferenceStore:
    def __init__(self, content_root: str | Path) -> None:
        self.root = Path(content_root) / "references"
        self.sources_dir = self.root / "sources"
        self.raw_dir = self.root / "raw"
        self.units_dir = self.root / "retrieval_units"
        self.artifacts_dir = self.root / "artifacts"

    def add_text(
        self,
        *,
        title: str,
        text: str,
        source_type: str = "uploaded_file",
        original_filename: str | None = None,
        allowed_uses: list[str] | None = None,
        provenance: str | Provenance = Provenance.THIRD_PARTY_REFERENCE,
        metadata: dict[str, str] | None = None,
    ) -> ReferenceIngestResult:
        clean = text.strip()
        if not clean:
            raise ValueError("reference text is empty")
        source_hash = hashlib.sha256(clean.encode("utf-8")).hexdigest()
        source_id = _unique_source_id(self.sources_dir, title, source_hash)
        profile = detect_language(clean)
        source = ReferenceSource(
            id=source_id,
            title=title.strip() or original_filename or source_id,
            source_type=source_type,
            original_filename=original_filename,
            allowed_uses=allowed_uses or [USE_INSPIRATION],
            provenance=parse_provenance(provenance),
            text_hash=source_hash,
            created_at=datetime.now(UTC).isoformat(),
            language=profile.label,
            languages=profile.labels,
            char_count=len(clean),
            metadata=metadata or {},
        )
        self.sources_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.raw_dir / f"{source.id}.txt", clean + "\n")
        # chunk_count records how many retrievable chunks a (possibly book-length) source produced,
        # so the UI can show that a whole novel was indexed rather than silently capped.
        chunks = self.chunks_for_source(source)
        source.chunk_count = len(chunks)
        # Scan untrusted reference text for prompt-injection before it can reach a grounding prompt.
        flagged = [chunk.id for chunk in chunks if scan_for_injection(chunk.body)]
        if flagged:
            source.metadata["injection_flagged"] = "true"
            source.metadata["quarantine_requested_uses"] = ",".join(source.allowed_uses)
            source.allowed_uses = []
            source.quarantine_status = "quarantined"
            source.quarantine_reason = "prompt_injection_detected"
        atomic_write_text(
            self.sources_dir / f"{source.id}.json",
            json.dumps(source.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        )
        return ReferenceIngestResult(
            source=source,
            chunks=chunks,
            indexed_count=0 if flagged else len(chunks),
            injection_flagged_chunks=flagged,
        )

    def add_source_segments(
        self,
        *,
        title: str,
        segments: Sequence[SourceSegment],
        policy: ChunkingPolicy,
        embedder: Embedder | None = None,
        original_filename: str | None = None,
        allowed_uses: list[str] | None = None,
        provenance: str | Provenance = Provenance.OWN_IP,
        metadata: dict[str, str] | None = None,
        artifact_upload: ValidatedUpload | None = None,
        token_counter: Callable[[str], int] = estimate_tokens,
        token_counter_id: str = "owcopilot-estimate-tokens-v1",
    ) -> ReferenceIngestResult:
        """Persist locatable intake segments as parent/child units in the reference RAG.

        This is the narrow adapter between team intake and the existing BM25/dense reference
        index.  It never promotes input into canon: callers still declare rights/allowed uses and
        the material remains in the physically separate reference library.
        """

        document = build_retrieval_document(
            segments,
            policy=policy,
            embedder=embedder,
            token_counter=token_counter,
            token_counter_id=token_counter_id,
        )
        ordered_segments = sorted(segments, key=lambda row: row.ordinal)
        raw_text = "\n".join(segment.text for segment in ordered_segments)
        source_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        source_id = _unique_source_id(self.sources_dir, title, source_hash)
        profile = detect_language(raw_text)
        source_metadata = {
            **(metadata or {}),
            "artifact_version_id": document.artifact_version_id,
            "retrieval_document_schema": document.schema_version,
            "chunk_policy_version": policy.version,
            "chunk_strategy": policy.strategy,
            "embedding_model_id": document.embedding_model_id or "none",
            "token_counter_id": document.token_counter_id,
        }
        if artifact_upload is not None:
            observed_hash = hashlib.sha256(artifact_upload.content_bytes).hexdigest()
            if observed_hash != artifact_upload.content_hash:
                raise ValueError("validated reference artifact bytes no longer match their hash")
            if artifact_upload.extension not in _LOCATABLE_ARTIFACT_EXTENSIONS:
                raise ValueError("reference artifact extension is not locator-preserving")
            if original_filename and original_filename != artifact_upload.original_filename:
                raise ValueError("reference filename does not match the validated artifact")
            source_metadata.update(
                {
                    "artifact_content_hash": artifact_upload.content_hash,
                    "artifact_byte_size": str(artifact_upload.byte_size),
                    "artifact_mime_type": artifact_upload.mime_type,
                    "artifact_extension": artifact_upload.extension,
                    # The UUID is the immutable source revision.  ``artifact_id`` (supplied by the
                    # upload action) is the logical document identity to which a future replacement
                    # endpoint may attach another version.
                    "artifact_revision": document.artifact_version_id,
                }
            )
        source = ReferenceSource(
            id=source_id,
            title=title.strip() or original_filename or source_id,
            source_type="intake_source_segments",
            original_filename=original_filename,
            allowed_uses=allowed_uses or [USE_INSPIRATION],
            provenance=parse_provenance(provenance),
            text_hash=source_hash,
            created_at=datetime.now(UTC).isoformat(),
            language=profile.label,
            languages=profile.labels,
            char_count=len(raw_text),
            metadata=source_metadata,
        )
        chunks = _chunks_from_retrieval_document(source, document)
        source.chunk_count = len(chunks)
        flagged = [chunk.id for chunk in chunks if scan_for_injection(chunk.body)]
        if flagged:
            source.metadata["injection_flagged"] = "true"
            source.metadata["quarantine_requested_uses"] = ",".join(source.allowed_uses)
            source.allowed_uses = []
            source.quarantine_status = "quarantined"
            source.quarantine_reason = "prompt_injection_detected"

        self.sources_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.units_dir.mkdir(parents=True, exist_ok=True)
        if artifact_upload is not None:
            artifact_path = self._artifact_path(
                document.artifact_version_id, artifact_upload.extension
            )
            if artifact_path.exists():
                raise ValueError("reference artifact revision already exists")
            atomic_write_bytes(artifact_path, artifact_upload.content_bytes)
        atomic_write_text(self.raw_dir / f"{source.id}.txt", raw_text + "\n")
        atomic_write_text(
            self.units_dir / f"{source.id}.json",
            json.dumps(document.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        )
        atomic_write_text(
            self.sources_dir / f"{source.id}.json",
            json.dumps(source.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        )
        return ReferenceIngestResult(
            source=source,
            chunks=chunks,
            indexed_count=0 if flagged else len(chunks),
            injection_flagged_chunks=flagged,
        )

    def list_sources(self) -> list[ReferenceSource]:
        if not self.sources_dir.exists():
            return []
        return [
            ReferenceSource.model_validate(read_json_file(path))
            for path in sorted(self.sources_dir.glob("*.json"))
        ]

    def load_text(self, source_id: str) -> str:
        path = self.raw_dir / f"{source_id}.txt"
        if not path.exists():
            raise KeyError(source_id)
        return path.read_text(encoding="utf-8")

    def load_chunks(self) -> list[ReferenceChunk]:
        chunks: list[ReferenceChunk] = []
        for source in self.list_sources():
            chunks.extend(self.chunks_for_source(source))
        return chunks

    def chunks_for_source(self, source: ReferenceSource) -> list[ReferenceChunk]:
        retrieval_document = self.load_retrieval_document(source.id)
        if retrieval_document is not None:
            expected_artifact = source.metadata.get("artifact_version_id")
            if expected_artifact != retrieval_document.artifact_version_id:
                raise ValueError(
                    f"retrieval sidecar for {source.id!r} does not match source artifact identity"
                )
            return _chunks_from_retrieval_document(source, retrieval_document)
        text = self.load_text(source.id)
        chunks: list[ReferenceChunk] = []
        for index, body in enumerate(_chunk_text(text)):
            chunks.append(
                ReferenceChunk(
                    # 5-digit zero-pad so chunk ids still sort monotonically for a whole novel
                    # (a 2M-char book is ~1100 chunks, well past the old 3-digit width).
                    id=f"{source.id}_chunk_{index + 1:05d}",
                    source_id=source.id,
                    chunk_index=index,
                    title=f"{source.title} #{index + 1}",
                    body=body,
                    metadata={"allowed_uses": ",".join(source.allowed_uses)},
                )
            )
        return chunks

    def load_retrieval_document(self, source_id: str) -> RetrievalDocument | None:
        """Load the versioned parent/child sidecar, or ``None`` for legacy text references."""

        path = self.units_dir / f"{source_id}.json"
        if not path.exists():
            return None
        return RetrievalDocument.model_validate(read_json_file(path))

    def load_artifact(self, source_id: str) -> bytes:
        """Load and hash-verify the exact binary artifact behind a locatable reference."""

        source_path = self.sources_dir / f"{source_id}.json"
        if not source_path.exists():
            raise KeyError(source_id)
        source = ReferenceSource.model_validate(read_json_file(source_path))
        version = source.metadata.get("artifact_version_id", "")
        extension = source.metadata.get("artifact_extension", "")
        expected_hash = source.metadata.get("artifact_content_hash", "")
        if not version or not extension or not expected_hash:
            raise ValueError(f"reference {source_id!r} has no immutable binary artifact")
        payload = self._artifact_path(version, extension).read_bytes()
        if hashlib.sha256(payload).hexdigest() != expected_hash:
            raise ValueError(f"reference artifact for {source_id!r} failed hash verification")
        return payload

    def sync_index(self, sqlite_store: Any) -> None:
        # Quarantined material remains visible in the library for human review but is absent from
        # both BM25 and dense tables, so no drafting/RAG caller can retrieve it accidentally.
        sources = [
            source
            for source in self.list_sources()
            if source.quarantine_status != "quarantined"
        ]
        chunks = [chunk for source in sources for chunk in self.chunks_for_source(source)]
        sqlite_store.replace_reference_index(sources, chunks)

    def approve_quarantined(
        self, source_id: str, *, allowed_uses: list[str], note: str = ""
    ) -> ReferenceSource:
        """Explicitly release one flagged source after human review and persist that decision."""
        uses = list(dict.fromkeys(use.strip() for use in allowed_uses if use.strip()))
        if not uses or any(use not in REFERENCE_USES for use in uses):
            raise ValueError("allowed_uses must contain inspiration and/or drafting")
        path = self.sources_dir / f"{source_id}.json"
        if not path.exists():
            raise KeyError(source_id)
        source = ReferenceSource.model_validate(read_json_file(path))
        if source.quarantine_status != "quarantined":
            raise ValueError(f"reference {source_id!r} is not quarantined")
        source.quarantine_status = "approved"
        source.quarantine_approved_at = datetime.now(UTC).isoformat()
        source.allowed_uses = uses
        if note.strip():
            source.metadata["quarantine_approval_note"] = note.strip()
        atomic_write_text(
            path,
            json.dumps(source.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        )
        return source

    def remove(self, source_id: str) -> None:
        """Physically delete a reference source's metadata + raw text files.

        References are not canon and carry no tombstone, so removal is a straight file delete —
        the caller is responsible for calling :meth:`sync_index` afterwards to prune the sqlite
        mirror (``replace_reference_index`` already drops any source no longer on disk). Raises
        ``KeyError`` when the id does not exist, mirroring :meth:`load_text`."""
        meta_path = self.sources_dir / f"{source_id}.json"
        if not meta_path.exists():
            raise KeyError(source_id)
        source = ReferenceSource.model_validate(read_json_file(meta_path))
        version = source.metadata.get("artifact_version_id", "")
        extension = source.metadata.get("artifact_extension", "")
        artifact_path = self._artifact_path(version, extension) if version and extension else None
        meta_path.unlink()
        (self.raw_dir / f"{source_id}.txt").unlink(missing_ok=True)
        (self.units_dir / f"{source_id}.json").unlink(missing_ok=True)
        if artifact_path is not None:
            artifact_path.unlink(missing_ok=True)

    def _artifact_path(self, artifact_version_id: str, extension: str) -> Path:
        if extension not in _LOCATABLE_ARTIFACT_EXTENSIONS:
            raise ValueError("reference artifact extension is not locator-preserving")
        # SourceSegment validation already guarantees a canonical UUID on writes.  Re-check the
        # character shape on reads so damaged metadata cannot turn removal into path traversal.
        if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", artifact_version_id):
            raise ValueError("reference artifact version is not a canonical UUID")
        return self.artifacts_dir / f"{artifact_version_id}{extension}"


def decode_reference_bytes(data: bytes, filename: str) -> str:
    binary = binary_document_text(data, filename)
    if binary is not None:
        return binary
    suffix = Path(filename).suffix.lower()
    text = decode_bytes(data)
    if suffix == ".json":
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text
        return json.dumps(parsed, ensure_ascii=False, indent=2)
    if suffix == ".csv":
        try:
            rows = csv.reader(io.StringIO(text))
            return "\n".join(" | ".join(cell.strip() for cell in row) for row in rows)
        except csv.Error:
            return text
    return text


def _chunk_text(text: str, *, max_chars: int = 1800, overlap_chars: int = 160) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs or [text.strip()]:
        if len(current) + len(paragraph) + 2 <= max_chars:
            current = f"{current}\n\n{paragraph}".strip()
            continue
        if current:
            chunks.append(current)
        while len(paragraph) > max_chars:
            chunks.append(paragraph[:max_chars].strip())
            paragraph = paragraph[max(0, max_chars - overlap_chars) :].strip()
        current = paragraph
    if current:
        chunks.append(current)
    return chunks


def _chunks_from_retrieval_document(
    source: ReferenceSource, document: RetrievalDocument
) -> list[ReferenceChunk]:
    parent_by_id = {parent.parent_id: parent for parent in document.parents}
    chunks: list[ReferenceChunk] = []
    for unit in document.units:
        parent = parent_by_id[unit.parent_id]
        chunks.append(
            ReferenceChunk(
                id=f"{source.id}_unit_{unit.chunk_index + 1:05d}_{unit.unit_id[:8]}",
                source_id=source.id,
                chunk_index=unit.chunk_index,
                title=f"{source.title} #{unit.chunk_index + 1}",
                body=unit.text,
                metadata={
                    "artifact_version_id": unit.artifact_version_id,
                    "unit_id": unit.unit_id,
                    "parent_id": unit.parent_id,
                    "parent_structure_kind": parent.structure_kind,
                    "segment_ids_json": json.dumps(unit.segment_ids, ensure_ascii=False),
                    "locators_json": json.dumps(
                        unit.locators, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ),
                    "locator_start_json": json.dumps(
                        parent.locator_start,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "locator_end_json": json.dumps(
                        parent.locator_end,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "policy_version": unit.policy_version,
                    "chunk_strategy": unit.strategy,
                    "embedding_model_id": unit.embedding_model_id or "none",
                    "token_counter_id": unit.token_counter_id,
                    "token_count": str(unit.token_count),
                    "oversized": str(unit.oversized).lower(),
                    "overlap_from_previous_json": json.dumps(unit.overlap_from_previous),
                },
            )
        )
    return chunks


def _unique_source_id(path: Path, title: str, text_hash: str) -> str:
    stem = _slug(title) or "reference"
    base = f"ref_{stem[:48]}_{text_hash[:8]}"
    candidate = base
    index = 2
    while (path / f"{candidate}.json").exists():
        candidate = f"{base}_{index}"
        index += 1
    return candidate


def _slug(value: str) -> str:
    return slugify(value)
