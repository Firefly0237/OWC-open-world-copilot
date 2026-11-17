"""SQLAlchemy Core registry for the PostgreSQL team truth through N2.1.

The Pydantic contracts remain the authority for canonical hashes and identifiers.  This module
mirrors their durable shape and adds relational scope/FK constraints; migrations remain the
authority for PostgreSQL-only RLS, grants, and immutable triggers.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

EXPECTED_ALEMBIC_HEAD = "n21_0001"
MIGRATION_ROLE = "owcopilot_migration"
RUNTIME_ROLE = "owcopilot_app"
TENANT_GUC = "owcopilot.tenant_id"
CONTAINER_GUC = "owcopilot.container_id"

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)


def _scope_columns() -> tuple[sa.Column[Any], sa.Column[Any]]:
    return (
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("container_id", UUID(as_uuid=True), nullable=False),
    )


tenants = sa.Table(
    "tenants",
    metadata,
    sa.Column("tenant_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("slug", sa.Text, nullable=False, unique=True),
    sa.Column("display_name", sa.Text, nullable=False),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.CheckConstraint("tenant_id <> '00000000-0000-0000-0000-000000000000'::uuid", name="non_nil"),
    sa.CheckConstraint("slug <> ''", name="slug_nonempty"),
    sa.CheckConstraint("display_name <> ''", name="display_name_nonempty"),
)

memberships = sa.Table(
    "memberships",
    metadata,
    sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
    sa.Column("subject_id", UUID(as_uuid=True), nullable=False),
    sa.Column("role", sa.Text, nullable=False),
    sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.PrimaryKeyConstraint("tenant_id", "subject_id"),
    sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
    sa.CheckConstraint(
        "subject_id <> '00000000-0000-0000-0000-000000000000'::uuid",
        name="subject_non_nil",
    ),
    sa.CheckConstraint("role IN ('owner', 'editor', 'reviewer', 'viewer')", name="role_known"),
)

containers = sa.Table(
    "containers",
    metadata,
    *_scope_columns(),
    sa.Column("slug", sa.Text, nullable=False),
    sa.Column("display_name", sa.Text, nullable=False),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.PrimaryKeyConstraint("tenant_id", "container_id"),
    sa.UniqueConstraint("tenant_id", "slug"),
    sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
    sa.CheckConstraint(
        "container_id <> '00000000-0000-0000-0000-000000000000'::uuid",
        name="container_non_nil",
    ),
    sa.CheckConstraint("slug <> ''", name="slug_nonempty"),
    sa.CheckConstraint("display_name <> ''", name="display_name_nonempty"),
)

bootstrap_import_runs = sa.Table(
    "bootstrap_import_runs",
    metadata,
    *_scope_columns(),
    sa.Column("import_run_id", UUID(as_uuid=True), nullable=False),
    sa.Column("idempotency_key", sa.Text, nullable=False),
    sa.Column("source_manifest_hash", sa.CHAR(64), nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column(
        "started_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.Column("genesis_revision_id", sa.Text),
    sa.Column("error_code", sa.Text),
    sa.PrimaryKeyConstraint("tenant_id", "container_id", "import_run_id"),
    sa.UniqueConstraint("tenant_id", "container_id", "idempotency_key"),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id"],
        ["containers.tenant_id", "containers.container_id"],
        ondelete="CASCADE",
    ),
    sa.CheckConstraint("idempotency_key <> ''", name="idempotency_key_nonempty"),
    sa.CheckConstraint(
        "source_manifest_hash ~ '^[0-9a-f]{64}$'",
        name="source_manifest_sha256",
    ),
    sa.CheckConstraint(
        "status IN ('building', 'committed')",
        name="status_known",
    ),
    sa.CheckConstraint(
        "(status = 'building' AND finished_at IS NULL AND genesis_revision_id IS NULL "
        "AND error_code IS NULL) OR (status = 'committed' AND finished_at IS NOT NULL "
        "AND genesis_revision_id IS NOT NULL AND error_code IS NULL)",
        name="state_shape",
    ),
)

legacy_archive_registry = sa.Table(
    "legacy_archive_registry",
    metadata,
    *_scope_columns(),
    sa.Column("archive_id", UUID(as_uuid=True), nullable=False),
    sa.Column("relative_path", sa.Text, nullable=False),
    sa.Column("entry_kind", sa.Text, nullable=False),
    sa.Column("sha256", sa.CHAR(64), nullable=False),
    sa.Column("byte_size", sa.BigInteger, nullable=False),
    sa.Column("record_class", sa.Text, nullable=False),
    sa.Column("disposition", sa.Text, nullable=False),
    sa.Column("source_status", sa.Text, nullable=False),
    sa.Column(
        "registered_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.PrimaryKeyConstraint("tenant_id", "container_id", "archive_id", "relative_path"),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id"],
        ["containers.tenant_id", "containers.container_id"],
        ondelete="CASCADE",
    ),
    sa.CheckConstraint("relative_path <> ''", name="relative_path_nonempty"),
    sa.CheckConstraint(
        "entry_kind IN ('directory_manifest', 'file', 'sqlite_record')",
        name="entry_kind_known",
    ),
    sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_valid"),
    sa.CheckConstraint("byte_size >= 0", name="byte_size_nonnegative"),
    sa.CheckConstraint(
        "record_class IN ('archive-only', 'blocker', 'unknown')",
        name="record_class_known",
    ),
    sa.CheckConstraint(
        "disposition IN ("
        "'restorable_archive_bundle', 'deterministic_directory_manifest', "
        "'preserve_read_only', 'must_migrate_or_resolve', "
        "'unknown_requires_adjudication')",
        name="disposition_known",
    ),
)

content_object_versions = sa.Table(
    "content_object_versions",
    metadata,
    *_scope_columns(),
    sa.Column("logical_id", sa.Text, nullable=False),
    sa.Column("object_version_id", sa.Text, nullable=False),
    sa.Column("object_type", sa.Text, nullable=False),
    sa.Column("payload_json", sa.Text, nullable=False),
    sa.Column("payload_jsonb", JSONB, nullable=False),
    sa.Column("content_hash", sa.CHAR(64), nullable=False),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.PrimaryKeyConstraint("tenant_id", "container_id", "object_version_id"),
    sa.UniqueConstraint("tenant_id", "container_id", "logical_id", "object_version_id"),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id"],
        ["containers.tenant_id", "containers.container_id"],
        ondelete="CASCADE",
    ),
    sa.CheckConstraint("logical_id <> ''", name="logical_id_nonempty"),
    sa.CheckConstraint("object_type <> ''", name="object_type_nonempty"),
    sa.CheckConstraint("object_version_id ~ '^objv_[0-9a-f]{64}$'", name="object_version_id_valid"),
    sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="content_hash_valid"),
    sa.CheckConstraint("payload_jsonb = payload_json::jsonb", name="payload_twins_match"),
)

content_revisions = sa.Table(
    "content_revisions",
    metadata,
    *_scope_columns(),
    sa.Column("revision_id", sa.Text, nullable=False),
    sa.Column("parent_revision", sa.Text),
    sa.Column("manifest_hash", sa.CHAR(64), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("created_by", sa.Text, nullable=False),
    sa.Column("import_run_id", UUID(as_uuid=True)),
    sa.PrimaryKeyConstraint("tenant_id", "container_id", "revision_id"),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id"],
        ["containers.tenant_id", "containers.container_id"],
        ondelete="CASCADE",
    ),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id", "parent_revision"],
        [
            "content_revisions.tenant_id",
            "content_revisions.container_id",
            "content_revisions.revision_id",
        ],
        deferrable=True,
        initially="DEFERRED",
    ),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id", "import_run_id"],
        [
            "bootstrap_import_runs.tenant_id",
            "bootstrap_import_runs.container_id",
            "bootstrap_import_runs.import_run_id",
        ],
        deferrable=True,
        initially="DEFERRED",
    ),
    sa.CheckConstraint("revision_id ~ '^rev_[0-9a-f]{64}$'", name="revision_id_valid"),
    sa.CheckConstraint(
        "parent_revision IS NULL OR parent_revision ~ '^rev_[0-9a-f]{64}$'",
        name="parent_revision_valid",
    ),
    sa.CheckConstraint("manifest_hash ~ '^[0-9a-f]{64}$'", name="manifest_hash_valid"),
    sa.CheckConstraint("created_by <> ''", name="created_by_nonempty"),
)

# Added by n12_0002 after both sides exist; kept here so metadata describes the final head.
bootstrap_import_runs.append_constraint(
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id", "genesis_revision_id"],
        [
            "content_revisions.tenant_id",
            "content_revisions.container_id",
            "content_revisions.revision_id",
        ],
        name="fk_bootstrap_import_runs_genesis_revision",
        deferrable=True,
        initially="DEFERRED",
    )
)
bootstrap_import_runs.append_constraint(
    sa.CheckConstraint(
        "genesis_revision_id IS NULL OR genesis_revision_id ~ '^rev_[0-9a-f]{64}$'",
        name="genesis_revision_valid",
    )
)

revision_memberships = sa.Table(
    "revision_memberships",
    metadata,
    *_scope_columns(),
    sa.Column("revision_id", sa.Text, nullable=False),
    sa.Column("logical_id", sa.Text, nullable=False),
    sa.Column("object_version_id", sa.Text),
    sa.Column("tombstone", sa.Boolean, nullable=False),
    sa.PrimaryKeyConstraint("tenant_id", "container_id", "revision_id", "logical_id"),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id"],
        ["containers.tenant_id", "containers.container_id"],
        ondelete="CASCADE",
    ),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id", "revision_id"],
        [
            "content_revisions.tenant_id",
            "content_revisions.container_id",
            "content_revisions.revision_id",
        ],
        ondelete="CASCADE",
    ),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id", "logical_id", "object_version_id"],
        [
            "content_object_versions.tenant_id",
            "content_object_versions.container_id",
            "content_object_versions.logical_id",
            "content_object_versions.object_version_id",
        ],
    ),
    sa.CheckConstraint("logical_id <> ''", name="logical_id_nonempty"),
    sa.CheckConstraint(
        "(object_version_id IS NOT NULL AND tombstone = false) OR "
        "(object_version_id IS NULL AND tombstone = true)",
        name="live_or_tombstone",
    ),
)

release_pointers = sa.Table(
    "release_pointers",
    metadata,
    *_scope_columns(),
    sa.Column("authoring_head", sa.Text),
    sa.Column("serving_head", sa.Text),
    sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.Column("lock_version", sa.BigInteger, nullable=False, server_default=sa.text("0")),
    sa.PrimaryKeyConstraint("tenant_id", "container_id"),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id"],
        ["containers.tenant_id", "containers.container_id"],
        ondelete="CASCADE",
    ),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id", "authoring_head"],
        [
            "content_revisions.tenant_id",
            "content_revisions.container_id",
            "content_revisions.revision_id",
        ],
        deferrable=True,
        initially="DEFERRED",
    ),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id", "serving_head"],
        [
            "content_revisions.tenant_id",
            "content_revisions.container_id",
            "content_revisions.revision_id",
        ],
        deferrable=True,
        initially="DEFERRED",
    ),
    sa.CheckConstraint(
        "serving_head IS NULL OR authoring_head IS NOT NULL",
        name="serving_requires_authoring",
    ),
    sa.CheckConstraint("lock_version >= 0", name="lock_version_nonnegative"),
)

operation_events = sa.Table(
    "operation_events",
    metadata,
    *_scope_columns(),
    sa.Column("event_id", UUID(as_uuid=True), nullable=False),
    sa.Column("event_type", sa.Text, nullable=False),
    sa.Column("subject_id", UUID(as_uuid=True), nullable=False),
    sa.Column("revision_id", sa.Text),
    sa.Column("actor_id", UUID(as_uuid=True), nullable=False),
    sa.Column(
        "occurred_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.Column("payload_jsonb", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.PrimaryKeyConstraint("tenant_id", "container_id", "event_id"),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id"],
        ["containers.tenant_id", "containers.container_id"],
        ondelete="CASCADE",
    ),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id", "revision_id"],
        [
            "content_revisions.tenant_id",
            "content_revisions.container_id",
            "content_revisions.revision_id",
        ],
        deferrable=True,
        initially="DEFERRED",
    ),
    sa.CheckConstraint("event_type <> ''", name="event_type_nonempty"),
    sa.CheckConstraint(
        "subject_id <> '00000000-0000-0000-0000-000000000000'::uuid",
        name="subject_id_non_nil",
    ),
    sa.CheckConstraint(
        "actor_id <> '00000000-0000-0000-0000-000000000000'::uuid",
        name="actor_id_non_nil",
    ),
)

source_artifacts = sa.Table(
    "source_artifacts",
    metadata,
    *_scope_columns(),
    sa.Column("artifact_id", UUID(as_uuid=True), nullable=False),
    sa.Column("display_name", sa.Text, nullable=False),
    sa.Column("created_by", UUID(as_uuid=True), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.Column("logical_deleted_at", sa.DateTime(timezone=True)),
    sa.PrimaryKeyConstraint("tenant_id", "container_id", "artifact_id"),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id"],
        ["containers.tenant_id", "containers.container_id"],
        ondelete="CASCADE",
    ),
    sa.CheckConstraint(
        "artifact_id <> '00000000-0000-0000-0000-000000000000'::uuid", name="artifact_non_nil"
    ),
    sa.CheckConstraint("display_name <> ''", name="display_name_nonempty"),
    sa.CheckConstraint(
        "created_by <> '00000000-0000-0000-0000-000000000000'::uuid", name="created_by_non_nil"
    ),
    sa.CheckConstraint(
        "logical_deleted_at IS NULL OR logical_deleted_at >= created_at",
        name="logical_delete_not_before_creation",
    ),
)

artifact_versions = sa.Table(
    "artifact_versions",
    metadata,
    *_scope_columns(),
    sa.Column("artifact_version_id", UUID(as_uuid=True), nullable=False),
    sa.Column("artifact_id", UUID(as_uuid=True), nullable=False),
    sa.Column("content_hash", sa.CHAR(64), nullable=False),
    sa.Column("mime_type", sa.Text, nullable=False),
    sa.Column("extension", sa.Text, nullable=False),
    sa.Column("byte_size", sa.BigInteger, nullable=False),
    sa.Column("content_bytes", sa.LargeBinary, nullable=False),
    sa.Column("created_by", UUID(as_uuid=True), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.PrimaryKeyConstraint("tenant_id", "container_id", "artifact_version_id"),
    sa.UniqueConstraint("tenant_id", "container_id", "artifact_id", "content_hash"),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id"],
        ["containers.tenant_id", "containers.container_id"],
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id", "artifact_id"],
        [
            "source_artifacts.tenant_id",
            "source_artifacts.container_id",
            "source_artifacts.artifact_id",
        ],
        ondelete="RESTRICT",
    ),
    sa.CheckConstraint(
        "artifact_version_id <> '00000000-0000-0000-0000-000000000000'::uuid",
        name="version_non_nil",
    ),
    sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="content_hash_valid"),
    sa.CheckConstraint(
        "content_hash = encode(sha256(content_bytes), 'hex')", name="content_hash_matches"
    ),
    sa.CheckConstraint(
        "byte_size > 0 AND byte_size = octet_length(content_bytes)", name="byte_size_matches"
    ),
    sa.CheckConstraint(
        "(extension = '.docx' AND mime_type = 'application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document') OR (extension = '.pdf' AND mime_type = "
        "'application/pdf') OR (extension = '.xlsx' AND mime_type = "
        "'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')",
        name="supported_format_pair",
    ),
    sa.CheckConstraint(
        "created_by <> '00000000-0000-0000-0000-000000000000'::uuid", name="created_by_non_nil"
    ),
)

intake_runs = sa.Table(
    "intake_runs",
    metadata,
    *_scope_columns(),
    sa.Column("run_id", UUID(as_uuid=True), nullable=False),
    sa.Column("artifact_version_id", UUID(as_uuid=True), nullable=False),
    sa.Column("original_filename", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("created_by", UUID(as_uuid=True), nullable=False),
    sa.Column(
        "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.Column("rejection_code", sa.Text),
    sa.Column("processing_code", sa.Text),
    sa.PrimaryKeyConstraint("tenant_id", "container_id", "run_id"),
    sa.UniqueConstraint("tenant_id", "container_id", "run_id", "artifact_version_id"),
    sa.UniqueConstraint("tenant_id", "container_id", "artifact_version_id"),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id"],
        ["containers.tenant_id", "containers.container_id"],
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id", "artifact_version_id"],
        [
            "artifact_versions.tenant_id",
            "artifact_versions.container_id",
            "artifact_versions.artifact_version_id",
        ],
        ondelete="RESTRICT",
    ),
    sa.CheckConstraint(
        "run_id <> '00000000-0000-0000-0000-000000000000'::uuid", name="run_non_nil"
    ),
    sa.CheckConstraint(
        "created_by <> '00000000-0000-0000-0000-000000000000'::uuid", name="created_by_non_nil"
    ),
    sa.CheckConstraint("original_filename <> ''", name="filename_nonempty"),
    sa.CheckConstraint(
        "status IN ('UPLOADED', 'SEGMENTED', 'OCR_REQUIRED', 'PROCESSING_FAILED', "
        "'AWAITING_HUMAN', 'REJECTED')",
        name="status_known",
    ),
    sa.CheckConstraint(
        "(status = 'REJECTED' AND finished_at IS NOT NULL AND rejection_code IS NOT NULL "
        "AND processing_code IS NULL) OR "
        "(status = 'OCR_REQUIRED' AND finished_at IS NULL AND rejection_code IS NULL "
        "AND processing_code = 'PDF_OCR_REQUIRED') OR "
        "(status = 'PROCESSING_FAILED' AND finished_at IS NULL AND rejection_code IS NULL "
        "AND processing_code IN ('CORRUPT_PDF', 'PDF_ENCRYPTED', "
        "'PDF_LAYOUT_BACKEND_UNAVAILABLE', 'PDF_MAGIC_MISMATCH', "
        "'PDF_PAGE_DIMENSION_EXCEEDED', 'PDF_PAGE_LIMIT_EXCEEDED', "
        "'PDF_PROCESSING_FAILED', 'PDF_TEXT_LIMIT_EXCEEDED', "
        "'PDF_VALIDATION_BACKEND_UNAVAILABLE', "
        "'UNLOCATABLE_PDF_TEXT')) OR "
        "(status = 'AWAITING_HUMAN' AND finished_at IS NOT NULL AND rejection_code IS NULL "
        "AND processing_code IS NULL) OR "
        "(status IN ('UPLOADED', 'SEGMENTED') AND finished_at IS NULL "
        "AND rejection_code IS NULL AND processing_code IS NULL)",
        name="state_shape",
    ),
    sa.CheckConstraint(
        "finished_at IS NULL OR finished_at >= started_at", name="finish_not_before_start"
    ),
    sa.CheckConstraint(
        "rejection_code IS NULL OR rejection_code <> ''", name="rejection_code_nonempty"
    ),
    sa.CheckConstraint(
        "processing_code IS NULL OR processing_code IN ('PDF_OCR_REQUIRED', 'CORRUPT_PDF', "
        "'PDF_ENCRYPTED', 'PDF_LAYOUT_BACKEND_UNAVAILABLE', 'PDF_MAGIC_MISMATCH', "
        "'PDF_PAGE_DIMENSION_EXCEEDED', 'PDF_PAGE_LIMIT_EXCEEDED', "
        "'PDF_PROCESSING_FAILED', 'PDF_TEXT_LIMIT_EXCEEDED', "
        "'PDF_VALIDATION_BACKEND_UNAVAILABLE', "
        "'UNLOCATABLE_PDF_TEXT')",
        name="processing_code_known",
    ),
)

source_segments = sa.Table(
    "source_segments",
    metadata,
    *_scope_columns(),
    sa.Column("segment_id", UUID(as_uuid=True), nullable=False),
    sa.Column("artifact_version_id", UUID(as_uuid=True), nullable=False),
    sa.Column("ordinal", sa.Integer, nullable=False),
    sa.Column("locator_kind", sa.Text, nullable=False),
    sa.Column("locator_jsonb", JSONB, nullable=False),
    sa.Column("text_content", sa.Text, nullable=False),
    sa.Column("text_hash", sa.CHAR(64), nullable=False),
    sa.Column("untrusted_input", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.PrimaryKeyConstraint("tenant_id", "container_id", "segment_id"),
    sa.UniqueConstraint("tenant_id", "container_id", "artifact_version_id", "ordinal"),
    sa.UniqueConstraint("tenant_id", "container_id", "segment_id", "artifact_version_id"),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id"],
        ["containers.tenant_id", "containers.container_id"],
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id", "artifact_version_id"],
        [
            "artifact_versions.tenant_id",
            "artifact_versions.container_id",
            "artifact_versions.artifact_version_id",
        ],
        ondelete="RESTRICT",
    ),
    sa.CheckConstraint(
        "segment_id <> '00000000-0000-0000-0000-000000000000'::uuid", name="segment_non_nil"
    ),
    sa.CheckConstraint("ordinal >= 0", name="ordinal_nonnegative"),
    sa.CheckConstraint(
        "locator_kind IN ('docx_paragraph', 'docx_table_cell', 'pdf_text_line', 'xlsx_cell')",
        name="locator_kind_known",
    ),
    sa.CheckConstraint(
        "owcopilot_valid_source_locator(locator_kind, locator_jsonb)", name="locator_exact"
    ),
    sa.CheckConstraint("text_content <> ''", name="text_nonempty"),
    sa.CheckConstraint("text_hash ~ '^[0-9a-f]{64}$'", name="text_hash_valid"),
    sa.CheckConstraint(
        "text_hash = encode(sha256(convert_to(text_content, 'UTF8')), 'hex')",
        name="text_hash_matches",
    ),
    sa.CheckConstraint("untrusted_input = true", name="always_untrusted"),
)

raw_triage_outputs = sa.Table(
    "raw_triage_outputs",
    metadata,
    *_scope_columns(),
    sa.Column("raw_output_id", UUID(as_uuid=True), nullable=False),
    sa.Column("run_id", UUID(as_uuid=True), nullable=False),
    sa.Column("segment_id", UUID(as_uuid=True), nullable=False),
    sa.Column("artifact_version_id", UUID(as_uuid=True), nullable=False),
    sa.Column("provider", sa.Text, nullable=False),
    sa.Column("model", sa.Text, nullable=False),
    sa.Column("output_text", sa.Text, nullable=False),
    sa.Column("output_hash", sa.CHAR(64), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.PrimaryKeyConstraint("tenant_id", "container_id", "raw_output_id"),
    sa.UniqueConstraint(
        "tenant_id",
        "container_id",
        "raw_output_id",
        "artifact_version_id",
        "segment_id",
    ),
    sa.UniqueConstraint("tenant_id", "container_id", "run_id", "segment_id"),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id"],
        ["containers.tenant_id", "containers.container_id"],
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id", "run_id", "artifact_version_id"],
        [
            "intake_runs.tenant_id",
            "intake_runs.container_id",
            "intake_runs.run_id",
            "intake_runs.artifact_version_id",
        ],
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id", "segment_id", "artifact_version_id"],
        [
            "source_segments.tenant_id",
            "source_segments.container_id",
            "source_segments.segment_id",
            "source_segments.artifact_version_id",
        ],
        ondelete="RESTRICT",
    ),
    sa.CheckConstraint(
        "raw_output_id <> '00000000-0000-0000-0000-000000000000'::uuid",
        name="raw_output_non_nil",
    ),
    sa.CheckConstraint("provider <> '' AND model <> ''", name="raw_provider_model_nonempty"),
    sa.CheckConstraint("output_hash ~ '^[0-9a-f]{64}$'", name="output_hash_valid"),
    sa.CheckConstraint(
        "output_hash = encode(sha256(convert_to(output_text, 'UTF8')), 'hex')",
        name="output_hash_matches",
    ),
)

triage_verdicts = sa.Table(
    "triage_verdicts",
    metadata,
    *_scope_columns(),
    sa.Column("verdict_id", UUID(as_uuid=True), nullable=False),
    sa.Column("raw_output_id", UUID(as_uuid=True), nullable=False),
    sa.Column("segment_id", UUID(as_uuid=True), nullable=False),
    sa.Column("artifact_version_id", UUID(as_uuid=True), nullable=False),
    sa.Column("content_role", sa.Text, nullable=False),
    sa.Column("confidence", sa.Double, nullable=False),
    sa.Column("policy_threshold", sa.Double, nullable=False),
    sa.Column("evidence_spans_jsonb", JSONB, nullable=False),
    sa.Column("abstain", sa.Boolean, nullable=False),
    sa.Column("abstain_reason", sa.Text),
    sa.Column("derivation_policy_version", sa.Text, nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.PrimaryKeyConstraint("tenant_id", "container_id", "verdict_id"),
    sa.UniqueConstraint(
        "tenant_id", "container_id", "verdict_id", "artifact_version_id", "segment_id"
    ),
    sa.UniqueConstraint("tenant_id", "container_id", "raw_output_id", "derivation_policy_version"),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id"],
        ["containers.tenant_id", "containers.container_id"],
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        [
            "tenant_id",
            "container_id",
            "raw_output_id",
            "artifact_version_id",
            "segment_id",
        ],
        [
            "raw_triage_outputs.tenant_id",
            "raw_triage_outputs.container_id",
            "raw_triage_outputs.raw_output_id",
            "raw_triage_outputs.artifact_version_id",
            "raw_triage_outputs.segment_id",
        ],
        ondelete="RESTRICT",
    ),
    sa.CheckConstraint(
        "verdict_id <> '00000000-0000-0000-0000-000000000000'::uuid",
        name="verdict_non_nil",
    ),
    sa.CheckConstraint(
        "content_role IN ('CANON_CANDIDATE', 'REFERENCE', 'MIXED', 'UNKNOWN')",
        name="content_role_known",
    ),
    sa.CheckConstraint(
        "confidence <> 'NaN'::double precision AND confidence >= 0.0 AND confidence <= 1.0",
        name="confidence_probability",
    ),
    sa.CheckConstraint(
        "policy_threshold <> 'NaN'::double precision AND policy_threshold >= 0.0 "
        "AND policy_threshold <= 1.0",
        name="threshold_probability",
    ),
    sa.CheckConstraint(
        "owcopilot_valid_triage_evidence(evidence_spans_jsonb)", name="evidence_exact"
    ),
    sa.CheckConstraint(
        "NOT (content_role = 'UNKNOWN' OR confidence < policy_threshold) OR abstain",
        name="mandatory_abstention",
    ),
    sa.CheckConstraint("abstain = (abstain_reason IS NOT NULL)", name="abstain_reason_shape"),
    sa.CheckConstraint(
        "abstain OR jsonb_array_length(evidence_spans_jsonb) > 0",
        name="non_abstain_has_evidence",
    ),
    sa.CheckConstraint(
        "derivation_policy_version <> ''", name="derivation_policy_version_nonempty"
    ),
)

human_triage_decisions = sa.Table(
    "human_triage_decisions",
    metadata,
    *_scope_columns(),
    sa.Column("decision_id", UUID(as_uuid=True), nullable=False),
    sa.Column("run_id", UUID(as_uuid=True), nullable=False),
    sa.Column("verdict_id", UUID(as_uuid=True), nullable=False),
    sa.Column("segment_id", UUID(as_uuid=True), nullable=False),
    sa.Column("artifact_version_id", UUID(as_uuid=True), nullable=False),
    sa.Column("decision", sa.Text, nullable=False),
    sa.Column("content_role", sa.Text, nullable=False),
    sa.Column("decided_by", UUID(as_uuid=True), nullable=False),
    sa.Column("rationale", sa.Text, nullable=False),
    sa.Column(
        "decided_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.PrimaryKeyConstraint("tenant_id", "container_id", "decision_id"),
    sa.UniqueConstraint("tenant_id", "container_id", "run_id", "segment_id"),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id"],
        ["containers.tenant_id", "containers.container_id"],
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id", "run_id", "artifact_version_id"],
        [
            "intake_runs.tenant_id",
            "intake_runs.container_id",
            "intake_runs.run_id",
            "intake_runs.artifact_version_id",
        ],
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id", "verdict_id", "artifact_version_id", "segment_id"],
        [
            "triage_verdicts.tenant_id",
            "triage_verdicts.container_id",
            "triage_verdicts.verdict_id",
            "triage_verdicts.artifact_version_id",
            "triage_verdicts.segment_id",
        ],
        ondelete="RESTRICT",
    ),
    sa.CheckConstraint(
        "decision_id <> '00000000-0000-0000-0000-000000000000'::uuid",
        name="decision_non_nil",
    ),
    sa.CheckConstraint("decision IN ('CONFIRM', 'OVERRIDE', 'REJECT')", name="decision_known"),
    sa.CheckConstraint(
        "content_role IN ('CANON_CANDIDATE', 'REFERENCE', 'MIXED', 'UNKNOWN')",
        name="content_role_known",
    ),
    sa.CheckConstraint("rationale <> ''", name="rationale_nonempty"),
    sa.CheckConstraint(
        "decided_by <> '00000000-0000-0000-0000-000000000000'::uuid",
        name="decided_by_non_nil",
    ),
)

rights_declarations = sa.Table(
    "rights_declarations",
    metadata,
    *_scope_columns(),
    sa.Column("rights_declaration_id", UUID(as_uuid=True), nullable=False),
    sa.Column("artifact_version_id", UUID(as_uuid=True), nullable=False),
    sa.Column("rights_provenance", sa.Text, nullable=False),
    sa.Column("declaration_text", sa.Text, nullable=False),
    sa.Column("declared_by", UUID(as_uuid=True), nullable=False),
    sa.Column("human_asserted", sa.Boolean, nullable=False),
    sa.Column(
        "declared_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.PrimaryKeyConstraint("tenant_id", "container_id", "rights_declaration_id"),
    sa.UniqueConstraint("tenant_id", "container_id", "artifact_version_id"),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id"],
        ["containers.tenant_id", "containers.container_id"],
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id", "artifact_version_id"],
        [
            "artifact_versions.tenant_id",
            "artifact_versions.container_id",
            "artifact_versions.artifact_version_id",
        ],
        ondelete="RESTRICT",
    ),
    sa.CheckConstraint(
        "rights_provenance IN ('OWNED', 'LICENSED', 'THIRD_PARTY', 'UNKNOWN')",
        name="provenance_known",
    ),
    sa.CheckConstraint("declaration_text <> ''", name="declaration_nonempty"),
    sa.CheckConstraint("human_asserted = true", name="human_only"),
    sa.CheckConstraint(
        "rights_declaration_id <> '00000000-0000-0000-0000-000000000000'::uuid",
        name="rights_declaration_non_nil",
    ),
    sa.CheckConstraint(
        "declared_by <> '00000000-0000-0000-0000-000000000000'::uuid",
        name="declared_by_non_nil",
    ),
)

allowed_uses = sa.Table(
    "allowed_uses",
    metadata,
    *_scope_columns(),
    sa.Column("allowed_use_id", UUID(as_uuid=True), nullable=False),
    sa.Column("rights_declaration_id", UUID(as_uuid=True), nullable=False),
    sa.Column("use_kind", sa.Text, nullable=False),
    sa.Column("allowed", sa.Boolean, nullable=False),
    sa.Column("declared_by", UUID(as_uuid=True), nullable=False),
    sa.Column(
        "declared_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.PrimaryKeyConstraint("tenant_id", "container_id", "allowed_use_id"),
    sa.UniqueConstraint("tenant_id", "container_id", "rights_declaration_id", "use_kind"),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id"],
        ["containers.tenant_id", "containers.container_id"],
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id", "rights_declaration_id"],
        [
            "rights_declarations.tenant_id",
            "rights_declarations.container_id",
            "rights_declarations.rights_declaration_id",
        ],
        ondelete="RESTRICT",
    ),
    sa.CheckConstraint(
        "use_kind IN ('ANALYSIS', 'RETRIEVAL', 'DRAFTING', 'TRAINING', 'PUBLICATION')",
        name="use_kind_known",
    ),
    sa.CheckConstraint(
        "allowed_use_id <> '00000000-0000-0000-0000-000000000000'::uuid",
        name="allowed_use_non_nil",
    ),
    sa.CheckConstraint(
        "declared_by <> '00000000-0000-0000-0000-000000000000'::uuid",
        name="declared_by_non_nil",
    ),
)

lineage_anchors = sa.Table(
    "lineage_anchors",
    metadata,
    *_scope_columns(),
    sa.Column("lineage_anchor_id", UUID(as_uuid=True), nullable=False),
    sa.Column("segment_id", UUID(as_uuid=True), nullable=False),
    sa.Column("artifact_version_id", UUID(as_uuid=True), nullable=False),
    sa.Column("lineage_kind", sa.Text, nullable=False),
    sa.Column("target_ref", sa.Text, nullable=False),
    sa.Column("created_by", UUID(as_uuid=True), nullable=False),
    sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.PrimaryKeyConstraint("tenant_id", "container_id", "lineage_anchor_id"),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id"],
        ["containers.tenant_id", "containers.container_id"],
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ["tenant_id", "container_id", "segment_id", "artifact_version_id"],
        [
            "source_segments.tenant_id",
            "source_segments.container_id",
            "source_segments.segment_id",
            "source_segments.artifact_version_id",
        ],
        ondelete="RESTRICT",
    ),
    sa.CheckConstraint(
        "lineage_kind IN ('SOURCE_EVIDENCE', 'INSPIRATION_LINEAGE')", name="lineage_kind_known"
    ),
    sa.CheckConstraint("target_ref <> ''", name="target_ref_nonempty"),
    sa.CheckConstraint(
        "lineage_anchor_id <> '00000000-0000-0000-0000-000000000000'::uuid",
        name="lineage_anchor_non_nil",
    ),
    sa.CheckConstraint(
        "created_by <> '00000000-0000-0000-0000-000000000000'::uuid",
        name="created_by_non_nil",
    ),
)

TENANT_ONLY_TABLES = frozenset({"tenants", "memberships"})
TENANT_SCOPED_TABLES = frozenset(metadata.tables)
OWNERSHIP_SCOPE_TABLES = TENANT_SCOPED_TABLES - TENANT_ONLY_TABLES
IMMUTABLE_TABLES = frozenset(
    {
        "content_object_versions",
        "content_revisions",
        "revision_memberships",
        "legacy_archive_registry",
        "operation_events",
        "artifact_versions",
        "source_segments",
        "raw_triage_outputs",
        "triage_verdicts",
        "human_triage_decisions",
        "rights_declarations",
        "allowed_uses",
        "lineage_anchors",
    }
)

__all__ = [
    "CONTAINER_GUC",
    "EXPECTED_ALEMBIC_HEAD",
    "IMMUTABLE_TABLES",
    "MIGRATION_ROLE",
    "OWNERSHIP_SCOPE_TABLES",
    "RUNTIME_ROLE",
    "TENANT_GUC",
    "TENANT_ONLY_TABLES",
    "TENANT_SCOPED_TABLES",
    "bootstrap_import_runs",
    "allowed_uses",
    "artifact_versions",
    "containers",
    "content_object_versions",
    "content_revisions",
    "legacy_archive_registry",
    "human_triage_decisions",
    "intake_runs",
    "lineage_anchors",
    "memberships",
    "metadata",
    "operation_events",
    "release_pointers",
    "raw_triage_outputs",
    "revision_memberships",
    "rights_declarations",
    "source_artifacts",
    "source_segments",
    "tenants",
    "triage_verdicts",
]
