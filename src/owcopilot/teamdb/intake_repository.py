"""PostgreSQL repository for durable N2.1 source intake truth.

The repository deliberately does not trust role claims, caller-supplied hashes, or
client snapshots.  Every public operation enters a scoped PostgreSQL transaction,
re-reads the active membership, and reconstructs the run from immutable rows.
"""

from __future__ import annotations

import hashlib
import hmac
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Literal, TypeAlias, cast
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import Table, and_, func, select, update
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.sql.elements import ColumnElement

from ..contracts import ArtifactIdentity, OwnershipScope
from ..intake.contracts import (
    PDF_PROCESSING_FAILURE_CODES,
    IntakeRun,
    SourceSegment,
    TriageVerdict,
    validate_verdict_against_segment,
)
from ..intake.parsers import DOCX_MIME, PDF_MIME, XLSX_MIME, ValidatedUpload
from ..intake.service import (
    ALLOWED_USE_KINDS,
    RightsProvenance,
    SegmentDecision,
    UseKind,
)
from .errors import (
    TeamAuthorizationError,
    TeamDatabaseError,
    TeamIntakeConflict,
    TeamIntegrityError,
    TeamNotFoundError,
)
from .schema import (
    allowed_uses as allowed_uses_table,
)
from .schema import (
    artifact_versions,
    human_triage_decisions,
    intake_runs,
    lineage_anchors,
    raw_triage_outputs,
    rights_declarations,
    source_artifacts,
    source_segments,
    triage_verdicts,
)
from .schema import memberships as membership_table
from .session import TeamDatabase

LineageKind: TypeAlias = Literal["SOURCE_EVIDENCE", "INSPIRATION_LINEAGE"]

_MEMBERSHIP_ROLES = frozenset({"owner", "editor", "reviewer", "viewer"})
_WRITE_ROLES = frozenset({"owner", "editor"})
_RIGHTS_PROVENANCE = frozenset({"OWNED", "LICENSED", "THIRD_PARTY", "UNKNOWN"})
_LINEAGE_KINDS = frozenset({"SOURCE_EVIDENCE", "INSPIRATION_LINEAGE"})
_FORMAT_PAIRS = {".docx": DOCX_MIME, ".pdf": PDF_MIME, ".xlsx": XLSX_MIME}


def _exact_text(value: object, *, label: str, allow_empty: bool = False) -> str:
    if type(value) is not str or (not allow_empty and not value):
        qualifier = "a string" if allow_empty else "a non-empty string"
        raise ValueError(f"{label} must be exactly {qualifier}")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} contains a lone Unicode surrogate") from exc
    return value


