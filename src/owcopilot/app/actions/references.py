# 灵感参考库：新增/列出/检索参考素材。
# Inspiration reference store: add, list, search.

"""references actions — split from the former ``app/actions.py`` monolith by domain.

Behaviour is unchanged; only the module boundary is new. The ``app.actions`` package
re-exports every action so existing imports keep working."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from uuid import uuid4

from ...inspiration import decode_reference_bytes
from ...inspiration.lineage import objects_citing_chunks
from ...inspiration.models import Provenance, parse_provenance
from ...intake import (
    DOCX_MIME,
    PDF_MIME,
    XLSX_MIME,
    IntakePolicy,
    IntakeValidationError,
    parse_source_segments,
    validate_upload_chunks,
)
from ...retrieval import ChunkingPolicy
from ...storage.world_lock import serialized_world_write
from .._common import deterministic_cost_budget as _deterministic_cost_budget
from .._common import open_project as _project

_STRUCTURED_MIME_TYPES = {".docx": DOCX_MIME, ".pdf": PDF_MIME, ".xlsx": XLSX_MIME}
_LEGACY_TEXT_EXTENSIONS = frozenset(
    {".txt", ".md", ".markdown", ".json", ".csv", ".yaml", ".yml", ".xml", ".html"}
)
_FLATTENED_BINARY_EXTENSIONS = frozenset({".epub"})
_REFERENCE_STRUCTURE_POLICY = ChunkingPolicy(
    version="reference-structure-locators-v1",
    strategy="structure",
    min_tokens=40,
    target_tokens=220,
    max_tokens=360,
    overlap_tokens=40,
)


@serialized_world_write
def add_reference_action(
    content_root: str | Path,
    *,
    title: str,
    text: str,
    sqlite_path: str | None = None,
    source_type: str = "uploaded_file",
    original_filename: str | None = None,
    allowed_uses: list[str] | None = None,
    provenance: str | Provenance = Provenance.THIRD_PARTY_REFERENCE,
) -> dict[str, Any]:
    """Ingest reference material into the inspiration library.

    Unlike the canon intake (extraction), this door takes all three provenances — the library is
    physically separate from canon and is only ever read as retrieval grounding, so third-party
    material is exactly what it is for. The default is the most conservative value."""
    with _project(content_root, sqlite_path) as project:
        result = project.reference_store.add_text(
            title=title,
            text=text,
            source_type=source_type,
            original_filename=original_filename,
            allowed_uses=allowed_uses,
            provenance=parse_provenance(provenance),
        )
        project.reference_store.sync_index(project.sqlite_store)
        return {
            "source": result.source.model_dump(mode="json"),
            "chunks": [chunk.model_dump(mode="json") for chunk in result.chunks],
            "indexed_count": result.indexed_count,
            # Untrusted reference text that matched a prompt-injection pattern, surfaced so the
            # human can make a risk call before it grounds a generation prompt (OWASP LLM01).
            "injection_flagged_chunks": result.injection_flagged_chunks,
            "cost_budget": _deterministic_cost_budget("reference_add"),
        }


@serialized_world_write
def approve_reference_action(
    content_root: str | Path,
    *,
    source_id: str,
    allowed_uses: list[str],
    note: str = "",
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Human review gate that explicitly releases a prompt-injection-quarantined source."""
    with _project(content_root, sqlite_path) as project:
        try:
            source = project.reference_store.approve_quarantined(
                source_id, allowed_uses=allowed_uses, note=note
            )
        except KeyError:
            raise ValueError(f"素材不存在：{source_id}。") from None
        project.reference_store.sync_index(project.sqlite_store)
        return {
            "source": source.model_dump(mode="json"),
            "indexed_count": source.chunk_count,
            "cost_budget": _deterministic_cost_budget("reference_approve"),
        }


