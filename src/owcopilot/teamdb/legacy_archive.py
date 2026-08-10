"""Deterministic, restorable archive and cutover preflight for legacy state."""

from __future__ import annotations

import base64
import ctypes
import errno
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from ..contracts.hashing import canonical_json, canonical_json_from_text

RecordClass = Literal["blocker", "archive-only", "unknown"]

_SCHEMA: Final = "owcopilot/legacy-archive/v1"
_ARCHIVE_NAMESPACE: Final = uuid.UUID("a1897baa-da6c-54df-a1c7-4b3d340487bc")
_LABEL_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z", flags=re.ASCII)
_REPARSE_POINT: Final = 0x400
_ZIP_TIMESTAMP: Final = (1980, 1, 1, 0, 0, 0)
_CHUNK: Final = 1024 * 1024


class LegacyArchiveError(RuntimeError):
    """The legacy snapshot could not be proven stable and restorable."""


class CutoverBlockedError(LegacyArchiveError):
    """Preflight found live work or unknown records that cannot be abandoned."""


@dataclass(frozen=True, slots=True)
class ArchiveFileEntry:
    relative_path: str
    source_kind: Literal["content", "sqlite"]
    sha256: str
    byte_size: int

    def to_dict(self) -> dict[str, object]:
        return {
            "byte_size": self.byte_size,
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "source_kind": self.source_kind,
        }


@dataclass(frozen=True, slots=True)
class SQLiteRecordDisposition:
    database_label: str
    table: str
    record_sha256: str
    primary_key_json: str
    record_class: RecordClass
    disposition: str
    source_status: str
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "database_label": self.database_label,
            "disposition": self.disposition,
            "primary_key_json": self.primary_key_json,
            "reason": self.reason,
            "record_class": self.record_class,
            "record_sha256": self.record_sha256,
            "source_status": self.source_status,
            "table": self.table,
        }


@dataclass(frozen=True, slots=True)
class SQLiteTableSummary:
    database_label: str
    table: str
    policy: str
    row_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "database_label": self.database_label,
            "policy": self.policy,
            "row_count": self.row_count,
            "table": self.table,
        }