def _canonical_uuid(value: object, *, label: str) -> str:
    text = _exact_text(value, label=label)
    try:
        parsed = UUID(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical UUID string") from exc
    if parsed.int == 0 or str(parsed) != text:
        raise ValueError(f"{label} must be a canonical non-nil UUID string")
    return text


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _serialize_datetime(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TeamIntegrityError(f"stored {label} is not a timezone-aware timestamp")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _scope_predicate(table: Table, scope: OwnershipScope) -> ColumnElement[bool]:
    return and_(
        table.c.tenant_id == UUID(scope.tenant_id),
        table.c.container_id == UUID(scope.container_id),
    )


class TeamIntakeRepository:
    """RLS-scoped repository implementing the N2.1 ``IntakeRepository`` protocol."""

    def __init__(self, database: TeamDatabase) -> None:
        self.database = database

    @contextmanager
    def _member_transaction(
        self,
        scope: OwnershipScope,
        *,
        subject_id: str,
        write: bool,
    ) -> Iterator[Connection]:
        checked_scope = OwnershipScope.model_validate(scope)
        subject = _canonical_uuid(subject_id, label="subject_id")
        try:
            with self.database.transaction(checked_scope) as connection:
                self._require_membership(
                    connection,
                    tenant_id=checked_scope.tenant_id,
                    subject_id=subject,
                    write=write,
                )
                yield connection
        except TeamDatabaseError:
            raise
        except IntegrityError as exc:
            raise TeamIntakeConflict("team intake truth conflicts with an existing row") from exc
        except SQLAlchemyError as exc:
            raise TeamDatabaseError("team intake database operation failed") from exc

    def create_uploaded_run(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        artifact_id: str,
        artifact_version_id: str,
        run_id: str,
        upload: ValidatedUpload,
    ) -> None:
        actor = _canonical_uuid(actor_subject, label="actor_subject")
        artifact = _canonical_uuid(artifact_id, label="artifact_id")
        version = _canonical_uuid(artifact_version_id, label="artifact_version_id")
        run = _canonical_uuid(run_id, label="run_id")
        checked_upload = self._validate_upload(upload)
        with self._member_transaction(scope, subject_id=actor, write=True) as connection:
            connection.execute(
                source_artifacts.insert().values(
                    tenant_id=UUID(scope.tenant_id),
                    container_id=UUID(scope.container_id),
                    artifact_id=UUID(artifact),
                    display_name=checked_upload.original_filename,
                    created_by=UUID(actor),
                )
            )
            connection.execute(
                artifact_versions.insert().values(
                    tenant_id=UUID(scope.tenant_id),
                    container_id=UUID(scope.container_id),
                    artifact_version_id=UUID(version),
                    artifact_id=UUID(artifact),
                    content_hash=hashlib.sha256(checked_upload.content_bytes).hexdigest(),
                    mime_type=checked_upload.mime_type,
                    extension=checked_upload.extension,
                    byte_size=len(checked_upload.content_bytes),
                    content_bytes=checked_upload.content_bytes,
                    created_by=UUID(actor),
                )
            )
            connection.execute(
                intake_runs.insert().values(
                    tenant_id=UUID(scope.tenant_id),
                    container_id=UUID(scope.container_id),
                    run_id=UUID(run),
                    artifact_version_id=UUID(version),
                    original_filename=checked_upload.original_filename,
                    status="UPLOADED",
                    created_by=UUID(actor),
                )
            )

    def create_replacement_uploaded_run(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        artifact_id: str,
        artifact_version_id: str,
        run_id: str,
        upload: ValidatedUpload,
    ) -> None:
        """Attach new immutable bytes and a fresh run to an existing active artifact."""

        actor = _canonical_uuid(actor_subject, label="actor_subject")
        artifact = _canonical_uuid(artifact_id, label="artifact_id")
        version = _canonical_uuid(artifact_version_id, label="artifact_version_id")
        run = _canonical_uuid(run_id, label="run_id")
        checked_upload = self._validate_upload(upload)
        content_hash = hashlib.sha256(checked_upload.content_bytes).hexdigest()
        with self._member_transaction(scope, subject_id=actor, write=True) as connection:
            artifact_row = (
                connection.execute(
                    select(
                        source_artifacts.c.artifact_id,
                        source_artifacts.c.logical_deleted_at,
                    )
                    .where(
                        and_(
                            _scope_predicate(source_artifacts, scope),
                            source_artifacts.c.artifact_id == UUID(artifact),
                        )
                    )
                    .with_for_update(of=source_artifacts)
                )
                .mappings()
                .one_or_none()
            )
            if artifact_row is None:
                raise TeamNotFoundError(
                    "source artifact does not exist in the requested ownership scope"
                )
            if artifact_row["logical_deleted_at"] is not None:
                raise TeamIntakeConflict("a logically deleted source artifact cannot be replaced")
            duplicate = connection.execute(
                select(artifact_versions.c.artifact_version_id).where(
                    and_(
                        _scope_predicate(artifact_versions, scope),
                        artifact_versions.c.artifact_id == UUID(artifact),
                        artifact_versions.c.content_hash == content_hash,
                    )
                )
            ).first()
            if duplicate is not None:
                raise TeamIntakeConflict(
                    "the source artifact already has a version with these exact bytes"
                )
            connection.execute(
                artifact_versions.insert().values(
                    tenant_id=UUID(scope.tenant_id),
                    container_id=UUID(scope.container_id),
                    artifact_version_id=UUID(version),
                    artifact_id=UUID(artifact),
                    content_hash=content_hash,
                    mime_type=checked_upload.mime_type,
                    extension=checked_upload.extension,
                    byte_size=len(checked_upload.content_bytes),
                    content_bytes=checked_upload.content_bytes,
                    created_by=UUID(actor),
                )
            )
            connection.execute(
                intake_runs.insert().values(
                    tenant_id=UUID(scope.tenant_id),
                    container_id=UUID(scope.container_id),
                    run_id=UUID(run),
                    artifact_version_id=UUID(version),
                    original_filename=checked_upload.original_filename,
                    status="UPLOADED",
                    created_by=UUID(actor),
                )
            )

    def store_segments(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        run_id: str,
        artifact_version_id: str,
        segments: tuple[SourceSegment, ...],
    ) -> None:
        actor = _canonical_uuid(actor_subject, label="actor_subject")
        run = _canonical_uuid(run_id, label="run_id")
        version = _canonical_uuid(artifact_version_id, label="artifact_version_id")
        checked_segments = self._validate_segments(segments, artifact_version_id=version)
        with self._member_transaction(scope, subject_id=actor, write=True) as connection:
            run_row = self._load_run(connection, scope=scope, run_id=run, for_update=True)
            self._require_active_run_in(
                run_row,
                expected_statuses=frozenset({"UPLOADED", "PROCESSING_FAILED"}),
            )
            self._require_run_version(run_row, version)
            connection.execute(
                source_segments.insert(),
                [
                    {
                        "tenant_id": UUID(scope.tenant_id),
                        "container_id": UUID(scope.container_id),
                        "segment_id": UUID(segment.segment_id),
                        "artifact_version_id": UUID(version),
                        "ordinal": segment.ordinal,
                        "locator_kind": segment.locator.kind,
                        "locator_jsonb": segment.locator.model_dump(mode="json"),
                        "text_content": segment.text,
                        "text_hash": _sha256_text(segment.text),
                        "untrusted_input": True,
                    }
                    for segment in checked_segments
                ],
            )
            result = connection.execute(
                update(intake_runs)
                .where(
                    and_(
                        _scope_predicate(intake_runs, scope),
                        intake_runs.c.run_id == UUID(run),
                        intake_runs.c.status.in_(("UPLOADED", "PROCESSING_FAILED")),
                    )
                )
                .values(status="SEGMENTED", processing_code=None)
            )
            if result.rowcount != 1:
                raise TeamIntakeConflict(
                    "intake run was not in UPLOADED or PROCESSING_FAILED state"
                )

    def mark_ocr_required(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        run_id: str,
    ) -> None:
        """Persist a resumable PDF handoff without inventing OCR output or segments."""

        actor = _canonical_uuid(actor_subject, label="actor_subject")
        run = _canonical_uuid(run_id, label="run_id")
        with self._member_transaction(scope, subject_id=actor, write=True) as connection:
            run_row = self._load_run(connection, scope=scope, run_id=run, for_update=True)
            self._require_active_run_in(
                run_row,
                expected_statuses=frozenset({"UPLOADED", "PROCESSING_FAILED"}),
            )
            if str(run_row["extension"]) != ".pdf":
                raise TeamIntegrityError("only a PDF intake run can require PDF OCR")
            result = connection.execute(
                update(intake_runs)
                .where(
                    and_(
                        _scope_predicate(intake_runs, scope),
                        intake_runs.c.run_id == UUID(run),
                        intake_runs.c.status.in_(("UPLOADED", "PROCESSING_FAILED")),
                    )
                )
                .values(status="OCR_REQUIRED", processing_code="PDF_OCR_REQUIRED")
            )
            if result.rowcount != 1:
                raise TeamIntakeConflict(
                    "intake run was not in UPLOADED or PROCESSING_FAILED state"
                )

    def mark_processing_failed(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        run_id: str,
        processing_code: str,
    ) -> None:
        """Persist a sanitized, retryable PDF processing failure with raw bytes retained."""

        actor = _canonical_uuid(actor_subject, label="actor_subject")
        run = _canonical_uuid(run_id, label="run_id")
        code = _exact_text(processing_code, label="processing_code")
        if code not in PDF_PROCESSING_FAILURE_CODES:
            raise ValueError("processing_code is not an accepted PDF processing failure")
        with self._member_transaction(scope, subject_id=actor, write=True) as connection:
            run_row = self._load_run(connection, scope=scope, run_id=run, for_update=True)
            self._require_active_run_in(
                run_row,
                expected_statuses=frozenset({"UPLOADED", "PROCESSING_FAILED"}),
            )
            if str(run_row["extension"]) != ".pdf":
                raise TeamIntegrityError("only a PDF intake run can carry a PDF processing failure")
            result = connection.execute(
                update(intake_runs)
                .where(
                    and_(
                        _scope_predicate(intake_runs, scope),
                        intake_runs.c.run_id == UUID(run),
                        intake_runs.c.status.in_(("UPLOADED", "PROCESSING_FAILED")),
                    )
                )
                .values(status="PROCESSING_FAILED", processing_code=code)
            )
            if result.rowcount != 1:
                raise TeamIntakeConflict(
                    "intake run was not in UPLOADED or PROCESSING_FAILED state"
                )

    def store_raw_triage_output(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        run_id: str,
        artifact_version_id: str,
        segment_id: str,
        raw_output_id: str,
        provider: str,
        model: str,
        output_text: str,
        output_hash: str,
    ) -> None:
        actor = _canonical_uuid(actor_subject, label="actor_subject")
        run = _canonical_uuid(run_id, label="run_id")
        version = _canonical_uuid(artifact_version_id, label="artifact_version_id")
        segment = _canonical_uuid(segment_id, label="segment_id")
        raw_id = _canonical_uuid(raw_output_id, label="raw_output_id")
        provider_text = _exact_text(provider, label="provider")
        model_text = _exact_text(model, label="model")
        raw_text = _exact_text(output_text, label="output_text", allow_empty=True)
        supplied_hash = _exact_text(output_hash, label="output_hash")
        observed_hash = _sha256_text(raw_text)
        if not hmac.compare_digest(supplied_hash, observed_hash):
            raise TeamIntegrityError("raw output hash does not match exact UTF-8 output text")
        with self._member_transaction(scope, subject_id=actor, write=True) as connection:
            run_row = self._load_run(connection, scope=scope, run_id=run, for_update=False)
            self._require_active_run(run_row, expected_status="SEGMENTED")
            self._require_run_version(run_row, version)
            segment_model = self._load_segment(
                connection,
                scope=scope,
                segment_id=segment,
                artifact_version_id=version,
            )
            self._reject_existing_segment_truth(
                connection,
                scope=scope,
                table=raw_triage_outputs,
                segment_id=segment_model.segment_id,
                label="raw triage output",
            )
            connection.execute(
                raw_triage_outputs.insert().values(
                    tenant_id=UUID(scope.tenant_id),
                    container_id=UUID(scope.container_id),
                    raw_output_id=UUID(raw_id),
                    run_id=UUID(run),
                    segment_id=UUID(segment),
                    artifact_version_id=UUID(version),
                    provider=provider_text,
                    model=model_text,
                    output_text=raw_text,
                    output_hash=observed_hash,
                )
            )

    def store_triage_verdict(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        run_id: str,
        artifact_version_id: str,
        verdict: TriageVerdict,
    ) -> None:
        actor = _canonical_uuid(actor_subject, label="actor_subject")
        run = _canonical_uuid(run_id, label="run_id")
        version = _canonical_uuid(artifact_version_id, label="artifact_version_id")
        try:
            checked_verdict = TriageVerdict.model_validate(verdict)
        except (TypeError, ValueError, ValidationError) as exc:
            raise TeamIntegrityError("triage verdict violates its N2.1 contract") from exc
        with self._member_transaction(scope, subject_id=actor, write=True) as connection:
            run_row = self._load_run(connection, scope=scope, run_id=run, for_update=False)
            self._require_active_run(run_row, expected_status="SEGMENTED")
            self._require_run_version(run_row, version)
            segment = self._load_segment(
                connection,
                scope=scope,
                segment_id=checked_verdict.segment_id,
                artifact_version_id=version,
            )
            raw_row = self._load_raw_output(
                connection,
                scope=scope,
                raw_output_id=checked_verdict.raw_output_id,
            )
            if (
                str(raw_row["run_id"]) != run
                or str(raw_row["artifact_version_id"]) != version
                or str(raw_row["segment_id"]) != segment.segment_id
            ):
                raise TeamIntegrityError("raw output, run, segment, and artifact version disagree")
            try:
                validate_verdict_against_segment(checked_verdict, segment)
            except (TypeError, ValueError, ValidationError) as exc:
                raise TeamIntegrityError(
                    "triage evidence does not bind the immutable segment"
                ) from exc
            self._reject_existing_segment_truth(
                connection,
                scope=scope,
                table=triage_verdicts,
                segment_id=segment.segment_id,
                label="derived triage verdict",
            )
            connection.execute(
                triage_verdicts.insert().values(
                    tenant_id=UUID(scope.tenant_id),
                    container_id=UUID(scope.container_id),
                    verdict_id=UUID(checked_verdict.verdict_id),
                    raw_output_id=UUID(checked_verdict.raw_output_id),
                    segment_id=UUID(checked_verdict.segment_id),
                    artifact_version_id=UUID(version),
                    content_role=checked_verdict.content_role,
                    confidence=checked_verdict.confidence,
                    policy_threshold=checked_verdict.policy_threshold,
                    evidence_spans_jsonb=[
                        item.model_dump(mode="json") for item in checked_verdict.evidence_spans
                    ],
                    abstain=checked_verdict.abstain,
                    abstain_reason=checked_verdict.abstain_reason,
                    derivation_policy_version=checked_verdict.derivation_policy_version,
                )
            )

    def mark_awaiting_human(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        run_id: str,
    ) -> None:
        actor = _canonical_uuid(actor_subject, label="actor_subject")
        run = _canonical_uuid(run_id, label="run_id")
        with self._member_transaction(scope, subject_id=actor, write=True) as connection:
            run_row = self._load_run(connection, scope=scope, run_id=run, for_update=True)
            self._require_active_run(run_row, expected_status="SEGMENTED")
            segments = self._load_segments(
                connection,
                scope=scope,
                artifact_version_id=str(run_row["artifact_version_id"]),
            )
            raw_rows = self._load_raw_outputs(connection, scope=scope, run_id=run)
            verdict_models = self._load_verdicts(
                connection,
                scope=scope,
                artifact_version_id=str(run_row["artifact_version_id"]),
                segments=segments,
                raw_rows=raw_rows,
            )
            segment_ids = {item.segment_id for item in segments}
            if not segment_ids:
                raise TeamIntegrityError("an intake run cannot await humans without segments")
            if Counter(str(row["segment_id"]) for row in raw_rows) != Counter(segment_ids):
                raise TeamIntegrityError("every segment requires exactly one persisted raw output")
            if Counter(item.segment_id for item in verdict_models) != Counter(segment_ids):
                raise TeamIntegrityError("every segment requires exactly one derived verdict")
            result = connection.execute(
                update(intake_runs)
                .where(
                    and_(
                        _scope_predicate(intake_runs, scope),
                        intake_runs.c.run_id == UUID(run),
                        intake_runs.c.status == "SEGMENTED",
                    )
                )
                .values(status="AWAITING_HUMAN", finished_at=func.now())
            )
            if result.rowcount != 1:
                raise TeamIntakeConflict("intake run was not in SEGMENTED state")

    def read_intake_run(
        self,
        scope: OwnershipScope,
        *,
        subject_id: str,
        run_id: str,
    ) -> Mapping[str, object]:
        subject = _canonical_uuid(subject_id, label="subject_id")
        run = _canonical_uuid(run_id, label="run_id")
        with self._member_transaction(scope, subject_id=subject, write=False) as connection:
            return self._read_intake_run_connection(connection, scope=scope, run_id=run)

    def declare_rights(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        run_id: str,
        rights_declaration_id: str,
        rights_provenance: RightsProvenance,
        declaration_text: str,
        allowed_uses: Mapping[UseKind, bool],
    ) -> Mapping[str, object]:
        actor = _canonical_uuid(actor_subject, label="actor_subject")
        run = _canonical_uuid(run_id, label="run_id")
        declaration_id = _canonical_uuid(rights_declaration_id, label="rights_declaration_id")
        provenance = _exact_text(rights_provenance, label="rights_provenance")
        if provenance not in _RIGHTS_PROVENANCE:
            raise ValueError("rights_provenance is not supported")
        declaration = _exact_text(declaration_text, label="declaration_text")
        normalized_uses = self._normalize_allowed_uses(allowed_uses)
        with self._member_transaction(scope, subject_id=actor, write=True) as connection:
            run_row = self._load_run(connection, scope=scope, run_id=run, for_update=True)
            self._require_active_run(run_row, expected_status="AWAITING_HUMAN")
            version = str(run_row["artifact_version_id"])
            connection.execute(
                rights_declarations.insert().values(
                    tenant_id=UUID(scope.tenant_id),
                    container_id=UUID(scope.container_id),
                    rights_declaration_id=UUID(declaration_id),
                    artifact_version_id=UUID(version),
                    rights_provenance=provenance,
                    declaration_text=declaration,
                    declared_by=UUID(actor),
                    human_asserted=True,
                )
            )
            connection.execute(
                allowed_uses_table.insert(),
                [
                    {
                        "tenant_id": UUID(scope.tenant_id),
                        "container_id": UUID(scope.container_id),
                        "allowed_use_id": uuid4(),
                        "rights_declaration_id": UUID(declaration_id),
                        "use_kind": use_kind,
                        "allowed": normalized_uses[use_kind],
                        "declared_by": UUID(actor),
                    }
                    for use_kind in ALLOWED_USE_KINDS
                ],
            )
            return self._read_intake_run_connection(connection, scope=scope, run_id=run)

    def submit_human_decisions(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        run_id: str,
        decisions: tuple[SegmentDecision, ...],
    ) -> Mapping[str, object]:
        actor = _canonical_uuid(actor_subject, label="actor_subject")
        run = _canonical_uuid(run_id, label="run_id")
        checked_decisions = self._validate_decisions(decisions)
        with self._member_transaction(scope, subject_id=actor, write=True) as connection:
            run_row = self._load_run(connection, scope=scope, run_id=run, for_update=True)
            self._require_active_run(run_row, expected_status="AWAITING_HUMAN")
            version = str(run_row["artifact_version_id"])
            segments = self._load_segments(
                connection,
                scope=scope,
                artifact_version_id=version,
            )
            raw_rows = self._load_raw_outputs(connection, scope=scope, run_id=run)
            verdict_models = self._load_verdicts(
                connection,
                scope=scope,
                artifact_version_id=version,
                segments=segments,
                raw_rows=raw_rows,
            )
            rights, uses = self._load_rights(connection, scope=scope, artifact_version_id=version)
            if rights is None or set(uses) != set(ALLOWED_USE_KINDS):
                raise TeamIntakeConflict(
                    "a complete human rights declaration is required before decisions"
                )
            segment_ids = {item.segment_id for item in segments}
            decision_ids = {item.segment_id for item in checked_decisions}
            if decision_ids != segment_ids or len(checked_decisions) != len(segment_ids):
                raise TeamIntakeConflict("human decisions must cover every segment exactly once")
            verdict_by_segment = {item.segment_id: item for item in verdict_models}
            if set(verdict_by_segment) != segment_ids or len(verdict_models) != len(segment_ids):
                raise TeamIntegrityError("each segment must have exactly one server-stored verdict")
            for decision in checked_decisions:
                verdict = verdict_by_segment[decision.segment_id]
                if decision.decision == "CONFIRM" and decision.content_role != verdict.content_role:
                    raise TeamIntakeConflict(
                        "CONFIRM decisions must retain the server-stored derived content role"
                    )
            existing = connection.execute(
                select(human_triage_decisions.c.segment_id).where(
                    and_(
                        _scope_predicate(human_triage_decisions, scope),
                        human_triage_decisions.c.run_id == UUID(run),
                    )
                )
            ).first()
            if existing is not None:
                raise TeamIntakeConflict("human decisions were already submitted for this run")
            connection.execute(
                human_triage_decisions.insert(),
                [
                    {
                        "tenant_id": UUID(scope.tenant_id),
                        "container_id": UUID(scope.container_id),
                        "decision_id": uuid4(),
                        "run_id": UUID(run),
                        "verdict_id": UUID(verdict_by_segment[item.segment_id].verdict_id),
                        "segment_id": UUID(item.segment_id),
                        "artifact_version_id": UUID(version),
                        "decision": item.decision,
                        "content_role": item.content_role,
                        "decided_by": UUID(actor),
                        "rationale": item.rationale,
                    }
                    for item in checked_decisions
                ],
            )
            return self._read_intake_run_connection(connection, scope=scope, run_id=run)

    def logical_delete_source_artifact(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        run_id: str,
    ) -> Mapping[str, object]:
        """One-way logical deletion; immutable bytes and locators remain readable."""

        actor = _canonical_uuid(actor_subject, label="actor_subject")
        run = _canonical_uuid(run_id, label="run_id")
        with self._member_transaction(scope, subject_id=actor, write=True) as connection:
            run_row = self._load_run(connection, scope=scope, run_id=run, for_update=True)
            if run_row["logical_deleted_at"] is not None:
                raise TeamIntakeConflict("source artifact is already logically deleted")
            result = connection.execute(
                update(source_artifacts)
                .where(
                    and_(
                        _scope_predicate(source_artifacts, scope),
                        source_artifacts.c.artifact_id == run_row["artifact_id"],
                        source_artifacts.c.logical_deleted_at.is_(None),
                    )
                )
                .values(logical_deleted_at=func.now())
            )
            if result.rowcount != 1:
                raise TeamIntakeConflict("source artifact logical-delete CAS lost")
            return self._read_intake_run_connection(connection, scope=scope, run_id=run)

    def create_lineage_anchor(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        run_id: str,
        segment_id: str,
        lineage_kind: LineageKind,
        target_ref: str,
    ) -> Mapping[str, object]:
        actor = _canonical_uuid(actor_subject, label="actor_subject")
        run = _canonical_uuid(run_id, label="run_id")
        segment = _canonical_uuid(segment_id, label="segment_id")
        kind = _exact_text(lineage_kind, label="lineage_kind")
        if kind not in _LINEAGE_KINDS:
            raise ValueError("lineage_kind is not supported")
        target = _exact_text(target_ref, label="target_ref")
        with self._member_transaction(scope, subject_id=actor, write=True) as connection:
            run_row = self._load_run(connection, scope=scope, run_id=run, for_update=True)
            self._require_active_run(run_row, expected_status="AWAITING_HUMAN")
            version = str(run_row["artifact_version_id"])
            selected_segment = self._load_segment(
                connection,
                scope=scope,
                segment_id=segment,
                artifact_version_id=version,
            )
            all_segments = self._load_segments(
                connection,
                scope=scope,
                artifact_version_id=version,
            )
            decision_rows = (
                connection.execute(
                    select(
                        human_triage_decisions.c.segment_id,
                        human_triage_decisions.c.artifact_version_id,
                        human_triage_decisions.c.decision,
                        human_triage_decisions.c.content_role,
                    ).where(
                        and_(
                            _scope_predicate(human_triage_decisions, scope),
                            human_triage_decisions.c.run_id == UUID(run),
                        )
                    )
                )
                .mappings()
                .all()
            )
            expected_segment_ids = {item.segment_id for item in all_segments}
            decided_segment_ids = {str(row["segment_id"]) for row in decision_rows}
            if (
                len(decision_rows) != len(expected_segment_ids)
                or decided_segment_ids != expected_segment_ids
            ):
                raise TeamIntakeConflict(
                    "lineage requires complete human decisions for every source segment"
                )
            final_row = next(
                (
                    row
                    for row in decision_rows
                    if str(row["segment_id"]) == selected_segment.segment_id
                ),
                None,
            )
            if final_row is None or str(final_row["artifact_version_id"]) != version:
                raise TeamIntegrityError("human decision and source artifact version disagree")
            final_decision = str(final_row["decision"])
            final_role = str(final_row["content_role"])
            if final_decision == "REJECT" or final_role == "UNKNOWN":
                raise TeamIntakeConflict(
                    "rejected or UNKNOWN human decisions cannot create lineage"
                )
            if kind == "SOURCE_EVIDENCE" and final_role != "CANON_CANDIDATE":
                raise TeamIntakeConflict(
                    "SOURCE_EVIDENCE requires a final human CANON_CANDIDATE decision"
                )
            if kind == "INSPIRATION_LINEAGE" and final_role not in {"REFERENCE", "MIXED"}:
                raise TeamIntakeConflict(
                    "INSPIRATION_LINEAGE requires a final human REFERENCE or MIXED decision"
                )
            connection.execute(
                lineage_anchors.insert().values(
                    tenant_id=UUID(scope.tenant_id),
                    container_id=UUID(scope.container_id),
                    lineage_anchor_id=uuid4(),
                    segment_id=UUID(segment),
                    artifact_version_id=UUID(version),
                    lineage_kind=kind,
                    target_ref=target,
                    created_by=UUID(actor),
                )
            )
            return self._read_intake_run_connection(connection, scope=scope, run_id=run)

    def is_use_allowed(
        self,
        scope: OwnershipScope,
        *,
        subject_id: str,
        run_id: str,
        use_kind: UseKind,
    ) -> bool:
        subject = _canonical_uuid(subject_id, label="subject_id")
        run = _canonical_uuid(run_id, label="run_id")
        kind = _exact_text(use_kind, label="use_kind")
        if kind not in ALLOWED_USE_KINDS:
            raise ValueError("use_kind is not supported")
        with self._member_transaction(scope, subject_id=subject, write=False) as connection:
            run_row = self._load_run(connection, scope=scope, run_id=run, for_update=False)
            if run_row["logical_deleted_at"] is not None:
                return False
            _rights, uses = self._load_rights(
                connection,
                scope=scope,
                artifact_version_id=str(run_row["artifact_version_id"]),
            )
            return uses.get(kind, False)

    @staticmethod
    def _validate_upload(upload: ValidatedUpload) -> ValidatedUpload:
        if not isinstance(upload, ValidatedUpload):
            raise TypeError("upload must be a ValidatedUpload")
        filename = _exact_text(upload.original_filename, label="original_filename")
        if "/" in filename or "\\" in filename or filename in {".", ".."}:
            raise ValueError("original_filename must be a basename")
        extension = _exact_text(upload.extension, label="extension")
        mime_type = _exact_text(upload.mime_type, label="mime_type")
        if extension not in _FORMAT_PAIRS or _FORMAT_PAIRS[extension] != mime_type:
            raise TeamIntegrityError("upload extension and MIME type are not an accepted pair")
        if not filename.casefold().endswith(extension):
            raise TeamIntegrityError("upload filename and accepted extension disagree")
        if type(upload.content_bytes) is not bytes or not upload.content_bytes:
            raise TeamIntegrityError("upload content must be non-empty exact bytes")
        observed_size = len(upload.content_bytes)
        observed_hash = hashlib.sha256(upload.content_bytes).hexdigest()
        if type(upload.byte_size) is not int or upload.byte_size != observed_size:
            raise TeamIntegrityError("upload byte size does not match exact content bytes")
        if type(upload.content_hash) is not str or not hmac.compare_digest(
            upload.content_hash, observed_hash
        ):
            raise TeamIntegrityError("upload hash does not match exact content bytes")
        return upload

    @staticmethod
    def _validate_segments(
        segments: tuple[SourceSegment, ...],
        *,
        artifact_version_id: str,
    ) -> tuple[SourceSegment, ...]:
        if type(segments) is not tuple or not segments:
            raise TeamIntegrityError("source segments must be a non-empty tuple")
        try:
            checked = tuple(SourceSegment.model_validate(item) for item in segments)
        except (TypeError, ValueError, ValidationError) as exc:
            raise TeamIntegrityError("source segment violates its N2.1 contract") from exc
        if any(item.artifact_version_id != artifact_version_id for item in checked):
            raise TeamIntegrityError("source segment and artifact version disagree")
        if len({item.segment_id for item in checked}) != len(checked):
            raise TeamIntegrityError("source segments contain a duplicate segment_id")
        if len({item.ordinal for item in checked}) != len(checked):
            raise TeamIntegrityError("source segments contain a duplicate ordinal")
        if tuple(sorted(item.ordinal for item in checked)) != tuple(range(len(checked))):
            raise TeamIntegrityError("source segment ordinals must be contiguous from zero")
        return tuple(sorted(checked, key=lambda item: item.ordinal))

    @staticmethod
    def _validate_decisions(
        decisions: tuple[SegmentDecision, ...],
    ) -> tuple[SegmentDecision, ...]:
        if type(decisions) is not tuple or not decisions:
            raise ValueError("human decisions must be a non-empty tuple")
        try:
            checked = tuple(
                SegmentDecision(
                    segment_id=item.segment_id,
                    decision=item.decision,
                    content_role=item.content_role,
                    rationale=item.rationale,
                )
                for item in decisions
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("human decisions contain an invalid item") from exc
        if len({item.segment_id for item in checked}) != len(checked):
            raise TeamIntakeConflict("human decisions contain a duplicate segment_id")
        return checked

    @staticmethod
    def _normalize_allowed_uses(supplied: Mapping[UseKind, bool]) -> dict[UseKind, bool]:
        if not isinstance(supplied, Mapping):
            raise ValueError("allowed_uses must be a mapping")
        normalized: dict[UseKind, bool] = {kind: False for kind in ALLOWED_USE_KINDS}
        for raw_kind, raw_allowed in supplied.items():
            kind = _exact_text(raw_kind, label="allowed use kind")
            if kind not in ALLOWED_USE_KINDS:
                raise ValueError("allowed_uses contains an unsupported use kind")
            if type(raw_allowed) is not bool:
                raise ValueError("allowed_uses values must be booleans")
            normalized[kind] = raw_allowed
        return normalized

    @staticmethod
    def _require_membership(
        connection: Connection,
        *,
        tenant_id: str,
        subject_id: str,
        write: bool,
    ) -> None:
        row = (
            connection.execute(
                select(membership_table.c.role, membership_table.c.active).where(
                    and_(
                        membership_table.c.tenant_id == UUID(tenant_id),
                        membership_table.c.subject_id == UUID(subject_id),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None or type(row["active"]) is not bool or not row["active"]:
            raise TeamAuthorizationError("no active membership for the requested tenant")
        role = str(row["role"])
        if role not in _MEMBERSHIP_ROLES:
            raise TeamAuthorizationError("membership has an unsupported role")
        if write and role not in _WRITE_ROLES:
            raise TeamAuthorizationError("an active editor or owner membership is required")

    @staticmethod
    def _require_run_version(row: RowMapping, artifact_version_id: str) -> None:
        if str(row["artifact_version_id"]) != artifact_version_id:
            raise TeamIntegrityError("run and artifact_version_id disagree")

    @staticmethod
    def _require_active_run(row: RowMapping, *, expected_status: str) -> None:
        if row["logical_deleted_at"] is not None:
            raise TeamIntakeConflict("source artifact is logically deleted")
        if str(row["status"]) != expected_status:
            raise TeamIntakeConflict(f"intake run must be in {expected_status} state")

    @staticmethod
    def _require_active_run_in(
        row: RowMapping,
        *,
        expected_statuses: frozenset[str],
    ) -> None:
        if row["logical_deleted_at"] is not None:
            raise TeamIntakeConflict("source artifact is logically deleted")
        if str(row["status"]) not in expected_statuses:
            expected = " or ".join(sorted(expected_statuses))
            raise TeamIntakeConflict(f"intake run must be in {expected} state")

    def _load_run(
        self,
        connection: Connection,
        *,
        scope: OwnershipScope,
        run_id: str,
        for_update: bool,
    ) -> RowMapping:
        statement = (
            select(
                intake_runs.c.run_id,
                intake_runs.c.artifact_version_id,
                intake_runs.c.original_filename,
                intake_runs.c.status,
                intake_runs.c.created_by.label("run_created_by"),
                intake_runs.c.started_at,
                intake_runs.c.finished_at,
                intake_runs.c.rejection_code,
                intake_runs.c.processing_code,
                artifact_versions.c.artifact_id,
                artifact_versions.c.content_hash,
                artifact_versions.c.mime_type,
                artifact_versions.c.extension,
                artifact_versions.c.byte_size,
                artifact_versions.c.content_bytes,
                artifact_versions.c.created_by.label("version_created_by"),
                artifact_versions.c.created_at.label("version_created_at"),
                source_artifacts.c.display_name,
                source_artifacts.c.created_by.label("artifact_created_by"),
                source_artifacts.c.created_at.label("artifact_created_at"),
                source_artifacts.c.logical_deleted_at,
            )
            .select_from(
                intake_runs.join(
                    artifact_versions,
                    and_(
                        intake_runs.c.tenant_id == artifact_versions.c.tenant_id,
                        intake_runs.c.container_id == artifact_versions.c.container_id,
                        intake_runs.c.artifact_version_id
                        == artifact_versions.c.artifact_version_id,
                    ),
                ).join(
                    source_artifacts,
                    and_(
                        artifact_versions.c.tenant_id == source_artifacts.c.tenant_id,
                        artifact_versions.c.container_id == source_artifacts.c.container_id,
                        artifact_versions.c.artifact_id == source_artifacts.c.artifact_id,
                    ),
                )
            )
            .where(
                and_(
                    _scope_predicate(intake_runs, scope),
                    intake_runs.c.run_id == UUID(run_id),
                )
            )
        )
        if for_update:
            statement = statement.with_for_update(of=intake_runs)
        row = connection.execute(statement).mappings().one_or_none()
        if row is None:
            raise TeamNotFoundError("intake run does not exist in the requested ownership scope")
        self._verify_run_row(row)
        return row

    @staticmethod
    def _verify_run_row(row: RowMapping) -> None:
        content = row["content_bytes"]
        if not isinstance(content, bytes | bytearray | memoryview):
            raise TeamIntegrityError("stored artifact bytes have an unsupported driver value")
        exact_bytes = bytes(content)
        observed_hash = hashlib.sha256(exact_bytes).hexdigest()
        if type(row["content_hash"]) is not str or not hmac.compare_digest(
            row["content_hash"], observed_hash
        ):
            raise TeamIntegrityError("stored artifact bytes do not match content_hash")
        if type(row["byte_size"]) is not int or row["byte_size"] != len(exact_bytes):
            raise TeamIntegrityError("stored artifact bytes do not match byte_size")
        extension = str(row["extension"])
        mime_type = str(row["mime_type"])
        if extension not in _FORMAT_PAIRS or _FORMAT_PAIRS[extension] != mime_type:
            raise TeamIntegrityError("stored artifact extension and MIME type disagree")
        try:
            IntakeRun.model_validate(
                {
                    "run_id": str(row["run_id"]),
                    "artifact_version_id": str(row["artifact_version_id"]),
                    "artifact": ArtifactIdentity.from_bytes(
                        exact_bytes,
                        artifact_id=str(row["artifact_version_id"]),
                        mime_type=mime_type,
                    ),
                    "original_filename": str(row["original_filename"]),
                    "extension": extension,
                    "byte_size": len(exact_bytes),
                    "status": str(row["status"]),
                    "created_by": str(row["run_created_by"]),
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "rejection_code": row["rejection_code"],
                    "processing_code": row["processing_code"],
                }
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise TeamIntegrityError("stored intake run violates its N2.1 contract") from exc

    def _load_segment(
        self,
        connection: Connection,
        *,
        scope: OwnershipScope,
        segment_id: str,
        artifact_version_id: str,
    ) -> SourceSegment:
        row = (
            connection.execute(
                select(source_segments).where(
                    and_(
                        _scope_predicate(source_segments, scope),
                        source_segments.c.segment_id == UUID(segment_id),
                        source_segments.c.artifact_version_id == UUID(artifact_version_id),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise TeamNotFoundError("source segment is not bound to the requested artifact version")
        return self._segment_from_row(row)

    def _load_segments(
        self,
        connection: Connection,
        *,
        scope: OwnershipScope,
        artifact_version_id: str,
    ) -> tuple[SourceSegment, ...]:
        rows = (
            connection.execute(
                select(source_segments)
                .where(
                    and_(
                        _scope_predicate(source_segments, scope),
                        source_segments.c.artifact_version_id == UUID(artifact_version_id),
                    )
                )
                .order_by(source_segments.c.ordinal)
            )
            .mappings()
            .all()
        )
        segments = tuple(self._segment_from_row(row) for row in rows)
        if segments and tuple(item.ordinal for item in segments) != tuple(range(len(segments))):
            raise TeamIntegrityError("stored source segment ordinals are not contiguous")
        return segments

    @staticmethod
    def _segment_from_row(row: RowMapping) -> SourceSegment:
        locator = row["locator_jsonb"]
        if not isinstance(locator, Mapping) or locator.get("kind") != row["locator_kind"]:
            raise TeamIntegrityError("stored source locator kind and JSON disagree")
        try:
            return SourceSegment.model_validate(
                {
                    "segment_id": str(row["segment_id"]),
                    "artifact_version_id": str(row["artifact_version_id"]),
                    "ordinal": row["ordinal"],
                    "locator": dict(locator),
                    "text": row["text_content"],
                    "text_hash": row["text_hash"],
                    "untrusted_input": row["untrusted_input"],
                }
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise TeamIntegrityError("stored source segment violates its N2.1 contract") from exc

    def _load_raw_output(
        self,
        connection: Connection,
        *,
        scope: OwnershipScope,
        raw_output_id: str,
    ) -> RowMapping:
        row = (
            connection.execute(
                select(raw_triage_outputs).where(
                    and_(
                        _scope_predicate(raw_triage_outputs, scope),
                        raw_triage_outputs.c.raw_output_id == UUID(raw_output_id),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise TeamNotFoundError("raw triage output does not exist")
        self._verify_raw_row(row)
        return row

    def _load_raw_outputs(
        self,
        connection: Connection,
        *,
        scope: OwnershipScope,
        run_id: str,
    ) -> tuple[RowMapping, ...]:
        rows = tuple(
            connection.execute(
                select(raw_triage_outputs)
                .where(
                    and_(
                        _scope_predicate(raw_triage_outputs, scope),
                        raw_triage_outputs.c.run_id == UUID(run_id),
                    )
                )
                .order_by(raw_triage_outputs.c.created_at, raw_triage_outputs.c.raw_output_id)
            )
            .mappings()
            .all()
        )
        for row in rows:
            self._verify_raw_row(row)
        return rows

    @staticmethod
    def _verify_raw_row(row: RowMapping) -> None:
        output_text = row["output_text"]
        if type(output_text) is not str:
            raise TeamIntegrityError("stored raw triage output is not exact text")
        observed_hash = _sha256_text(output_text)
        if type(row["output_hash"]) is not str or not hmac.compare_digest(
            row["output_hash"], observed_hash
        ):
            raise TeamIntegrityError("stored raw triage output does not match output_hash")

    def _load_verdicts(
        self,
        connection: Connection,
        *,
        scope: OwnershipScope,
        artifact_version_id: str,
        segments: tuple[SourceSegment, ...],
        raw_rows: tuple[RowMapping, ...],
    ) -> tuple[TriageVerdict, ...]:
        rows = (
            connection.execute(
                select(triage_verdicts)
                .where(
                    and_(
                        _scope_predicate(triage_verdicts, scope),
                        triage_verdicts.c.artifact_version_id == UUID(artifact_version_id),
                    )
                )
                .order_by(triage_verdicts.c.created_at, triage_verdicts.c.verdict_id)
            )
            .mappings()
            .all()
        )
        segment_by_id = {item.segment_id: item for item in segments}
        raw_by_id = {str(row["raw_output_id"]): row for row in raw_rows}
        verdicts: list[TriageVerdict] = []
        for row in rows:
            try:
                verdict = TriageVerdict.model_validate(
                    {
                        "verdict_id": str(row["verdict_id"]),
                        "raw_output_id": str(row["raw_output_id"]),
                        "segment_id": str(row["segment_id"]),
                        "content_role": row["content_role"],
                        "confidence": row["confidence"],
                        "policy_threshold": row["policy_threshold"],
                        "evidence_spans": tuple(row["evidence_spans_jsonb"]),
                        "abstain": row["abstain"],
                        "abstain_reason": row["abstain_reason"],
                        "derivation_policy_version": row["derivation_policy_version"],
                    }
                )
            except (TypeError, ValueError, ValidationError) as exc:
                raise TeamIntegrityError("stored triage verdict violates its contract") from exc
            segment = segment_by_id.get(verdict.segment_id)
            raw = raw_by_id.get(verdict.raw_output_id)
            if (
                segment is None
                or raw is None
                or str(raw["segment_id"]) != verdict.segment_id
                or str(raw["artifact_version_id"]) != artifact_version_id
                or str(row["artifact_version_id"]) != artifact_version_id
            ):
                raise TeamIntegrityError(
                    "stored verdict identity does not bind raw/segment/version"
                )
            try:
                validate_verdict_against_segment(verdict, segment)
            except (TypeError, ValueError, ValidationError) as exc:
                raise TeamIntegrityError(
                    "stored verdict evidence does not bind source text"
                ) from exc
            verdicts.append(verdict)
        return tuple(verdicts)

    def _load_rights(
        self,
        connection: Connection,
        *,
        scope: OwnershipScope,
        artifact_version_id: str,
    ) -> tuple[dict[str, object] | None, dict[UseKind, bool]]:
        row = (
            connection.execute(
                select(rights_declarations).where(
                    and_(
                        _scope_predicate(rights_declarations, scope),
                        rights_declarations.c.artifact_version_id == UUID(artifact_version_id),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None, {kind: False for kind in ALLOWED_USE_KINDS}
        if (
            row["human_asserted"] is not True
            or str(row["rights_provenance"]) not in _RIGHTS_PROVENANCE
            or type(row["declaration_text"]) is not str
            or not row["declaration_text"]
        ):
            raise TeamIntegrityError("stored rights declaration is not a complete human assertion")
        declaration_id = str(row["rights_declaration_id"])
        use_rows = (
            connection.execute(
                select(allowed_uses_table)
                .where(
                    and_(
                        _scope_predicate(allowed_uses_table, scope),
                        allowed_uses_table.c.rights_declaration_id == UUID(declaration_id),
                    )
                )
                .order_by(allowed_uses_table.c.use_kind)
            )
            .mappings()
            .all()
        )
        uses: dict[UseKind, bool] = {}
        for use_row in use_rows:
            kind = str(use_row["use_kind"])
            allowed = use_row["allowed"]
            if kind not in ALLOWED_USE_KINDS or type(allowed) is not bool:
                raise TeamIntegrityError("stored allowed-use row is invalid")
            if kind in uses:
                raise TeamIntegrityError("stored allowed-use map contains a duplicate kind")
            uses[kind] = allowed
        if set(uses) != set(ALLOWED_USE_KINDS):
            raise TeamIntegrityError("stored rights declaration must contain all five use kinds")
        rights = {
            "rights_declaration_id": declaration_id,
            "artifact_version_id": str(row["artifact_version_id"]),
            "rights_provenance": str(row["rights_provenance"]),
            "declaration_text": str(row["declaration_text"]),
            "declared_by": str(row["declared_by"]),
            "human_asserted": True,
            "declared_at": _serialize_datetime(row["declared_at"], label="declared_at"),
        }
        return rights, uses

    def _read_intake_run_connection(
        self,
        connection: Connection,
        *,
        scope: OwnershipScope,
        run_id: str,
    ) -> Mapping[str, object]:
        run_row = self._load_run(connection, scope=scope, run_id=run_id, for_update=False)
        version = str(run_row["artifact_version_id"])
        segments = self._load_segments(
            connection,
            scope=scope,
            artifact_version_id=version,
        )
        raw_rows = self._load_raw_outputs(connection, scope=scope, run_id=run_id)
        verdict_models = self._load_verdicts(
            connection,
            scope=scope,
            artifact_version_id=version,
            segments=segments,
            raw_rows=raw_rows,
        )
        decision_rows = (
            connection.execute(
                select(human_triage_decisions)
                .where(
                    and_(
                        _scope_predicate(human_triage_decisions, scope),
                        human_triage_decisions.c.run_id == UUID(run_id),
                    )
                )
                .order_by(
                    human_triage_decisions.c.decided_at,
                    human_triage_decisions.c.decision_id,
                )
            )
            .mappings()
            .all()
        )
        verdict_by_id = {item.verdict_id: item for item in verdict_models}
        decisions: list[dict[str, object]] = []
        seen_decision_segments: set[str] = set()
        for row in decision_rows:
            segment_id = str(row["segment_id"])
            verdict = verdict_by_id.get(str(row["verdict_id"]))
            if (
                verdict is None
                or verdict.segment_id != segment_id
                or str(row["artifact_version_id"]) != version
                or str(row["run_id"]) != run_id
                or segment_id in seen_decision_segments
            ):
                raise TeamIntegrityError("stored human decision identity is inconsistent")
            seen_decision_segments.add(segment_id)
            decisions.append(
                {
                    "decision_id": str(row["decision_id"]),
                    "run_id": run_id,
                    "verdict_id": verdict.verdict_id,
                    "segment_id": segment_id,
                    "artifact_version_id": version,
                    "decision": str(row["decision"]),
                    "content_role": str(row["content_role"]),
                    "decided_by": str(row["decided_by"]),
                    "rationale": str(row["rationale"]),
                    "decided_at": _serialize_datetime(row["decided_at"], label="decided_at"),
                }
            )
        expected_decision_segments = {item.segment_id for item in segments}
        if seen_decision_segments and seen_decision_segments != expected_decision_segments:
            raise TeamIntegrityError("stored human decisions do not cover every source segment")
        rights, use_map = self._load_rights(
            connection,
            scope=scope,
            artifact_version_id=version,
        )
        lineage_rows = (
            connection.execute(
                select(lineage_anchors)
                .where(
                    and_(
                        _scope_predicate(lineage_anchors, scope),
                        lineage_anchors.c.artifact_version_id == UUID(version),
                    )
                )
                .order_by(lineage_anchors.c.created_at, lineage_anchors.c.lineage_anchor_id)
            )
            .mappings()
            .all()
        )
        segment_ids = {item.segment_id for item in segments}
        lineage: list[dict[str, object]] = []
        for row in lineage_rows:
            kind = str(row["lineage_kind"])
            target_ref = row["target_ref"]
            if (
                str(row["segment_id"]) not in segment_ids
                or str(row["artifact_version_id"]) != version
                or kind not in _LINEAGE_KINDS
                or type(target_ref) is not str
                or not target_ref
            ):
                raise TeamIntegrityError("stored lineage anchor is inconsistent")
            lineage.append(
                {
                    "lineage_anchor_id": str(row["lineage_anchor_id"]),
                    "segment_id": str(row["segment_id"]),
                    "artifact_version_id": version,
                    "lineage_kind": kind,
                    "target_ref": target_ref,
                    "created_by": str(row["created_by"]),
                    "created_at": _serialize_datetime(row["created_at"], label="created_at"),
                }
            )
        logical_deleted_at = _serialize_datetime(
            run_row["logical_deleted_at"], label="logical_deleted_at"
        )
        artifact = {
            "artifact_id": str(run_row["artifact_id"]),
            "artifact_version_id": version,
            "display_name": str(run_row["display_name"]),
            "content_hash": str(run_row["content_hash"]),
            "mime_type": str(run_row["mime_type"]),
            "extension": str(run_row["extension"]),
            "byte_size": cast(int, run_row["byte_size"]),
            "created_by": str(run_row["version_created_by"]),
            "created_at": _serialize_datetime(run_row["version_created_at"], label="created_at"),
            "logical_deleted_at": logical_deleted_at,
            "immutable_bytes_retained": True,
        }
        run = {
            "run_id": run_id,
            "artifact_version_id": version,
            "original_filename": str(run_row["original_filename"]),
            "status": str(run_row["status"]),
            "created_by": str(run_row["run_created_by"]),
            "started_at": _serialize_datetime(run_row["started_at"], label="started_at"),
            "finished_at": _serialize_datetime(run_row["finished_at"], label="finished_at"),
            "rejection_code": run_row["rejection_code"],
            "processing_code": run_row["processing_code"],
        }
        return {
            "scope": scope.model_dump(mode="json"),
            "run_id": run_id,
            "artifact_version_id": version,
            "status": str(run_row["status"]),
            "processing_code": run_row["processing_code"],
            "run": run,
            "artifact": artifact,
            "segments": [item.model_dump(mode="json") for item in segments],
            "raw_triage_outputs": [self._raw_as_dict(row) for row in raw_rows],
            "triage_verdicts": [item.model_dump(mode="json") for item in verdict_models],
            "human_decisions": decisions,
            "submitted": bool(decisions),
            "rights": rights,
            "allowed_uses": dict(use_map),
            "drafting_allowed": (logical_deleted_at is None and use_map.get("DRAFTING", False)),
            "lineage": lineage,
            "logical_deleted": logical_deleted_at is not None,
        }

    @staticmethod
    def _raw_as_dict(row: RowMapping) -> dict[str, object]:
        return {
            "raw_output_id": str(row["raw_output_id"]),
            "run_id": str(row["run_id"]),
            "segment_id": str(row["segment_id"]),
            "artifact_version_id": str(row["artifact_version_id"]),
            "provider": str(row["provider"]),
            "model": str(row["model"]),
            "output_text": cast(str, row["output_text"]),
            "output_hash": cast(str, row["output_hash"]),
            "created_at": _serialize_datetime(row["created_at"], label="created_at"),
        }

    @staticmethod
    def _reject_existing_segment_truth(
        connection: Connection,
        *,
        scope: OwnershipScope,
        table: Table,
        segment_id: str,
        label: str,
    ) -> None:
        existing = connection.execute(
            select(table.c.segment_id).where(
                and_(
                    _scope_predicate(table, scope),
                    table.c.segment_id == UUID(segment_id),
                )
            )
        ).first()
        if existing is not None:
            raise TeamIntakeConflict(f"segment already has a persisted {label}")


__all__ = ["LineageKind", "TeamIntakeRepository"]
