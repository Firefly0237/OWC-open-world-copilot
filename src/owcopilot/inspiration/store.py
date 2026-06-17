"""File-backed inspiration reference store."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..content.documents import binary_document_text
from ..content.encoding import decode_bytes
from ..content.injection import scan_for_injection
from ..content.lang import detect_language
from ..util import slugify
from .models import ReferenceChunk, ReferenceIngestResult, ReferenceSource


class ReferenceStore:
    def __init__(self, content_root: str | Path) -> None:
        self.root = Path(content_root) / "references"
        self.sources_dir = self.root / "sources"
        self.raw_dir = self.root / "raw"

    def add_text(
        self,
        *,
        title: str,
        text: str,
        source_type: str = "uploaded_file",
        original_filename: str | None = None,
        allowed_uses: list[str] | None = None,
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
            allowed_uses=allowed_uses or ["inspiration"],
            text_hash=source_hash,
            created_at=datetime.now(UTC).isoformat(),
            language=profile.label,
            languages=profile.labels,
            char_count=len(clean),
            metadata=metadata or {},
        )
        self.sources_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        (self.raw_dir / f"{source.id}.txt").write_text(clean + "\n", encoding="utf-8")
        # chunk_count records how many retrievable chunks a (possibly book-length) source produced,
        # so the UI can show that a whole novel was indexed rather than silently capped.
        chunks = self.chunks_for_source(source)
        source.chunk_count = len(chunks)
        # Scan untrusted reference text for prompt-injection before it can reach a grounding prompt.
        flagged = [chunk.id for chunk in chunks if scan_for_injection(chunk.body)]
        if flagged:
            source.metadata["injection_flagged"] = "true"
        (self.sources_dir / f"{source.id}.json").write_text(
            json.dumps(source.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return ReferenceIngestResult(
            source=source,
            chunks=chunks,
            indexed_count=len(chunks),
            injection_flagged_chunks=flagged,
        )

    def list_sources(self) -> list[ReferenceSource]:
        if not self.sources_dir.exists():
            return []
        return [
            ReferenceSource.model_validate_json(path.read_text(encoding="utf-8"))
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

    def sync_index(self, sqlite_store: Any) -> None:
        sqlite_store.replace_reference_index(self.list_sources(), self.load_chunks())


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
