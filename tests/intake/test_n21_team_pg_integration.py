"""Real PostgreSQL acceptance for the N2.1 intake truth tables."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, delete, func, insert, select, text, update
from sqlalchemy.engine import URL, Connection, make_url
from sqlalchemy.exc import DBAPIError

from owcopilot.contracts import OwnershipScope
from owcopilot.teamdb.schema import (
    EXPECTED_ALEMBIC_HEAD,
    MIGRATION_ROLE,
    RUNTIME_ROLE,
    allowed_uses,
    artifact_versions,
    containers,
    human_triage_decisions,
    intake_runs,
    lineage_anchors,
    raw_triage_outputs,
    rights_declarations,
    source_artifacts,
    source_segments,
    tenants,
    triage_verdicts,
)

pytest.importorskip("psycopg")

_MIGRATION_URL_ENV = "OWCOPILOT_MIGRATION_DATABASE_URL"
_APP_URL_ENV = "OWCOPILOT_TEAM_DATABASE_URL"
_NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_PDF_MIME = "application/pdf"


@dataclass(frozen=True, slots=True)
class _PgEnvironment:
    migration_url: URL
    app_url: URL
    migration_engine: Engine
    app_engine: Engine


@dataclass(frozen=True, slots=True)
class _Seed:
    scope: OwnershipScope
    actor: str


@dataclass(frozen=True, slots=True)
class _TruthIds:
    artifact_id: UUID
    artifact_version_id: UUID
    run_id: UUID
    segment_id: UUID
    raw_output_id: UUID
    verdict_id: UUID
    decision_id: UUID
    rights_declaration_id: UUID
    allowed_use_id: UUID
    lineage_anchor_id: UUID


@pytest.fixture(scope="session")
def pg_environment() -> Iterator[_PgEnvironment]:
    migration_raw = os.environ.get(_MIGRATION_URL_ENV)
    app_raw = os.environ.get(_APP_URL_ENV)
    if not migration_raw or not app_raw:
        pytest.skip(f"real PostgreSQL tests require {_MIGRATION_URL_ENV} and {_APP_URL_ENV}")

    migration_url = make_url(migration_raw)
    app_url = make_url(app_raw)
    if migration_url.drivername != "postgresql+psycopg" or app_url.drivername != (
        "postgresql+psycopg"
    ):
        pytest.fail("N2.1 integration URLs must both use postgresql+psycopg")
    if migration_url.username != MIGRATION_ROLE or app_url.username != RUNTIME_ROLE:
        pytest.fail("N2.1 integration URLs use the wrong PostgreSQL roles")

    migration_engine = create_engine(migration_url, pool_pre_ping=True)
    app_engine = create_engine(app_url, pool_pre_ping=True)
    environment = _PgEnvironment(
        migration_url=migration_url,
        app_url=app_url,
        migration_engine=migration_engine,
        app_engine=app_engine,
    )
    try:
        with migration_engine.connect() as connection:
            observed = connection.execute(
                text(
                    "SELECT current_user, current_setting('server_version_num')::integer, "
                    "(SELECT version_num FROM alembic_version)"
                )
            ).one()
        assert observed.current_user == MIGRATION_ROLE
        assert int(observed[1]) // 10_000 == 17
        assert observed.version_num == EXPECTED_ALEMBIC_HEAD
        with app_engine.connect() as connection:
            assert connection.execute(text("SELECT current_user")).scalar_one() == RUNTIME_ROLE
        yield environment
    finally:
        app_engine.dispose()
        migration_engine.dispose()


def _seed_scope(environment: _PgEnvironment) -> _Seed:
    tenant_id = uuid4()
    container_id = uuid4()
    actor_id = uuid4()
    suffix = uuid4().hex
    with environment.migration_engine.begin() as connection:
        connection.execute(
            insert(tenants).values(
                tenant_id=tenant_id,
                slug=f"n21-pg-it-tenant-{suffix}",
                display_name=f"N2.1 synthetic tenant {suffix}",
            )
        )
        connection.execute(
            insert(containers).values(
                tenant_id=tenant_id,
                container_id=container_id,
                slug=f"n21-pg-it-container-{suffix}",
                display_name=f"N2.1 synthetic container {suffix}",
            )
        )
    return _Seed(
        scope=OwnershipScope(tenant_id=str(tenant_id), container_id=str(container_id)),
        actor=str(actor_id),
    )


@contextmanager
def _app_scope(
    environment: _PgEnvironment,
    scope: OwnershipScope,
) -> Iterator[Connection]:
    with environment.app_engine.begin() as connection:
        observed = connection.execute(
            text(
                "SELECT "
                "set_config('owcopilot.tenant_id', :tenant_id, true), "
                "set_config('owcopilot.container_id', :container_id, true), "
                "set_config('TimeZone', 'UTC', true)"
            ),
            {"tenant_id": scope.tenant_id, "container_id": scope.container_id},
        ).one()
        assert tuple(str(item) for item in observed) == (
            scope.tenant_id,
            scope.container_id,
            "UTC",
        )
        yield connection


def _digest(value: bytes | str) -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return sha256(payload).hexdigest()


def _insert_truth_chain(
    environment: _PgEnvironment,
    seed: _Seed,
) -> _TruthIds:
    tenant_id = UUID(seed.scope.tenant_id)
    container_id = UUID(seed.scope.container_id)
    actor_id = UUID(seed.actor)
    artifact_id = uuid4()
    artifact_version_id = uuid4()
    run_id = uuid4()
    segment_id = uuid4()
    raw_output_id = uuid4()
    verdict_id = uuid4()
    decision_id = uuid4()
    rights_declaration_id = uuid4()
    allowed_use_id = uuid4()
    lineage_anchor_id = uuid4()
    content_bytes = b"synthetic immutable docx bytes"
    segment_text = "合成资料中的第一段"
    raw_output = '{"content_role":"REFERENCE","confidence":0.93}'

    with _app_scope(environment, seed.scope) as connection:
        connection.execute(
            insert(source_artifacts).values(
                tenant_id=tenant_id,
                container_id=container_id,
                artifact_id=artifact_id,
                display_name="synthetic-source.docx",
                created_by=actor_id,
                created_at=_NOW,
            )
        )
        connection.execute(
            insert(artifact_versions).values(
                tenant_id=tenant_id,
                container_id=container_id,
                artifact_version_id=artifact_version_id,
                artifact_id=artifact_id,
                content_hash=_digest(content_bytes),
                mime_type=_DOCX_MIME,
                extension=".docx",
                byte_size=len(content_bytes),
                content_bytes=content_bytes,
                created_by=actor_id,
                created_at=_NOW,
            )
        )
        connection.execute(
            insert(intake_runs).values(
                tenant_id=tenant_id,
                container_id=container_id,
                run_id=run_id,
                artifact_version_id=artifact_version_id,
                original_filename="synthetic-source.docx",
                status="UPLOADED",
                created_by=actor_id,
                started_at=_NOW,
            )
        )
        connection.execute(
            insert(source_segments).values(
                tenant_id=tenant_id,
                container_id=container_id,
                segment_id=segment_id,
                artifact_version_id=artifact_version_id,
                ordinal=0,
                locator_kind="docx_paragraph",
                locator_jsonb={"kind": "docx_paragraph", "paragraph_index": 0},
                text_content=segment_text,
                text_hash=_digest(segment_text),
                untrusted_input=True,
                created_at=_NOW,
            )
        )
        connection.execute(
            update(intake_runs).where(intake_runs.c.run_id == run_id).values(status="SEGMENTED")
        )
        connection.execute(
            insert(raw_triage_outputs).values(
                tenant_id=tenant_id,
                container_id=container_id,
                raw_output_id=raw_output_id,
                run_id=run_id,
                segment_id=segment_id,
                artifact_version_id=artifact_version_id,
                provider="synthetic",
                model="deterministic-test-double",
                output_text=raw_output,
                output_hash=_digest(raw_output),
                created_at=_NOW,
            )
        )
        connection.execute(
            insert(triage_verdicts).values(
                tenant_id=tenant_id,
                container_id=container_id,
                verdict_id=verdict_id,
                raw_output_id=raw_output_id,
                segment_id=segment_id,
                artifact_version_id=artifact_version_id,
                content_role="REFERENCE",
                confidence=0.93,
                policy_threshold=0.8,
                evidence_spans_jsonb=[{"start": 0, "end": 2, "quote": "合成"}],
                abstain=False,
                derivation_policy_version="n21-test-v1",
                created_at=_NOW,
            )
        )
        connection.execute(
            update(intake_runs)
            .where(intake_runs.c.run_id == run_id)
            .values(status="AWAITING_HUMAN", finished_at=_NOW)
        )
        connection.execute(
            insert(rights_declarations).values(
                tenant_id=tenant_id,
                container_id=container_id,
                rights_declaration_id=rights_declaration_id,
                artifact_version_id=artifact_version_id,
                rights_provenance="OWNED",
                declaration_text="人工声明：仅用于验收。",
                declared_by=actor_id,
                human_asserted=True,
                declared_at=_NOW,
            )
        )
        connection.execute(
            insert(allowed_uses),
            [
                {
                    "tenant_id": tenant_id,
                    "container_id": container_id,
                    "allowed_use_id": allowed_use_id if kind == "DRAFTING" else uuid4(),
                    "rights_declaration_id": rights_declaration_id,
                    "use_kind": kind,
                    "allowed": False,
                    "declared_by": actor_id,
                    "declared_at": _NOW,
                }
                for kind in ("ANALYSIS", "RETRIEVAL", "DRAFTING", "TRAINING", "PUBLICATION")
            ],
        )
        connection.execute(
            insert(human_triage_decisions).values(
                tenant_id=tenant_id,
                container_id=container_id,
                decision_id=decision_id,
                run_id=run_id,
                verdict_id=verdict_id,
                segment_id=segment_id,
                artifact_version_id=artifact_version_id,
                decision="CONFIRM",
                content_role="REFERENCE",
                decided_by=actor_id,
                rationale="人工复核合成验收资料",
                decided_at=_NOW,
            )
        )
        connection.execute(
            insert(lineage_anchors).values(
                tenant_id=tenant_id,
                container_id=container_id,
                lineage_anchor_id=lineage_anchor_id,
                segment_id=segment_id,
                artifact_version_id=artifact_version_id,
                lineage_kind="INSPIRATION_LINEAGE",
                target_ref="acceptance:synthetic-source",
                created_by=actor_id,
                created_at=_NOW,
            )
        )

    return _TruthIds(
        artifact_id=artifact_id,
        artifact_version_id=artifact_version_id,
        run_id=run_id,
        segment_id=segment_id,
        raw_output_id=raw_output_id,
        verdict_id=verdict_id,
        decision_id=decision_id,
        rights_declaration_id=rights_declaration_id,
        allowed_use_id=allowed_use_id,
        lineage_anchor_id=lineage_anchor_id,
    )


def test_n21_intake_tables_same_scope_round_trip(
    pg_environment: _PgEnvironment,
) -> None:
    seed = _seed_scope(pg_environment)
    ids = _insert_truth_chain(pg_environment, seed)

    with _app_scope(pg_environment, seed.scope) as connection:
        version = connection.execute(
            select(
                artifact_versions.c.content_hash,
                artifact_versions.c.byte_size,
                artifact_versions.c.content_bytes,
            ).where(artifact_versions.c.artifact_version_id == ids.artifact_version_id)
        ).one()
        segment = connection.execute(
            select(
                source_segments.c.locator_kind,
                source_segments.c.locator_jsonb,
                source_segments.c.text_hash,
            ).where(source_segments.c.segment_id == ids.segment_id)
        ).one()
        raw_count = connection.execute(
            select(
                raw_triage_outputs.c.raw_output_id,
                raw_triage_outputs.c.output_text,
                raw_triage_outputs.c.output_hash,
            ).where(raw_triage_outputs.c.run_id == ids.run_id)
        ).all()
        verdict = connection.execute(
            select(
                triage_verdicts.c.raw_output_id,
                triage_verdicts.c.content_role,
                triage_verdicts.c.confidence,
                triage_verdicts.c.policy_threshold,
                triage_verdicts.c.evidence_spans_jsonb,
                triage_verdicts.c.abstain,
            ).where(triage_verdicts.c.verdict_id == ids.verdict_id)
        ).one()
        decision_count = connection.execute(
            select(human_triage_decisions.c.decision_id).where(
                human_triage_decisions.c.run_id == ids.run_id
            )
        ).all()
        drafting_allowed = connection.execute(
            select(allowed_uses.c.allowed).where(
                allowed_uses.c.rights_declaration_id == ids.rights_declaration_id,
                allowed_uses.c.use_kind == "DRAFTING",
            )
        ).scalar_one()
        lineage_kind = connection.execute(
            select(lineage_anchors.c.lineage_kind).where(
                lineage_anchors.c.segment_id == ids.segment_id
            )
        ).scalar_one()

    assert version.content_hash == _digest(version.content_bytes)
    assert version.byte_size == len(version.content_bytes)
    assert segment.locator_kind == "docx_paragraph"
    assert segment.locator_jsonb == {"kind": "docx_paragraph", "paragraph_index": 0}
    assert len(raw_count) == 1
    assert raw_count[0].output_hash == _digest(raw_count[0].output_text)
    assert verdict.raw_output_id == ids.raw_output_id
    assert verdict.content_role == "REFERENCE"
    assert verdict.confidence >= verdict.policy_threshold
    assert verdict.evidence_spans_jsonb == [{"start": 0, "end": 2, "quote": "合成"}]
    assert verdict.abstain is False
    assert len(decision_count) == 1
    assert drafting_allowed is False
    assert lineage_kind == "INSPIRATION_LINEAGE"


def _insert_uploaded_run(
    environment: _PgEnvironment,
    seed: _Seed,
    *,
    label: str,
) -> tuple[UUID, UUID, UUID]:
    tenant_id = UUID(seed.scope.tenant_id)
    container_id = UUID(seed.scope.container_id)
    actor_id = UUID(seed.actor)
    artifact_id = uuid4()
    artifact_version_id = uuid4()
    run_id = uuid4()
    content_bytes = f"synthetic {label} bytes".encode()
    with _app_scope(environment, seed.scope) as connection:
        connection.execute(
            insert(source_artifacts).values(
                tenant_id=tenant_id,
                container_id=container_id,
                artifact_id=artifact_id,
                display_name=f"{label}.docx",
                created_by=actor_id,
                created_at=_NOW,
            )
        )
        connection.execute(
            insert(artifact_versions).values(
                tenant_id=tenant_id,
                container_id=container_id,
                artifact_version_id=artifact_version_id,
                artifact_id=artifact_id,
                content_hash=_digest(content_bytes),
                mime_type=_DOCX_MIME,
                extension=".docx",
                byte_size=len(content_bytes),
                content_bytes=content_bytes,
                created_by=actor_id,
                created_at=_NOW,
            )
        )
        connection.execute(
            insert(intake_runs).values(
                tenant_id=tenant_id,
                container_id=container_id,
                run_id=run_id,
                artifact_version_id=artifact_version_id,
                original_filename=f"{label}.docx",
                status="UPLOADED",
                created_by=actor_id,
                started_at=_NOW,
            )
        )
    return artifact_id, artifact_version_id, run_id


def _insert_uploaded_pdf_run(
    environment: _PgEnvironment,
    seed: _Seed,
    *,
    label: str,
) -> tuple[UUID, UUID, UUID, bytes]:
    artifact_id = uuid4()
    artifact_version_id = uuid4()
    run_id = uuid4()
    content_bytes = f"%PDF-1.4\nsynthetic {label} bytes".encode()
    with _app_scope(environment, seed.scope) as connection:
        connection.execute(
            insert(source_artifacts).values(
                tenant_id=UUID(seed.scope.tenant_id),
                container_id=UUID(seed.scope.container_id),
                artifact_id=artifact_id,
                display_name=f"{label}.pdf",
                created_by=UUID(seed.actor),
                created_at=_NOW,
            )
        )
        connection.execute(
            insert(artifact_versions).values(
                tenant_id=UUID(seed.scope.tenant_id),
                container_id=UUID(seed.scope.container_id),
                artifact_version_id=artifact_version_id,
                artifact_id=artifact_id,
                content_hash=_digest(content_bytes),
                mime_type=_PDF_MIME,
                extension=".pdf",
                byte_size=len(content_bytes),
                content_bytes=content_bytes,
                created_by=UUID(seed.actor),
                created_at=_NOW,
            )
        )
        connection.execute(
            insert(intake_runs).values(
                tenant_id=UUID(seed.scope.tenant_id),
                container_id=UUID(seed.scope.container_id),
                run_id=run_id,
                artifact_version_id=artifact_version_id,
                original_filename=f"{label}.pdf",
                status="UPLOADED",
                created_by=UUID(seed.actor),
                started_at=_NOW,
            )
        )
    return artifact_id, artifact_version_id, run_id, content_bytes


def test_n21_source_artifact_logical_delete_state_machine(
    pg_environment: _PgEnvironment,
) -> None:
    seed = _seed_scope(pg_environment)
    ids = _insert_truth_chain(pg_environment, seed)

    with _app_scope(pg_environment, seed.scope) as connection:
        connection.execute(
            update(source_artifacts)
            .where(source_artifacts.c.artifact_id == ids.artifact_id)
            .values(logical_deleted_at=_NOW)
        )
        retained = connection.execute(
            select(artifact_versions.c.content_bytes, source_segments.c.locator_jsonb)
            .select_from(
                artifact_versions.join(
                    source_segments,
                    (artifact_versions.c.tenant_id == source_segments.c.tenant_id)
                    & (artifact_versions.c.container_id == source_segments.c.container_id)
                    & (
                        artifact_versions.c.artifact_version_id
                        == source_segments.c.artifact_version_id
                    ),
                )
            )
            .where(artifact_versions.c.artifact_version_id == ids.artifact_version_id)
        ).one()
    assert retained.content_bytes
    assert retained.locator_jsonb["kind"] == "docx_paragraph"

    for values, message in (
        ({"logical_deleted_at": None}, "logical delete is one-way"),
        ({"logical_deleted_at": _NOW}, "logical delete is one-way"),
        ({"display_name": "mutated.docx"}, "identity and audit fields are frozen"),
    ):
        with pytest.raises(DBAPIError, match=message):
            with _app_scope(pg_environment, seed.scope) as connection:
                connection.execute(
                    update(source_artifacts)
                    .where(source_artifacts.c.artifact_id == ids.artifact_id)
                    .values(**values)
                )

    with pytest.raises(DBAPIError, match="permits logical delete only"):
        with pg_environment.migration_engine.begin() as connection:
            connection.execute(
                delete(source_artifacts).where(
                    source_artifacts.c.tenant_id == UUID(seed.scope.tenant_id),
                    source_artifacts.c.container_id == UUID(seed.scope.container_id),
                    source_artifacts.c.artifact_id == ids.artifact_id,
                )
            )

    with pytest.raises(DBAPIError, match="must start active"):
        with _app_scope(pg_environment, seed.scope) as connection:
            connection.execute(
                insert(source_artifacts).values(
                    tenant_id=UUID(seed.scope.tenant_id),
                    container_id=UUID(seed.scope.container_id),
                    artifact_id=uuid4(),
                    display_name="pre-deleted.docx",
                    created_by=UUID(seed.actor),
                    created_at=_NOW,
                    logical_deleted_at=_NOW,
                )
            )


def test_n21_intake_run_state_machine(pg_environment: _PgEnvironment) -> None:
    seed = _seed_scope(pg_environment)
    _, version_id, run_id = _insert_uploaded_run(pg_environment, seed, label=f"state-{uuid4().hex}")

    with pytest.raises(DBAPIError, match="invalid intake_runs state transition"):
        with _app_scope(pg_environment, seed.scope) as connection:
            connection.execute(
                update(intake_runs)
                .where(intake_runs.c.run_id == run_id)
                .values(status="AWAITING_HUMAN", finished_at=_NOW)
            )

    with _app_scope(pg_environment, seed.scope) as connection:
        connection.execute(
            update(intake_runs).where(intake_runs.c.run_id == run_id).values(status="SEGMENTED")
        )
        connection.execute(
            update(intake_runs)
            .where(intake_runs.c.run_id == run_id)
            .values(status="AWAITING_HUMAN", finished_at=_NOW)
        )

    with pytest.raises(DBAPIError, match="invalid intake_runs state transition"):
        with _app_scope(pg_environment, seed.scope) as connection:
            connection.execute(
                update(intake_runs)
                .where(intake_runs.c.run_id == run_id)
                .values(status="REJECTED", rejection_code="TOO_LATE")
            )

    for from_segmented in (False, True):
        _, _, rejected_run = _insert_uploaded_run(
            pg_environment,
            seed,
            label=f"reject-{from_segmented}-{uuid4().hex}",
        )
        with _app_scope(pg_environment, seed.scope) as connection:
            if from_segmented:
                connection.execute(
                    update(intake_runs)
                    .where(intake_runs.c.run_id == rejected_run)
                    .values(status="SEGMENTED")
                )
            connection.execute(
                update(intake_runs)
                .where(intake_runs.c.run_id == rejected_run)
                .values(status="REJECTED", finished_at=_NOW, rejection_code="SYNTHETIC")
            )

    with pytest.raises(DBAPIError, match="identity and audit fields are frozen"):
        with _app_scope(pg_environment, seed.scope) as connection:
            connection.execute(
                update(intake_runs)
                .where(intake_runs.c.run_id == run_id)
                .values(artifact_version_id=version_id, original_filename="changed.docx")
            )

    with pytest.raises(DBAPIError, match="cannot be deleted"):
        with pg_environment.migration_engine.begin() as connection:
            connection.execute(
                delete(intake_runs).where(
                    intake_runs.c.tenant_id == UUID(seed.scope.tenant_id),
                    intake_runs.c.container_id == UUID(seed.scope.container_id),
                    intake_runs.c.run_id == run_id,
                )
            )

    artifact_id = uuid4()
    content_bytes = b"invalid initial state"
    version = uuid4()
    with _app_scope(pg_environment, seed.scope) as connection:
        connection.execute(
            insert(source_artifacts).values(
                tenant_id=UUID(seed.scope.tenant_id),
                container_id=UUID(seed.scope.container_id),
                artifact_id=artifact_id,
                display_name="invalid-initial.docx",
                created_by=UUID(seed.actor),
                created_at=_NOW,
            )
        )
        connection.execute(
            insert(artifact_versions).values(
                tenant_id=UUID(seed.scope.tenant_id),
                container_id=UUID(seed.scope.container_id),
                artifact_version_id=version,
                artifact_id=artifact_id,
                content_hash=_digest(content_bytes),
                mime_type=_DOCX_MIME,
                extension=".docx",
                byte_size=len(content_bytes),
                content_bytes=content_bytes,
                created_by=UUID(seed.actor),
                created_at=_NOW,
            )
        )
    with pytest.raises(DBAPIError, match="must start in UPLOADED"):
        with _app_scope(pg_environment, seed.scope) as connection:
            connection.execute(
                insert(intake_runs).values(
                    tenant_id=UUID(seed.scope.tenant_id),
                    container_id=UUID(seed.scope.container_id),
                    run_id=uuid4(),
                    artifact_version_id=version,
                    original_filename="invalid-initial.docx",
                    status="SEGMENTED",
                    created_by=UUID(seed.actor),
                    started_at=_NOW,
                )
            )


def test_n21_locator_constraints_fail_closed(pg_environment: _PgEnvironment) -> None:
    seed = _seed_scope(pg_environment)
    ids = _insert_truth_chain(pg_environment, seed)
    invalid_locators = (
        ("docx_paragraph", {"kind": "docx_paragraph", "paragraph_index": 1, "extra": 0}),
        ("docx_paragraph", {"kind": "docx_paragraph"}),
        ("docx_paragraph", {"kind": "docx_paragraph", "paragraph_index": -1}),
        ("docx_table_cell", {"kind": "docx_table_cell", "table_index": 0, "row_index": 0}),
        (
            "xlsx_cell",
            {
                "kind": "xlsx_cell",
                "sheet_name": "Sheet1",
                "row_index": 1,
                "column_index": 1,
                "cell_reference": "B1",
            },
        ),
        (
            "xlsx_cell",
            {
                "kind": "xlsx_cell",
                "sheet_name": "Sheet1",
                "row_index": 1048577,
                "column_index": 1,
                "cell_reference": "A1048577",
            },
        ),
        (
            "pdf_text_line",
            {
                "kind": "pdf_text_line",
                "page_index": 0,
                "line_index": 0,
                "x0": 72.0,
                "top": 72.0,
                "x1": 700.0,
                "bottom": 84.0,
                "page_width": 612.0,
                "page_height": 792.0,
                "coordinate_space": "pdf_points_top_left_display",
                "extraction_mode": "born_digital_text_line_baseline",
                "validation_backend": "pypdfium2",
                "validation_backend_version": "5.12.1",
                "extraction_backend": "pdfplumber",
                "extraction_backend_version": "0.11.10",
            },
        ),
    )
    for ordinal, (kind, locator) in enumerate(invalid_locators, start=1):
        text_value = f"invalid locator {ordinal}"
        with pytest.raises(DBAPIError):
            with _app_scope(pg_environment, seed.scope) as connection:
                connection.execute(
                    insert(source_segments).values(
                        tenant_id=UUID(seed.scope.tenant_id),
                        container_id=UUID(seed.scope.container_id),
                        segment_id=uuid4(),
                        artifact_version_id=ids.artifact_version_id,
                        ordinal=ordinal,
                        locator_kind=kind,
                        locator_jsonb=locator,
                        text_content=text_value,
                        text_hash=_digest(text_value),
                        untrusted_input=True,
                        created_at=_NOW,
                    )
                )

    mismatched_bytes = b"bytes with a forged digest"
    with pytest.raises(DBAPIError):
        with _app_scope(pg_environment, seed.scope) as connection:
            connection.execute(
                insert(artifact_versions).values(
                    tenant_id=UUID(seed.scope.tenant_id),
                    container_id=UUID(seed.scope.container_id),
                    artifact_version_id=uuid4(),
                    artifact_id=ids.artifact_id,
                    content_hash="0" * 64,
                    mime_type=_DOCX_MIME,
                    extension=".docx",
                    byte_size=len(mismatched_bytes),
                    content_bytes=mismatched_bytes,
                    created_by=UUID(seed.actor),
                    created_at=_NOW,
                )
            )

    mismatched_text = "forged segment digest"
    with pytest.raises(DBAPIError):
        with _app_scope(pg_environment, seed.scope) as connection:
            connection.execute(
                insert(source_segments).values(
                    tenant_id=UUID(seed.scope.tenant_id),
                    container_id=UUID(seed.scope.container_id),
                    segment_id=uuid4(),
                    artifact_version_id=ids.artifact_version_id,
                    ordinal=100,
                    locator_kind="docx_paragraph",
                    locator_jsonb={"kind": "docx_paragraph", "paragraph_index": 100},
                    text_content=mismatched_text,
                    text_hash="0" * 64,
                    untrusted_input=True,
                    created_at=_NOW,
                )
            )


@pytest.mark.parametrize(
    ("status", "processing_code"),
    [
        ("OCR_REQUIRED", "PDF_OCR_REQUIRED"),
        ("PROCESSING_FAILED", "CORRUPT_PDF"),
        ("PROCESSING_FAILED", "PDF_ENCRYPTED"),
        ("PROCESSING_FAILED", "PDF_LAYOUT_BACKEND_UNAVAILABLE"),
        ("PROCESSING_FAILED", "PDF_MAGIC_MISMATCH"),
        ("PROCESSING_FAILED", "PDF_PAGE_DIMENSION_EXCEEDED"),
        ("PROCESSING_FAILED", "PDF_PAGE_LIMIT_EXCEEDED"),
        ("PROCESSING_FAILED", "PDF_PROCESSING_FAILED"),
        ("PROCESSING_FAILED", "PDF_TEXT_LIMIT_EXCEEDED"),
        ("PROCESSING_FAILED", "PDF_VALIDATION_BACKEND_UNAVAILABLE"),
        ("PROCESSING_FAILED", "UNLOCATABLE_PDF_TEXT"),
    ],
)
def test_n21_pdf_handoff_states_retain_raw_bytes_without_fake_segments(
    pg_environment: _PgEnvironment,
    status: str,
    processing_code: str,
) -> None:
    seed = _seed_scope(pg_environment)
    _artifact_id, version_id, run_id, content_bytes = _insert_uploaded_pdf_run(
        pg_environment,
        seed,
        label=f"handoff-{status.lower()}-{uuid4().hex}",
    )
    with _app_scope(pg_environment, seed.scope) as connection:
        connection.execute(
            update(intake_runs)
            .where(intake_runs.c.run_id == run_id)
            .values(status=status, processing_code=processing_code)
        )
        stored = connection.execute(
            select(
                intake_runs.c.status,
                intake_runs.c.processing_code,
                intake_runs.c.finished_at,
                artifact_versions.c.content_bytes,
            )
            .select_from(
                intake_runs.join(
                    artifact_versions,
                    (intake_runs.c.tenant_id == artifact_versions.c.tenant_id)
                    & (intake_runs.c.container_id == artifact_versions.c.container_id)
                    & (
                        intake_runs.c.artifact_version_id
                        == artifact_versions.c.artifact_version_id
                    ),
                )
            )
            .where(
                intake_runs.c.run_id == run_id,
                intake_runs.c.artifact_version_id == version_id,
            )
        ).one()
        segment_count = connection.execute(
            select(func.count()).select_from(source_segments).where(
                source_segments.c.artifact_version_id == version_id
            )
        ).scalar_one()
    assert stored.status == status
    assert stored.processing_code == processing_code
    assert stored.finished_at is None
    assert bytes(stored.content_bytes) == content_bytes
    assert segment_count == 0


def test_n21_processing_failed_pdf_can_resume_into_real_segments(
    pg_environment: _PgEnvironment,
) -> None:
    seed = _seed_scope(pg_environment)
    _artifact_id, version_id, run_id, _content_bytes = _insert_uploaded_pdf_run(
        pg_environment,
        seed,
        label=f"retry-{uuid4().hex}",
    )
    locator = {
        "kind": "pdf_text_line",
        "page_index": 0,
        "line_index": 0,
        "x0": 72.0,
        "top": 62.484,
        "x1": 130.0,
        "bottom": 74.484,
        "page_width": 612.0,
        "page_height": 792.0,
        "coordinate_space": "pdf_points_top_left_display",
        "extraction_mode": "born_digital_text_line_baseline",
        "validation_backend": "pypdfium2",
        "validation_backend_version": "5.12.1",
        "extraction_backend": "pdfplumber",
        "extraction_backend_version": "0.11.10",
    }
    with _app_scope(pg_environment, seed.scope) as connection:
        connection.execute(
            update(intake_runs)
            .where(intake_runs.c.run_id == run_id)
            .values(
                status="PROCESSING_FAILED",
                processing_code="PDF_LAYOUT_BACKEND_UNAVAILABLE",
            )
        )
        segment_id = uuid4()
        connection.execute(
            insert(source_segments).values(
                tenant_id=UUID(seed.scope.tenant_id),
                container_id=UUID(seed.scope.container_id),
                segment_id=segment_id,
                artifact_version_id=version_id,
                ordinal=0,
                locator_kind="pdf_text_line",
                locator_jsonb=locator,
                text_content="Recovered text",
                text_hash=_digest("Recovered text"),
                untrusted_input=True,
                created_at=_NOW,
            )
        )
        connection.execute(
            update(intake_runs)
            .where(intake_runs.c.run_id == run_id)
            .values(status="SEGMENTED", processing_code=None)
        )
        stored = connection.execute(
            select(intake_runs.c.status, intake_runs.c.processing_code).where(
                intake_runs.c.run_id == run_id
            )
        ).one()
    assert stored.status == "SEGMENTED"
    assert stored.processing_code is None


def test_n21_pdf_format_and_exact_locator_shape_are_database_enforced(
    pg_environment: _PgEnvironment,
) -> None:
    seed = _seed_scope(pg_environment)
    artifact_id = uuid4()
    artifact_version_id = uuid4()
    run_id = uuid4()
    segment_id = uuid4()
    content_bytes = b"%PDF-1.4\nsynthetic database fixture"
    locator = {
        "kind": "pdf_text_line",
        "page_index": 0,
        "line_index": 0,
        "x0": 72.0,
        "top": 62.484,
        "x1": 130.0,
        "bottom": 74.484,
        "page_width": 612.0,
        "page_height": 792.0,
        "coordinate_space": "pdf_points_top_left_display",
        "extraction_mode": "born_digital_text_line_baseline",
        "validation_backend": "pypdfium2",
        "validation_backend_version": "5.12.1",
        "extraction_backend": "pdfplumber",
        "extraction_backend_version": "0.11.10",
    }
    with _app_scope(pg_environment, seed.scope) as connection:
        connection.execute(
            insert(source_artifacts).values(
                tenant_id=UUID(seed.scope.tenant_id),
                container_id=UUID(seed.scope.container_id),
                artifact_id=artifact_id,
                display_name="source.pdf",
                created_by=UUID(seed.actor),
                created_at=_NOW,
            )
        )
        connection.execute(
            insert(artifact_versions).values(
                tenant_id=UUID(seed.scope.tenant_id),
                container_id=UUID(seed.scope.container_id),
                artifact_version_id=artifact_version_id,
                artifact_id=artifact_id,
                content_hash=_digest(content_bytes),
                mime_type=_PDF_MIME,
                extension=".pdf",
                byte_size=len(content_bytes),
                content_bytes=content_bytes,
                created_by=UUID(seed.actor),
                created_at=_NOW,
            )
        )
        connection.execute(
            insert(intake_runs).values(
                tenant_id=UUID(seed.scope.tenant_id),
                container_id=UUID(seed.scope.container_id),
                run_id=run_id,
                artifact_version_id=artifact_version_id,
                original_filename="source.pdf",
                status="UPLOADED",
                created_by=UUID(seed.actor),
                started_at=_NOW,
            )
        )
        connection.execute(
            insert(source_segments).values(
                tenant_id=UUID(seed.scope.tenant_id),
                container_id=UUID(seed.scope.container_id),
                segment_id=segment_id,
                artifact_version_id=artifact_version_id,
                ordinal=0,
                locator_kind="pdf_text_line",
                locator_jsonb=locator,
                text_content="Quest condition",
                text_hash=_digest("Quest condition"),
                untrusted_input=True,
                created_at=_NOW,
            )
        )
        stored = connection.execute(
            select(source_segments.c.locator_jsonb).where(
                source_segments.c.segment_id == segment_id
            )
        ).scalar_one()
    assert stored == locator

    _, docx_version_id, _ = _insert_uploaded_run(
        pg_environment,
        seed,
        label=f"pdf-locator-on-docx-{uuid4().hex}",
    )
    with pytest.raises(DBAPIError, match="locator kind does not match artifact format"):
        with _app_scope(pg_environment, seed.scope) as connection:
            connection.execute(
                insert(source_segments).values(
                    tenant_id=UUID(seed.scope.tenant_id),
                    container_id=UUID(seed.scope.container_id),
                    segment_id=uuid4(),
                    artifact_version_id=docx_version_id,
                    ordinal=0,
                    locator_kind="pdf_text_line",
                    locator_jsonb=locator,
                    text_content="wrong format",
                    text_hash=_digest("wrong format"),
                    untrusted_input=True,
                    created_at=_NOW,
                )
            )


def test_n21_all_immutable_tables_reject_update_and_delete(
    pg_environment: _PgEnvironment,
) -> None:
    seed = _seed_scope(pg_environment)
    ids = _insert_truth_chain(pg_environment, seed)
    targets = {
        "artifact_versions": ("artifact_version_id", ids.artifact_version_id),
        "source_segments": ("segment_id", ids.segment_id),
        "raw_triage_outputs": ("raw_output_id", ids.raw_output_id),
        "triage_verdicts": ("verdict_id", ids.verdict_id),
        "human_triage_decisions": ("decision_id", ids.decision_id),
        "rights_declarations": ("rights_declaration_id", ids.rights_declaration_id),
        "allowed_uses": ("allowed_use_id", ids.allowed_use_id),
        "lineage_anchors": ("lineage_anchor_id", ids.lineage_anchor_id),
    }
    for table_name, (key_name, key_value) in targets.items():
        parameters = {
            "tenant_id": seed.scope.tenant_id,
            "container_id": seed.scope.container_id,
            "key_value": str(key_value),
        }
        with pytest.raises(DBAPIError, match=rf"immutable table {table_name} rejects UPDATE"):
            with pg_environment.migration_engine.begin() as connection:
                connection.execute(
                    text(
                        f"UPDATE {table_name} SET tenant_id = tenant_id "
                        f"WHERE tenant_id = :tenant_id AND container_id = :container_id "
                        f"AND {key_name} = :key_value"
                    ),
                    parameters,
                )
        with pytest.raises(DBAPIError, match=rf"immutable table {table_name} rejects DELETE"):
            with pg_environment.migration_engine.begin() as connection:
                connection.execute(
                    text(
                        f"DELETE FROM {table_name} "
                        f"WHERE tenant_id = :tenant_id AND container_id = :container_id "
                        f"AND {key_name} = :key_value"
                    ),
                    parameters,
                )


def test_n21_raw_and_derived_triage_are_version_bound(
    pg_environment: _PgEnvironment,
) -> None:
    seed = _seed_scope(pg_environment)
    first = _insert_truth_chain(pg_environment, seed)
    second = _insert_truth_chain(pg_environment, seed)

    mismatched_raw = '{"content_role":"REFERENCE"}'
    with pytest.raises(DBAPIError):
        with _app_scope(pg_environment, seed.scope) as connection:
            connection.execute(
                insert(raw_triage_outputs).values(
                    tenant_id=UUID(seed.scope.tenant_id),
                    container_id=UUID(seed.scope.container_id),
                    raw_output_id=uuid4(),
                    run_id=first.run_id,
                    segment_id=second.segment_id,
                    artifact_version_id=first.artifact_version_id,
                    provider="synthetic",
                    model="version-mismatch",
                    output_text=mismatched_raw,
                    output_hash=_digest(mismatched_raw),
                    created_at=_NOW,
                )
            )

    with pytest.raises(DBAPIError):
        with _app_scope(pg_environment, seed.scope) as connection:
            connection.execute(
                insert(human_triage_decisions).values(
                    tenant_id=UUID(seed.scope.tenant_id),
                    container_id=UUID(seed.scope.container_id),
                    decision_id=uuid4(),
                    run_id=first.run_id,
                    verdict_id=second.verdict_id,
                    segment_id=second.segment_id,
                    artifact_version_id=second.artifact_version_id,
                    decision="CONFIRM",
                    content_role="REFERENCE",
                    decided_by=UUID(seed.actor),
                    rationale="跨版本伪造",
                    decided_at=_NOW,
                )
            )

    with pytest.raises(DBAPIError):
        with _app_scope(pg_environment, seed.scope) as connection:
            connection.execute(
                insert(human_triage_decisions).values(
                    tenant_id=UUID(seed.scope.tenant_id),
                    container_id=UUID(seed.scope.container_id),
                    decision_id=uuid4(),
                    run_id=first.run_id,
                    verdict_id=first.verdict_id,
                    segment_id=first.segment_id,
                    artifact_version_id=first.artifact_version_id,
                    decision="OVERRIDE",
                    content_role="UNKNOWN",
                    decided_by=UUID(seed.actor),
                    rationale="同一 run/segment 不允许第二份冲突裁决",
                    decided_at=_NOW,
                )
            )

    with pytest.raises(DBAPIError):
        with _app_scope(pg_environment, seed.scope) as connection:
            connection.execute(
                insert(raw_triage_outputs).values(
                    tenant_id=UUID(seed.scope.tenant_id),
                    container_id=UUID(seed.scope.container_id),
                    raw_output_id=uuid4(),
                    run_id=first.run_id,
                    segment_id=first.segment_id,
                    artifact_version_id=first.artifact_version_id,
                    provider="synthetic",
                    model="bad-hash",
                    output_text="",
                    output_hash="0" * 64,
                    created_at=_NOW,
                )
            )

    for index, raw_text in enumerate(("", "   ", "not-json", "[]")):
        raw_output_id = uuid4()
        with _app_scope(pg_environment, seed.scope) as connection:
            connection.execute(
                insert(raw_triage_outputs).values(
                    tenant_id=UUID(seed.scope.tenant_id),
                    container_id=UUID(seed.scope.container_id),
                    raw_output_id=raw_output_id,
                    run_id=first.run_id,
                    segment_id=first.segment_id,
                    artifact_version_id=first.artifact_version_id,
                    provider="synthetic",
                    model="invalid-output-preservation",
                    output_text=raw_text,
                    output_hash=_digest(raw_text),
                    created_at=_NOW,
                )
            )

        common = {
            "tenant_id": UUID(seed.scope.tenant_id),
            "container_id": UUID(seed.scope.container_id),
            "raw_output_id": raw_output_id,
            "segment_id": first.segment_id,
            "artifact_version_id": first.artifact_version_id,
            "confidence": 0.9,
            "policy_threshold": 0.8,
            "derivation_policy_version": f"invalid-raw-{index}",
            "created_at": _NOW,
        }
        with pytest.raises(DBAPIError, match="requires UNKNOWN abstention"):
            with _app_scope(pg_environment, seed.scope) as connection:
                connection.execute(
                    insert(triage_verdicts).values(
                        verdict_id=uuid4(),
                        content_role="REFERENCE",
                        evidence_spans_jsonb=[{"start": 0, "end": 1, "quote": "x"}],
                        abstain=False,
                        **common,
                    )
                )
        with _app_scope(pg_environment, seed.scope) as connection:
            connection.execute(
                insert(triage_verdicts).values(
                    verdict_id=uuid4(),
                    content_role="UNKNOWN",
                    confidence=0.0,
                    policy_threshold=0.8,
                    evidence_spans_jsonb=[],
                    abstain=True,
                    abstain_reason="RAW_OUTPUT_INVALID",
                    tenant_id=UUID(seed.scope.tenant_id),
                    container_id=UUID(seed.scope.container_id),
                    raw_output_id=raw_output_id,
                    segment_id=first.segment_id,
                    artifact_version_id=first.artifact_version_id,
                    derivation_policy_version=f"invalid-raw-{index}",
                    created_at=_NOW,
                )
            )


def test_n21_cross_scope_and_human_rights_guards(
    pg_environment: _PgEnvironment,
) -> None:
    first = _seed_scope(pg_environment)
    second = _seed_scope(pg_environment)
    with pytest.raises(DBAPIError):
        with _app_scope(pg_environment, first.scope) as connection:
            connection.execute(
                insert(source_artifacts).values(
                    tenant_id=UUID(second.scope.tenant_id),
                    container_id=UUID(second.scope.container_id),
                    artifact_id=uuid4(),
                    display_name="cross-scope.docx",
                    created_by=UUID(first.actor),
                    created_at=_NOW,
                )
            )

    _, version_id, _ = _insert_uploaded_run(pg_environment, first, label=f"rights-{uuid4().hex}")
    with pytest.raises(DBAPIError):
        with _app_scope(pg_environment, first.scope) as connection:
            connection.execute(
                insert(rights_declarations).values(
                    tenant_id=UUID(first.scope.tenant_id),
                    container_id=UUID(first.scope.container_id),
                    rights_declaration_id=uuid4(),
                    artifact_version_id=version_id,
                    rights_provenance="OWNED",
                    declaration_text="伪造的非人工声明",
                    declared_by=UUID(first.actor),
                    human_asserted=False,
                    declared_at=_NOW,
                )
            )

    with _app_scope(pg_environment, first.scope) as connection:
        connection.execute(
            insert(rights_declarations).values(
                tenant_id=UUID(first.scope.tenant_id),
                container_id=UUID(first.scope.container_id),
                rights_declaration_id=uuid4(),
                artifact_version_id=version_id,
                rights_provenance="OWNED",
                declaration_text="人工有效声明",
                declared_by=UUID(first.actor),
                human_asserted=True,
                declared_at=_NOW,
            )
        )
    with pytest.raises(DBAPIError):
        with _app_scope(pg_environment, first.scope) as connection:
            connection.execute(
                insert(rights_declarations).values(
                    tenant_id=UUID(first.scope.tenant_id),
                    container_id=UUID(first.scope.container_id),
                    rights_declaration_id=uuid4(),
                    artifact_version_id=version_id,
                    rights_provenance="LICENSED",
                    declaration_text="同版本第二份声明",
                    declared_by=UUID(first.actor),
                    human_asserted=True,
                    declared_at=_NOW,
                )
            )
