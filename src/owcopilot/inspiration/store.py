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
        source = ReferenceSource(
            id=source_id,
            title=title.strip() or original_filename or source_id,
            source_type=source_type,
            original_filename=original_filename,
            allowed_uses=allowed_uses or ["inspiration"],
            text_hash=source_hash,
            created_at=datetime.now(UTC).isoformat(),
            metadata=metadata or {},
        )
        self.sources_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        (self.sources_dir / f"{source.id}.json").write_text(
            json.dumps(source.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (self.raw_dir / f"{source.id}.txt").write_text(clean + "\n", encoding="utf-8")
        return ReferenceIngestResult(
            source=source,
            chunks=self.chunks_for_source(source),
            indexed_count=len(self.chunks_for_source(source)),
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
                    id=f"{source.id}_chunk_{index + 1:03d}",
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
    suffix = Path(filename).suffix.lower()
    text = _decode_text(data)
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


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


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
    text = value.strip().lower()
    text = re.sub(r"[^a-z0-9\u3400-\u9fff]+", "_", text)
    return text.strip("_")