@serialized_world_write
def upload_reference_action(
    content_root: str | Path,
    *,
    title: str,
    content: bytes,
    original_filename: str,
    mime_type: str | None = None,
    sqlite_path: str | None = None,
    allowed_uses: list[str] | None = None,
    provenance: str | Provenance = Provenance.THIRD_PARTY_REFERENCE,
) -> dict[str, Any]:
    """Ingest uploaded bytes without flattening formats that have exact source locators.

    PDF/DOCX/XLSX use the same fail-closed parser as team intake and persist both the immutable
    artifact revision and its locator-bearing parent/child retrieval sidecar. Plain-text formats
    retain the existing compatibility path. EPUB remains readable, but is explicitly reported as
    degraded because that extractor cannot yet produce stable chapter/DOM locators.
    """

    if type(content) is not bytes or not content:
        raise IntakeValidationError("EMPTY_UPLOAD", "upload body is empty")
    safe_name = Path(original_filename).name
    if not safe_name or safe_name != original_filename or safe_name.startswith("."):
        raise IntakeValidationError("INVALID_FILENAME", "upload filename must be a basename")
    suffix = Path(safe_name).suffix.casefold()
    with _project(content_root, sqlite_path) as project:
        expected_mime = _STRUCTURED_MIME_TYPES.get(suffix)
        if expected_mime is not None:
            intake_policy = IntakePolicy.from_env()
            upload = validate_upload_chunks(
                (content,),
                original_filename=safe_name,
                mime_type=mime_type or expected_mime,
                policy=intake_policy,
            )
            artifact_id = str(uuid4())
            artifact_version_id = str(uuid4())
            segments = parse_source_segments(
                upload,
                artifact_version_id=artifact_version_id,
                policy=intake_policy,
            )
            result = project.reference_store.add_source_segments(
                title=title,
                segments=segments,
                policy=_REFERENCE_STRUCTURE_POLICY,
                original_filename=safe_name,
                allowed_uses=allowed_uses,
                provenance=parse_provenance(provenance),
                metadata={
                    "artifact_id": artifact_id,
                    "artifact_identity_kind": "logical_artifact_with_immutable_versions",
                },
                artifact_upload=upload,
            )
            project.reference_store.sync_index(project.sqlite_store)
            return {
                "source": result.source.model_dump(mode="json"),
                "chunks": [chunk.model_dump(mode="json") for chunk in result.chunks],
                "indexed_count": result.indexed_count,
                "injection_flagged_chunks": result.injection_flagged_chunks,
                "ingestion_mode": "structured_locator",
                "artifact": {
                    "artifact_id": artifact_id,
                    "artifact_version_id": artifact_version_id,
                    "content_hash": upload.content_hash,
                    "byte_size": upload.byte_size,
                    "mime_type": upload.mime_type,
                    "raw_preserved": True,
                    "locators_preserved": True,
                },
                "degraded": False,
                "degradation_reasons": [],
                "cost_budget": _deterministic_cost_budget("reference_upload_structured"),
            }

        if suffix not in _LEGACY_TEXT_EXTENSIONS | _FLATTENED_BINARY_EXTENSIONS:
            raise IntakeValidationError(
                "UNSUPPORTED_REFERENCE_FORMAT",
                "reference upload format is not supported",
                status_code=415,
            )
        text = decode_reference_bytes(content, safe_name)
        flattened = suffix in _FLATTENED_BINARY_EXTENSIONS
        result = project.reference_store.add_text(
            title=title,
            text=text,
            source_type="uploaded_file",
            original_filename=safe_name,
            allowed_uses=allowed_uses,
            provenance=parse_provenance(provenance),
            metadata={
                "upload_content_hash": hashlib.sha256(content).hexdigest(),
                "upload_byte_size": str(len(content)),
                "upload_ingestion_mode": "flattened_binary" if flattened else "legacy_text",
            },
        )
        project.reference_store.sync_index(project.sqlite_store)
        return {
            "source": result.source.model_dump(mode="json"),
            "chunks": [chunk.model_dump(mode="json") for chunk in result.chunks],
            "indexed_count": result.indexed_count,
            "injection_flagged_chunks": result.injection_flagged_chunks,
            "ingestion_mode": "flattened_binary" if flattened else "legacy_text",
            "artifact": None,
            "degraded": flattened,
            "degradation_reasons": (
                ["epub_locator_preservation_unavailable"] if flattened else []
            ),
            "cost_budget": _deterministic_cost_budget("reference_upload_legacy"),
        }


@serialized_world_write
def remove_reference_action(
    content_root: str | Path,
    *,
    source_id: str,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Physically delete a reference source (N2.3 / B6): a reference is not canon, so removal is a
    straight file delete, never a tombstone. Non-blocking: if an already-adopted canon object's
    ``metadata.inspiration_refs`` still cites one of this source's chunks, the deletion still goes
    through — the citation just goes stale — but the caller gets a warning list naming which
    objects now have a dangling reference (the same check N2.4's readiness dangling-ref pass runs
    for the whole world; here it is scoped to just the chunks about to disappear)."""
    with _project(content_root, sqlite_path) as project:
        sources = {source.id: source for source in project.reference_store.list_sources()}
        source = sources.get(source_id)
        if source is None:
            raise ValueError(
                f"素材不存在：{source_id}。它可能已被删除，或 id 输入有误——"
                "请到「灵感库」列表核对当前的素材 id。"
            )
        chunk_ids = {chunk.id for chunk in project.reference_store.chunks_for_source(source)}
        warnings = sorted(objects_citing_chunks(project.bundle, chunk_ids))
        project.reference_store.remove(source_id)
        project.reference_store.sync_index(project.sqlite_store)
        return {
            "removed": source_id,
            "dangling_ref_warnings": warnings,
            "cost_budget": _deterministic_cost_budget("reference_remove"),
        }


def list_references_action(
    content_root: str | Path,
    *,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        sources = project.reference_store.list_sources()
        return {
            "count": len(sources),
            "sources": [source.model_dump(mode="json") for source in sources],
            "cost_budget": _deterministic_cost_budget("reference_list"),
        }


def search_references_action(
    content_root: str | Path,
    *,
    query: str,
    sqlite_path: str | None = None,
    budget_tokens: int = 1000,
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        pack = project.reference_context_builder.build(query, budget_tokens=budget_tokens)
        return {
            "query": query,
            "refs": pack.refs,
            "hits": [hit.model_dump(mode="json") for hit in pack.hits],
            "degraded": pack.degraded,
            "degradation_reasons": pack.degradation_reasons,
            "cost_budget": _deterministic_cost_budget("reference_search"),
        }
