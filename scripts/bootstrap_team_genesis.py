"""Archive, preflight, and atomically bootstrap one team canonical genesis."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from owcopilot.contracts import OwnershipScope
from owcopilot.contracts.hashing import canonical_json
from owcopilot.teamdb.bootstrap import (
    PostgresBootstrapRepository,
    bootstrap_from_legacy_archive,
)
from owcopilot.teamdb.legacy_archive import (
    CutoverBlockedError,
    LegacyArchivePackage,
    create_legacy_archive,
)
from owcopilot.teamdb.session import TeamDatabase


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create a deterministic legacy archive, fail closed on unresolved SQLite records, "
            "then publish one parentless PostgreSQL authoring revision."
        )
    )
    parser.add_argument("--content-root", required=True, type=Path)
    sqlite_inventory = parser.add_mutually_exclusive_group(required=True)
    sqlite_inventory.add_argument(
        "--sqlite",
        action="append",
        default=[],
        metavar="LABEL=PATH",
        help="quiescent legacy SQLite database; repeat for multiple databases",
    )
    sqlite_inventory.add_argument(
        "--no-legacy-sqlite",
        action="store_true",
        help="explicitly attest that this cutover has no legacy SQLite databases",
    )
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--tenant-id")
    parser.add_argument("--container-id")
    parser.add_argument("--actor-subject")
    parser.add_argument(
        "--created-at",
        help="RFC 3339 UTC timestamp for genesis metadata (required unless --preflight-only)",
    )
    parser.add_argument("--scratch-parent", type=Path)
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="create/verify the archive and report blockers without connecting to PostgreSQL",
    )
    return parser


def _sqlite_inputs(parser: argparse.ArgumentParser, values: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for item in values:
        label, separator, raw_path = item.partition("=")
        if not separator or not label or not raw_path:
            parser.error(f"--sqlite must be LABEL=PATH, got {item!r}")
        if label in result:
            parser.error(f"duplicate --sqlite label: {label}")
        result[label] = Path(raw_path)
    return result


def _created_at(parser: argparse.ArgumentParser, raw: str | None) -> datetime:
    if raw is None:
        parser.error("--created-at is required for a real bootstrap")
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        parser.error(f"--created-at must be an RFC 3339 datetime: {exc}")
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
        parser.error("--created-at must be timezone-aware UTC")
    return value


def _package_summary(package: LegacyArchivePackage) -> dict[str, object]:
    manifest = package.manifest
    return {
        "archive_id": package.archive_id,
        "archive_path": str(package.archive_path),
        "archive_sha256": package.archive_sha256,
        "archive_size": package.byte_size,
        "blocker_count": len(manifest.blockers),
        "cutover_safe": manifest.cutover_safe,
        "file_count": len(manifest.files),
        "source_manifest_hash": manifest.source_manifest_hash,
        "sqlite_record_count": len(manifest.sqlite_records),
    }


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    sqlite_inputs = _sqlite_inputs(parser, args.sqlite)
    package = create_legacy_archive(
        content_root=args.content_root,
        sqlite_databases=sqlite_inputs,
        archive_path=args.archive,
    )
    summary = _package_summary(package)
    if args.preflight_only:
        print(canonical_json(summary))
        return 0 if package.manifest.cutover_safe else 2

    missing = [
        name
        for name, value in (
            ("--tenant-id", args.tenant_id),
            ("--container-id", args.container_id),
            ("--actor-subject", args.actor_subject),
        )
        if not value
    ]
    if missing:
        parser.error(f"real bootstrap requires {', '.join(missing)}")
    created_at = _created_at(parser, args.created_at)
    scope = OwnershipScope(tenant_id=args.tenant_id, container_id=args.container_id)
    database = TeamDatabase.from_env()
    try:
        repository = PostgresBootstrapRepository(database, actor_subject=args.actor_subject)
        result = bootstrap_from_legacy_archive(
            repository=repository,
            scope=scope,
            archive=package,
            legacy_version=None,
            created_at=created_at,
            created_by=args.actor_subject,
            scratch_parent=args.scratch_parent,
        )
    finally:
        database.close()
    print(
        canonical_json(
            {
                **summary,
                "genesis_manifest_hash": result.completion.genesis_manifest_hash,
                "genesis_revision_id": result.completion.genesis_revision_id,
                "idempotent_replay": result.idempotent_replay,
                "import_run_id": result.completion.import_run_id,
                "status": "COMPLETED",
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CutoverBlockedError as exc:
        print(json.dumps({"error": "CUTOVER_BLOCKED", "detail": str(exc)}), file=sys.stderr)
        raise SystemExit(2) from exc