@dataclass(frozen=True, slots=True)
class LegacyArchiveManifest:
    directories: tuple[str, ...]
    files: tuple[ArchiveFileEntry, ...]
    sqlite_records: tuple[SQLiteRecordDisposition, ...]
    sqlite_tables: tuple[SQLiteTableSummary, ...]

    @property
    def blockers(self) -> tuple[SQLiteRecordDisposition, ...]:
        return tuple(row for row in self.sqlite_records if row.record_class != "archive-only")

    @property
    def cutover_safe(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict[str, object]:
        return {
            "cutover_safe": self.cutover_safe,
            "directories": list(self.directories),
            "files": [entry.to_dict() for entry in self.files],
            "schema": _SCHEMA,
            "sqlite_records": [row.to_dict() for row in self.sqlite_records],
            "sqlite_tables": [table.to_dict() for table in self.sqlite_tables],
        }

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @property
    def source_manifest_hash(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    def require_cutover_safe(self) -> None:
        if self.cutover_safe:
            return
        sample = ", ".join(
            f"{row.database_label}:{row.table}:{row.source_status or '<none>'}"
            for row in self.blockers[:5]
        )
        raise CutoverBlockedError(
            f"legacy cutover blocked by {len(self.blockers)} active/unknown record(s): {sample}"
        )


@dataclass(frozen=True, slots=True)
class LegacyArchivePackage:
    archive_path: Path
    archive_id: str
    archive_sha256: str
    byte_size: int
    manifest: LegacyArchiveManifest

    def require_cutover_safe(self) -> None:
        self.manifest.require_cutover_safe()


@dataclass(frozen=True, slots=True)
class _FsEntry:
    relative_path: str
    is_dir: bool


# Tables that are projections/caches and can be regenerated from an accepted
# source in their owning future node.  Their bytes are still archived, but rows
# do not need individual retention classifications.
_REBUILDABLE_TABLES: Final = {
    "content_index",
    "content_vectors",
    "reference_vectors",
    "community_vectors",
    "graph_edges",
    "content_fts",
    "reference_fts",
    "community_reports",
}

# These records are historical/source material whose current N1.2 team API is
# explicitly not claiming to query.  The exact database bytes and one
# disposition per row remain in the archive registry.
_ARCHIVE_ONLY_TABLES: Final = {
    "audit_runs",
    "telemetry",
    "audit_log",
    "lessons",
    "agent_runs",
    "agent_run_embeddings",
    "reflections",
    "version_registry",
}

# These identity/control-plane rows cannot be silently replaced by newly
# supplied bootstrap credentials.
_MUST_MIGRATE_TABLES: Final = {
    "tenants",
    "users",
    "memberships",
    "reference_sources",
    "reference_chunks",
}

# Workflow records are classified by their real state.  Unknown spellings are
# unknown, not optimistically terminal.
_WORKFLOW_TABLES: Final = {
    "issues": "status",
    "patches": "status",
    "review_items": "status",
    "agent_messages": "status",
}
_ACTIVE_STATES: Final = {
    "active",
    "awaiting_review",
    "building",
    "claimed",
    "in_progress",
    "open",
    "pending",
    "pending_review",
    "proposed",
    "queued",
    "running",
    "started",
}
_TERMINAL_STATES: Final = {
    "accepted",
    "applied",
    "archived",
    "cancelled",
    "closed",
    "completed",
    "deleted",
    "done",
    "failed",
    "finished",
    "fixed",
    "rejected",
    "rolled_back",
    "suppressed",
}


def create_legacy_archive(
    *,
    content_root: str | Path,
    sqlite_databases: Mapping[str, str | Path],
    archive_path: str | Path,
) -> LegacyArchivePackage:
    """Capture stable bytes, classifications, and a deterministic ZIP.

    The content tree and every SQLite database must be quiescent.  Symlinks,
    Windows junction/reparse points, special files, path escape, SQLite WAL or
    journal companions, and any before/after scan drift are rejected.
    """

    root = _resolve_source_directory(content_root, label="content root")
    target = Path(archive_path).resolve()
    if _is_within(target, root):
        raise LegacyArchiveError("archive_path must be outside content_root")
    if target.exists() and not _is_regular_file(target):
        raise LegacyArchiveError(f"archive target is not a regular file: {target}")

    normalized_sqlite = _normalize_sqlite_inputs(sqlite_databases)
    for label, source in normalized_sqlite.items():
        if target == source:
            raise LegacyArchiveError(f"archive target aliases SQLite input {label!r}")
        _reject_sqlite_companions(source)

    target.parent.mkdir(parents=True, exist_ok=True)
    before = _scan_tree(root)
    staging_parent = target.parent
    with tempfile.TemporaryDirectory(prefix=".legacy-archive-stage-", dir=staging_parent) as raw:
        stage = Path(raw)
        staged_content = stage / "content"
        staged_content.mkdir()
        directories = ["content"]
        file_entries: list[ArchiveFileEntry] = []

        for entry in before:
            destination = staged_content / Path(entry.relative_path)
            if entry.is_dir:
                destination.mkdir(parents=True, exist_ok=True)
                directories.append(f"content/{entry.relative_path}")
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest, size = _copy_stable_file(root / Path(entry.relative_path), destination)
            file_entries.append(
                ArchiveFileEntry(
                    relative_path=f"content/{entry.relative_path}",
                    source_kind="content",
                    sha256=digest,
                    byte_size=size,
                )
            )

        sqlite_records: list[SQLiteRecordDisposition] = []
        sqlite_tables: list[SQLiteTableSummary] = []
        sqlite_dir = stage / "sqlite"
        sqlite_dir.mkdir()
        directories.append("sqlite")
        for label, source in normalized_sqlite.items():
            archive_name = f"{label}.sqlite3"
            staged = sqlite_dir / archive_name
            digest, size = _copy_stable_file(source, staged)
            file_entries.append(
                ArchiveFileEntry(
                    relative_path=f"sqlite/{archive_name}",
                    source_kind="sqlite",
                    sha256=digest,
                    byte_size=size,
                )
            )
            records, tables = classify_legacy_sqlite(staged, database_label=label)
            sqlite_records.extend(records)
            sqlite_tables.extend(tables)

        after = _scan_tree(root)
        if before != after:
            raise LegacyArchiveError("content tree changed while the archive was being captured")
        for archive_entry in file_entries:
            if archive_entry.source_kind != "content":
                continue
            source_relative = archive_entry.relative_path.removeprefix("content/")
            if _sha256_file(root / Path(source_relative)) != archive_entry.sha256:
                raise LegacyArchiveError(
                    f"content file changed during capture: {archive_entry.relative_path}"
                )
        for label, source in normalized_sqlite.items():
            _reject_sqlite_companions(source)
            staged = sqlite_dir / f"{label}.sqlite3"
            if _sha256_file(source) != _sha256_file(staged):
                raise LegacyArchiveError(f"SQLite database changed during capture: {label}")

        manifest = LegacyArchiveManifest(
            directories=tuple(sorted(set(directories))),
            files=tuple(sorted(file_entries, key=lambda item: item.relative_path)),
            sqlite_records=tuple(
                sorted(
                    sqlite_records,
                    key=lambda item: (
                        item.database_label,
                        item.table,
                        item.primary_key_json,
                        item.record_sha256,
                    ),
                )
            ),
            sqlite_tables=tuple(
                sorted(sqlite_tables, key=lambda item: (item.database_label, item.table))
            ),
        )
        manifest_path = stage / "legacy-manifest.json"
        manifest_path.write_text(manifest.to_json(), encoding="utf-8", newline="")

        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            _write_deterministic_zip(
                temporary,
                stage=stage,
                directories=manifest.directories,
                files=("legacy-manifest.json", *(entry.relative_path for entry in manifest.files)),
            )
            # Verify the exact temporary artifact before publishing it.
            _read_and_verify_archive(temporary)
            _publish_file_noreplace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    archive_sha = _sha256_file(target)
    archive_id = str(uuid.uuid5(_ARCHIVE_NAMESPACE, manifest.source_manifest_hash))
    return LegacyArchivePackage(
        archive_path=target,
        archive_id=archive_id,
        archive_sha256=archive_sha,
        byte_size=target.stat().st_size,
        manifest=manifest,
    )


def classify_legacy_sqlite(
    database_path: str | Path,
    *,
    database_label: str,
) -> tuple[tuple[SQLiteRecordDisposition, ...], tuple[SQLiteTableSummary, ...]]:
    """Classify every non-rebuildable SQLite row without mutating the DB."""

    path = _resolve_source_file(database_path)
    _validate_label(database_label)
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        result = connection.execute("PRAGMA quick_check").fetchall()
        if [str(row[0]) for row in result] != ["ok"]:
            raise LegacyArchiveError(f"SQLite quick_check failed for {database_label}: {result!r}")
        master = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        table_types = {str(row[1]): str(row[2]) for row in connection.execute("PRAGMA table_list")}
        rebuildable_shadows = _rebuildable_virtual_shadows(master, table_types)
        records: list[SQLiteRecordDisposition] = []
        summaries: list[SQLiteTableSummary] = []
        for master_row in master:
            table = str(master_row["name"])
            if table.startswith("sqlite_") or table in rebuildable_shadows:
                continue
            if table_types.get(table) == "virtual" and table in _REBUILDABLE_TABLES:
                continue
            quoted = _quote_identifier(table)
            rows = connection.execute(f"SELECT * FROM {quoted}").fetchall()  # noqa: S608
            policy = _table_policy(table)
            summaries.append(
                SQLiteTableSummary(
                    database_label=database_label,
                    table=table,
                    policy=policy,
                    row_count=len(rows),
                )
            )
            if policy == "rebuildable":
                continue
            columns = [str(item[1]) for item in connection.execute(f"PRAGMA table_info({quoted})")]
            pk_columns = [
                str(item[1])
                for item in sorted(
                    connection.execute(f"PRAGMA table_info({quoted})").fetchall(),
                    key=lambda item: int(item[5]) if int(item[5]) else 10_000,
                )
                if int(item[5])
            ]
            for row in rows:
                payload = {column: _json_value(row[column]) for column in columns}
                record_json = canonical_json(payload)
                primary_key = {column: _json_value(row[column]) for column in pk_columns} or {
                    "record_sha256": hashlib.sha256(record_json.encode("utf-8")).hexdigest()
                }
                record_class, disposition, source_status, reason = _classify_record(
                    table, row, policy
                )
                records.append(
                    SQLiteRecordDisposition(
                        database_label=database_label,
                        table=table,
                        record_sha256=hashlib.sha256(record_json.encode("utf-8")).hexdigest(),
                        primary_key_json=canonical_json(primary_key),
                        record_class=record_class,
                        disposition=disposition,
                        source_status=source_status,
                        reason=reason,
                    )
                )
        return (
            tuple(
                sorted(
                    records,
                    key=lambda item: (
                        item.table,
                        item.primary_key_json,
                        item.record_sha256,
                    ),
                )
            ),
            tuple(sorted(summaries, key=lambda item: item.table)),
        )
    except sqlite3.DatabaseError as exc:
        raise LegacyArchiveError(f"cannot inspect SQLite database {database_label}: {exc}") from exc
    finally:
        connection.close()


def restore_legacy_archive(
    archive_path: str | Path,
    destination: str | Path,
    *,
    expected_archive_sha256: str | None = None,
) -> LegacyArchiveManifest:
    """Restore into a new directory and verify every manifest checksum."""

    archive = Path(archive_path).resolve()
    target = Path(destination).resolve()
    if not _is_regular_file(archive):
        raise LegacyArchiveError(f"archive is not a regular file: {archive}")
    observed_archive_hash = _sha256_file(archive)
    if expected_archive_sha256 is not None and observed_archive_hash != expected_archive_sha256:
        raise LegacyArchiveError("legacy archive SHA-256 does not match the registry")
    if target.exists():
        raise LegacyArchiveError(f"restore destination already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    manifest, payloads = _read_and_verify_archive(archive)

    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.restore-", dir=target.parent))
    try:
        for directory in manifest.directories:
            _safe_restore_path(stage, directory).mkdir(parents=True, exist_ok=True)
        manifest_target = stage / "legacy-manifest.json"
        manifest_target.write_text(manifest.to_json(), encoding="utf-8", newline="")
        for entry in manifest.files:
            output = _safe_restore_path(stage, entry.relative_path)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(payloads[entry.relative_path])
            if output.stat().st_size != entry.byte_size or _sha256_file(output) != entry.sha256:
                raise LegacyArchiveError(f"restored checksum mismatch: {entry.relative_path}")
        _rename_directory_noreplace(stage, target)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return manifest


def verify_legacy_archive(
    archive_path: str | Path,
    *,
    expected_archive_sha256: str | None = None,
) -> LegacyArchiveManifest:
    """Verify ZIP structure, the canonical manifest, and every payload byte."""

    archive = Path(archive_path).resolve()
    if not _is_regular_file(archive):
        raise LegacyArchiveError(f"archive is not a regular file: {archive}")
    if expected_archive_sha256 is not None and _sha256_file(archive) != expected_archive_sha256:
        raise LegacyArchiveError("legacy archive SHA-256 does not match the registry")
    manifest, _ = _read_and_verify_archive(archive)
    return manifest


def _table_policy(table: str) -> str:
    if table in _REBUILDABLE_TABLES:
        return "rebuildable"
    if table in _ARCHIVE_ONLY_TABLES:
        return "archive-only"
    if table in _MUST_MIGRATE_TABLES:
        return "must-migrate"
    if table in _WORKFLOW_TABLES:
        return "workflow-state"
    return "unknown"


def _classify_record(
    table: str,
    row: sqlite3.Row,
    policy: str,
) -> tuple[RecordClass, str, str, str]:
    if policy == "archive-only":
        return (
            "archive-only",
            "preserve_read_only",
            "",
            "record is retained in the legacy archive and is not claimed by the N1.2 query API",
        )
    if policy == "must-migrate":
        return (
            "blocker",
            "must_migrate_or_resolve",
            "active",
            "identity/control-plane record cannot be silently replaced at cutover",
        )
    if policy == "workflow-state":
        status_column = _WORKFLOW_TABLES[table]
        raw = row[status_column]
        status = raw.strip().lower() if type(raw) is str else ""
        if status in _ACTIVE_STATES:
            return (
                "blocker",
                "must_migrate_or_resolve",
                status,
                "active or pending workflow record would be orphaned by cutover",
            )
        if status in _TERMINAL_STATES:
            return (
                "archive-only",
                "preserve_read_only",
                status,
                "terminal workflow record is retained for audit only",
            )
        return (
            "unknown",
            "unknown_requires_adjudication",
            status,
            "unrecognized workflow status must not be treated as terminal",
        )
    return (
        "unknown",
        "unknown_requires_adjudication",
        "",
        "table has no frozen cutover disposition policy",
    )


def _read_and_verify_archive(
    archive: Path,
) -> tuple[LegacyArchiveManifest, dict[str, bytes]]:
    try:
        with zipfile.ZipFile(archive, "r") as bundle:
            infos = bundle.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise LegacyArchiveError("archive contains duplicate ZIP entry names")
            if "legacy-manifest.json" not in names:
                raise LegacyArchiveError("archive is missing legacy-manifest.json")
            for info in infos:
                _validate_archive_member(info)
            raw_manifest = bundle.read("legacy-manifest.json").decode("utf-8", errors="strict")
            if canonical_json_from_text(raw_manifest) != raw_manifest:
                raise LegacyArchiveError("legacy manifest is not canonical JSON")
            manifest = _manifest_from_dict(json.loads(raw_manifest))
            expected_names = {"legacy-manifest.json"}
            expected_names.update(f"{directory}/" for directory in manifest.directories)
            expected_names.update(entry.relative_path for entry in manifest.files)
            if set(names) != expected_names:
                extra = sorted(set(names) - expected_names)
                missing = sorted(expected_names - set(names))
                raise LegacyArchiveError(
                    f"archive/member manifest mismatch: extra={extra!r}, missing={missing!r}"
                )
            payloads: dict[str, bytes] = {}
            for entry in manifest.files:
                payload = bundle.read(entry.relative_path)
                if len(payload) != entry.byte_size:
                    raise LegacyArchiveError(f"archive size mismatch: {entry.relative_path}")
                if hashlib.sha256(payload).hexdigest() != entry.sha256:
                    raise LegacyArchiveError(f"archive checksum mismatch: {entry.relative_path}")
                payloads[entry.relative_path] = payload
            return manifest, payloads
    except (OSError, UnicodeError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        raise LegacyArchiveError(f"invalid legacy archive: {exc}") from exc


def _manifest_from_dict(raw: object) -> LegacyArchiveManifest:
    if not isinstance(raw, dict) or raw.get("schema") != _SCHEMA:
        raise LegacyArchiveError("unsupported legacy archive manifest schema")
    try:
        directories = tuple(str(item) for item in raw["directories"])
        files = tuple(
            ArchiveFileEntry(
                relative_path=str(item["relative_path"]),
                source_kind=item["source_kind"],
                sha256=str(item["sha256"]),
                byte_size=int(item["byte_size"]),
            )
            for item in raw["files"]
        )
        records = tuple(
            SQLiteRecordDisposition(
                database_label=str(item["database_label"]),
                table=str(item["table"]),
                record_sha256=str(item["record_sha256"]),
                primary_key_json=str(item["primary_key_json"]),
                record_class=item["record_class"],
                disposition=str(item["disposition"]),
                source_status=str(item["source_status"]),
                reason=str(item["reason"]),
            )
            for item in raw["sqlite_records"]
        )
        tables = tuple(
            SQLiteTableSummary(
                database_label=str(item["database_label"]),
                table=str(item["table"]),
                policy=str(item["policy"]),
                row_count=int(item["row_count"]),
            )
            for item in raw["sqlite_tables"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LegacyArchiveError(f"malformed legacy manifest: {exc}") from exc
    manifest = LegacyArchiveManifest(
        directories=directories,
        files=files,
        sqlite_records=records,
        sqlite_tables=tables,
    )
    if manifest.to_dict() != raw:
        raise LegacyArchiveError("legacy manifest contains invalid or non-normalized fields")
    for directory in manifest.directories:
        _validate_relative_posix(directory)
    for entry in manifest.files:
        _validate_relative_posix(entry.relative_path)
        if not re.fullmatch(r"[0-9a-f]{64}", entry.sha256) or entry.byte_size < 0:
            raise LegacyArchiveError(f"invalid file identity: {entry.relative_path}")
    return manifest


def _write_deterministic_zip(
    output: Path,
    *,
    stage: Path,
    directories: tuple[str, ...],
    files: tuple[str, ...],
) -> None:
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED, strict_timestamps=True) as zf:
        for directory in sorted(directories):
            info = _zip_info(f"{directory}/", is_dir=True)
            zf.writestr(info, b"")
        for relative_path in sorted(files):
            info = _zip_info(relative_path, is_dir=False)
            with (stage / Path(relative_path)).open("rb") as source, zf.open(info, "w") as sink:
                shutil.copyfileobj(source, sink, length=_CHUNK)


def _zip_info(name: str, *, is_dir: bool) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    mode = (stat.S_IFDIR | 0o755) if is_dir else (stat.S_IFREG | 0o644)
    info.external_attr = mode << 16
    info.flag_bits = 0x800
    return info


def _validate_archive_member(info: zipfile.ZipInfo) -> None:
    _validate_relative_posix(info.filename.rstrip("/"))
    if info.flag_bits & 0x1:
        raise LegacyArchiveError("encrypted ZIP members are forbidden")
    if info.compress_type != zipfile.ZIP_STORED:
        raise LegacyArchiveError("legacy archive must use deterministic stored ZIP members")
    mode = (info.external_attr >> 16) & 0xFFFF
    if info.is_dir():
        if mode and not stat.S_ISDIR(mode):
            raise LegacyArchiveError(f"non-directory mode on ZIP directory: {info.filename}")
    elif mode and not stat.S_ISREG(mode):
        raise LegacyArchiveError(f"special or symlink ZIP member is forbidden: {info.filename}")


def _scan_tree(root: Path) -> tuple[_FsEntry, ...]:
    _require_directory(root, label="content root")
    entries: list[_FsEntry] = []

    def visit(directory: Path, relative: Path) -> None:
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise LegacyArchiveError(f"cannot scan legacy directory {directory}: {exc}") from exc
        for child in children:
            path = Path(child.path)
            child_relative = relative / child.name
            _require_no_reparse(path)
            try:
                observed = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise LegacyArchiveError(f"cannot stat legacy path {path}: {exc}") from exc
            posix = child_relative.as_posix()
            if stat.S_ISDIR(observed.st_mode):
                entries.append(_FsEntry(relative_path=posix, is_dir=True))
                visit(path, child_relative)
            elif stat.S_ISREG(observed.st_mode):
                entries.append(_FsEntry(relative_path=posix, is_dir=False))
            else:
                raise LegacyArchiveError(f"special file is forbidden in legacy tree: {path}")

    visit(root, Path())
    return tuple(entries)


def _copy_stable_file(source: Path, destination: Path) -> tuple[str, int]:
    before = _regular_stat(source)
    digest = hashlib.sha256()
    total = 0
    with source.open("rb") as reader, destination.open("xb") as writer:
        opened = os.fstat(reader.fileno())
        if _stat_identity(before) != _stat_identity(opened):
            raise LegacyArchiveError(f"file changed before capture: {source}")
        while chunk := reader.read(_CHUNK):
            digest.update(chunk)
            total += len(chunk)
            writer.write(chunk)
        writer.flush()
        os.fsync(writer.fileno())
        closed = os.fstat(reader.fileno())
        if _stat_identity(opened) != _stat_identity(closed):
            raise LegacyArchiveError(f"file changed while being captured: {source}")
    after = _regular_stat(source)
    if _stat_identity(before) != _stat_identity(after):
        raise LegacyArchiveError(f"file changed while being captured: {source}")
    if total != before.st_size or _sha256_file(source) != digest.hexdigest():
        raise LegacyArchiveError(f"file bytes changed while being captured: {source}")
    return digest.hexdigest(), total


def _stat_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        int(value.st_mode),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_dev),
        int(value.st_ino),
    )


def _regular_stat(path: Path) -> os.stat_result:
    _require_no_reparse(path)
    try:
        observed = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise LegacyArchiveError(f"cannot stat legacy file {path}: {exc}") from exc
    if not stat.S_ISREG(observed.st_mode):
        raise LegacyArchiveError(f"legacy input is not a regular file: {path}")
    return observed


def _require_directory(path: Path, *, label: str) -> None:
    _require_no_reparse(path)
    try:
        observed = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise LegacyArchiveError(f"cannot stat {label} {path}: {exc}") from exc
    if not stat.S_ISDIR(observed.st_mode):
        raise LegacyArchiveError(f"{label} is not a directory: {path}")


def _require_no_reparse(path: Path) -> None:
    try:
        observed = os.lstat(path)
    except OSError as exc:
        raise LegacyArchiveError(f"cannot lstat legacy path {path}: {exc}") from exc
    attributes = int(getattr(observed, "st_file_attributes", 0))
    if stat.S_ISLNK(observed.st_mode) or attributes & _REPARSE_POINT:
        raise LegacyArchiveError(f"symlink/junction/reparse point is forbidden: {path}")


def _absolute_unresolved_path(raw_path: str | Path) -> Path:
    """Make a path absolute without resolving symlinks or reparse points."""

    return Path(os.path.abspath(os.fspath(raw_path)))


def _resolve_source_directory(raw_path: str | Path, *, label: str) -> Path:
    """Validate the caller-spelled source before any link-resolving operation."""

    unresolved = _absolute_unresolved_path(raw_path)
    _require_directory(unresolved, label=label)
    resolved = unresolved.resolve(strict=True)
    _require_directory(resolved, label=label)
    return resolved


def _resolve_source_file(raw_path: str | Path) -> Path:
    """Validate a source file before ``resolve`` can hide its link identity."""

    unresolved = _absolute_unresolved_path(raw_path)
    _regular_stat(unresolved)
    resolved = unresolved.resolve(strict=True)
    _regular_stat(resolved)
    return resolved


def _is_regular_file(path: Path) -> bool:
    try:
        _regular_stat(path)
    except LegacyArchiveError:
        return False
    return True


def _normalize_sqlite_inputs(
    databases: Mapping[str, str | Path],
) -> dict[str, Path]:
    result: dict[str, Path] = {}
    seen: set[Path] = set()
    for label, raw_path in sorted(databases.items()):
        _validate_label(label)
        path = _resolve_source_file(raw_path)
        if path in seen:
            raise LegacyArchiveError(f"SQLite input is listed more than once: {path}")
        seen.add(path)
        result[label] = path
    return result


def _validate_label(label: str) -> None:
    if type(label) is not str or _LABEL_RE.fullmatch(label) is None:
        raise LegacyArchiveError(f"invalid SQLite database label: {label!r}")


def _reject_sqlite_companions(path: Path) -> None:
    for suffix in ("-wal", "-journal", "-shm"):
        companion = Path(f"{path}{suffix}")
        if companion.exists() or companion.is_symlink():
            raise LegacyArchiveError(
                f"SQLite must be quiescent; companion file exists: {companion.name}"
            )


def _rebuildable_virtual_shadows(
    master: list[sqlite3.Row],
    table_types: dict[str, str],
) -> set[str]:
    """Return exact known shadow names for only frozen rebuildable virtual roots."""

    result: set[str] = set()
    for row in master:
        root = str(row["name"])
        sql = str(row["sql"] or "").lower()
        if table_types.get(root) != "virtual" or root not in _REBUILDABLE_TABLES:
            continue
        suffixes: tuple[str, ...]
        if "using fts5" in sql:
            suffixes = ("data", "idx", "content", "docsize", "config")
        elif "using rtree" in sql:
            suffixes = ("node", "rowid", "parent")
        else:
            # A newly introduced module must freeze its exact shadow contract
            # before the migration bridge is allowed to ignore those tables.
            continue
        result.update(
            candidate
            for suffix in suffixes
            if table_types.get(candidate := f"{root}_{suffix}") == "shadow"
        )
    return result


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _json_value(value: object) -> object:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        number = float(value)
        return number if math.isfinite(number) else {"$nonfinite_float": repr(number)}
    if isinstance(value, bytes):
        return {"$bytes_base64": base64.b64encode(value).decode("ascii")}
    return {"$sqlite_value": str(value), "$type": type(value).__name__}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _publish_file_noreplace(temporary: Path, target: Path) -> None:
    """Atomically publish a file without ever replacing an existing name."""

    # Windows' CRT rejects ``fsync`` on a read-only descriptor even though no
    # bytes are changed.  ``r+b`` gives FlushFileBuffers a writable handle while
    # preserving the already verified archive bytes.
    with temporary.open("r+b") as handle:
        os.fsync(handle.fileno())
    try:
        # Same-directory staging guarantees the hard link stays on one volume.
        # Link creation is atomic and has O_EXCL semantics for the target name.
        os.link(temporary, target, follow_symlinks=False)
    except FileExistsError:
        _regular_stat(target)
        if _sha256_file(target) != _sha256_file(temporary):
            raise LegacyArchiveError(
                "archive target already exists with different bytes; refusing overwrite"
            ) from None
    except OSError as exc:
        raise LegacyArchiveError(f"cannot publish legacy archive without clobber: {exc}") from exc
    else:
        _fsync_directory(target.parent)


def _rename_directory_noreplace(source: Path, target: Path) -> None:
    """Atomically publish a restored directory with no-replace semantics."""

    if os.name == "nt":
        try:
            # Windows MoveFile semantics used by os.rename do not replace an
            # existing directory, including an empty one.
            os.rename(source, target)
        except (FileExistsError, PermissionError) as exc:
            if target.exists() or target.is_symlink():
                raise LegacyArchiveError(
                    f"restore destination appeared concurrently: {target}"
                ) from exc
            raise
        return

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise LegacyArchiveError("this platform lacks atomic no-replace directory publication")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    at_fdcwd = -100  # Linux AT_FDCWD
    rename_noreplace = 1
    result = renameat2(
        at_fdcwd,
        os.fsencode(source),
        at_fdcwd,
        os.fsencode(target),
        rename_noreplace,
    )
    if result == 0:
        _fsync_directory(target.parent)
        return
    observed_errno = ctypes.get_errno()
    if observed_errno == errno.EEXIST:
        raise LegacyArchiveError(f"restore destination appeared concurrently: {target}")
    raise LegacyArchiveError(f"atomic restore publication failed: {os.strerror(observed_errno)}")


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_relative_posix(value: str) -> None:
    if not value or "\\" in value or value.startswith("/"):
        raise LegacyArchiveError(f"unsafe archive path: {value!r}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise LegacyArchiveError(f"unsafe archive path: {value!r}")
    if parts[0].endswith(":"):
        raise LegacyArchiveError(f"unsafe archive path: {value!r}")


def _safe_restore_path(root: Path, relative_posix: str) -> Path:
    _validate_relative_posix(relative_posix)
    target = (root / Path(*relative_posix.split("/"))).resolve()
    if not _is_within(target, root.resolve()):
        raise LegacyArchiveError(f"archive member escapes restore root: {relative_posix!r}")
    return target


__all__ = [
    "ArchiveFileEntry",
    "CutoverBlockedError",
    "LegacyArchiveError",
    "LegacyArchiveManifest",
    "LegacyArchivePackage",
    "SQLiteRecordDisposition",
    "SQLiteTableSummary",
    "classify_legacy_sqlite",
    "create_legacy_archive",
    "restore_legacy_archive",
    "verify_legacy_archive",
]
