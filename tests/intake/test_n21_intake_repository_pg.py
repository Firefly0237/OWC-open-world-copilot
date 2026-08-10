"""Real PostgreSQL service-to-repository acceptance for N2.1 intake."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from io import BytesIO
from uuid import UUID, uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from sqlalchemy import Engine, create_engine, insert, text
from sqlalchemy.engine import URL, make_url

from owcopilot.contracts import OwnershipScope
from owcopilot.intake import (
    DOCX_MIME,
    XLSX_MIME,
    IntakePolicy,
    SegmentDecision,
    TeamIntakeService,
    TriagePolicy,
    ValidatedUpload,
    derive_triage_verdict,
    parse_source_segments,
    validate_upload_chunks,
)
from owcopilot.teamdb import (
    TeamAuthorizationError,
    TeamDatabase,
    TeamDatabaseConfig,
    TeamIntakeConflict,
    TeamIntakeRepository,
    TeamNotFoundError,
)
from owcopilot.teamdb.schema import (
    EXPECTED_ALEMBIC_HEAD,
    MIGRATION_ROLE,
    RUNTIME_ROLE,
    containers,
    memberships,
    tenants,
)
from tests.intake.test_n21_intake_parsers import _docx_bytes, _xlsx_bytes

pytest.importorskip("psycopg")

_MIGRATION_URL_ENV = "OWCOPILOT_MIGRATION_DATABASE_URL"
_APP_URL_ENV = "OWCOPILOT_TEAM_DATABASE_URL"


@dataclass(frozen=True, slots=True)
class _PgEnvironment:
    migration_url: URL
    app_url: URL
    migration_engine: Engine

    def database(self) -> TeamDatabase:
        return TeamDatabase(
            TeamDatabaseConfig(
                database_url=self.app_url,
                pool_size=2,
                max_overflow=0,
                pool_timeout_seconds=5,
            )
        )


@dataclass(frozen=True, slots=True)
class _Seed:
    scope: OwnershipScope
    actor: str


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
        pytest.fail("N2.1 repository integration URLs must use postgresql+psycopg")
    if migration_url.username != MIGRATION_ROLE or app_url.username != RUNTIME_ROLE:
        pytest.fail("N2.1 repository integration URLs use the wrong PostgreSQL roles")

    migration_engine = create_engine(migration_url, pool_pre_ping=True)
    environment = _PgEnvironment(
        migration_url=migration_url,
        app_url=app_url,
        migration_engine=migration_engine,
    )
    database = environment.database()
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
        health = database.health()
        assert health.ready, health.as_dict()
        yield environment
    finally:
        database.close()
        migration_engine.dispose()


def _seed_scope(
    environment: _PgEnvironment,
    *,
    tenant_id: str | None = None,
    actor: str | None = None,
) -> _Seed:
    resolved_tenant = tenant_id or str(uuid4())
    resolved_actor = actor or str(uuid4())
    container_id = str(uuid4())
    suffix = uuid4().hex
    with environment.migration_engine.begin() as connection:
        if tenant_id is None:
            connection.execute(
                insert(tenants).values(
                    tenant_id=UUID(resolved_tenant),
                    slug=f"n21-repository-tenant-{suffix}",
                    display_name=f"N2.1 repository tenant {suffix}",
                )
            )
            connection.execute(
                insert(memberships).values(
                    tenant_id=UUID(resolved_tenant),
                    subject_id=UUID(resolved_actor),
                    role="owner",
                    active=True,
                )
            )
        connection.execute(
            insert(containers).values(
                tenant_id=UUID(resolved_tenant),
                container_id=UUID(container_id),
                slug=f"n21-repository-container-{suffix}",
                display_name=f"N2.1 repository container {suffix}",
            )
        )
    return _Seed(
        scope=OwnershipScope(tenant_id=resolved_tenant, container_id=container_id),
        actor=resolved_actor,
    )


def _service(
    environment: _PgEnvironment,
) -> tuple[TeamDatabase, TeamIntakeRepository, TeamIntakeService]:
    database = environment.database()
    repository = TeamIntakeRepository(database)
    return database, repository, TeamIntakeService(repository)


def _replacement_docx_bytes() -> bytes:
    source = BytesIO(_docx_bytes())
    target = BytesIO()
    with (
        ZipFile(source, "r") as source_archive,
        ZipFile(target, "w", compression=ZIP_DEFLATED) as target_archive,
    ):
        for entry in source_archive.infolist():
            payload = source_archive.read(entry.filename)
            if entry.filename == "word/document.xml":
                payload = payload.replace(
                    b"Canon paragraph",
                    b"Replacement canon paragraph",
                )
            target_archive.writestr(entry, payload)
    return target.getvalue()


def _complete_replacement_run(
    repository: TeamIntakeRepository,
    seed: _Seed,
    *,
    run_id: str,
    artifact_version_id: str,
    upload: ValidatedUpload,
) -> Mapping[str, object]:
    segments = parse_source_segments(upload, artifact_version_id=artifact_version_id)
    repository.store_segments(
        seed.scope,
        actor_subject=seed.actor,
        run_id=run_id,
        artifact_version_id=artifact_version_id,
        segments=segments,
    )
    for segment in segments:
        raw_output_id = str(uuid4())
        raw_output = ""
        repository.store_raw_triage_output(
            seed.scope,
            actor_subject=seed.actor,
            run_id=run_id,
            artifact_version_id=artifact_version_id,
            segment_id=segment.segment_id,
            raw_output_id=raw_output_id,
            provider="not-configured",
            model="none",
            output_text=raw_output,
            output_hash=hashlib.sha256(raw_output.encode("utf-8")).hexdigest(),
        )
        repository.store_triage_verdict(
            seed.scope,
            actor_subject=seed.actor,
            run_id=run_id,
            artifact_version_id=artifact_version_id,
            verdict=derive_triage_verdict(
                raw_output_id=raw_output_id,
                raw_output=raw_output,
                segment=segment,
                policy=TriagePolicy(),
            ),
        )
    repository.mark_awaiting_human(
        seed.scope,
        actor_subject=seed.actor,
        run_id=run_id,
    )
    return repository.read_intake_run(
        seed.scope,
        subject_id=seed.actor,
        run_id=run_id,
    )


def _assert_complete_machine_truth(
    state: Mapping[str, object], *, expected_kinds: frozenset[str]
) -> None:
    segments = state["segments"]
    raw_outputs = state["raw_triage_outputs"]
    verdicts = state["triage_verdicts"]
    assert isinstance(segments, list) and segments
    assert isinstance(raw_outputs, list) and len(raw_outputs) == len(segments)
    assert isinstance(verdicts, list) and len(verdicts) == len(segments)
    assert {
        str(item["locator"]["kind"])
        for item in segments
        if isinstance(item, dict) and isinstance(item.get("locator"), dict)
    } == expected_kinds
    assert all(
        isinstance(item, dict)
        and item["output_hash"]
        == hashlib.sha256(str(item["output_text"]).encode("utf-8")).hexdigest()
        for item in raw_outputs
    )
    assert all(
        isinstance(item, dict) and item["content_role"] == "UNKNOWN" and item["abstain"] is True
        for item in verdicts
    )
    assert state["allowed_uses"] == {
        "ANALYSIS": False,
        "RETRIEVAL": False,
        "DRAFTING": False,
        "TRAINING": False,
        "PUBLICATION": False,
    }
    assert state["drafting_allowed"] is False


@pytest.mark.parametrize(
    ("filename", "mime_type", "payload_factory", "expected_locators"),
    [
        (
            "source.docx",
            DOCX_MIME,
            _docx_bytes,
            frozenset({"docx_paragraph", "docx_table_cell"}),
        ),
        ("source.xlsx", XLSX_MIME, _xlsx_bytes, frozenset({"xlsx_cell"})),
    ],
)
def test_docx_and_xlsx_run_real_service_repository_pipeline(
    pg_environment: _PgEnvironment,
    filename: str,
    mime_type: str,
    payload_factory: Callable[[], bytes],
    expected_locators: frozenset[str],
) -> None:
    seed = _seed_scope(pg_environment)
    database, _repository, service = _service(pg_environment)
    try:
        payload = payload_factory()
        state = service.create_intake_run(
            seed.scope,
            actor_subject=seed.actor,
            original_filename=filename,
            mime_type=mime_type,
            chunks=(payload,),
        )
        assert state["status"] == "AWAITING_HUMAN"
        _assert_complete_machine_truth(state, expected_kinds=expected_locators)

        reread = service.read_intake_run(
            seed.scope,
            subject_id=seed.actor,
            run_id=str(state["run_id"]),
        )
        assert reread == state
    finally:
        database.close()


def test_human_governance_lineage_and_logical_delete_remain_reconstructable(
    pg_environment: _PgEnvironment,
) -> None:
    seed = _seed_scope(pg_environment)
    database, repository, service = _service(pg_environment)
    try:
        initial = service.create_intake_run(
            seed.scope,
            actor_subject=seed.actor,
            original_filename="governance.docx",
            mime_type=DOCX_MIME,
            chunks=(_docx_bytes(),),
        )
        run_id = str(initial["run_id"])
        rights_map = {
            "ANALYSIS": True,
            "RETRIEVAL": True,
            "DRAFTING": False,
            "TRAINING": False,
            "PUBLICATION": True,
        }
        with_rights = service.declare_rights(
            seed.scope,
            actor_subject=seed.actor,
            run_id=run_id,
            rights_provenance="OWNED",
            declaration_text="由项目所有者人工确认的合成验收材料。",
            allowed_uses=rights_map,
        )
        assert with_rights["allowed_uses"] == rights_map
        assert with_rights["drafting_allowed"] is False

        segments = cast_list(with_rights["segments"])
        role_plan = ("CANON_CANDIDATE", "REFERENCE", "UNKNOWN", "MIXED")
        assert len(segments) == len(role_plan)
        submitted = service.submit_human_decisions(
            seed.scope,
            actor_subject=seed.actor,
            run_id=run_id,
            decisions=(
                SegmentDecision(
                    segment_id=str(segment["segment_id"]),
                    decision="REJECT" if index == 3 else "OVERRIDE",
                    content_role=role_plan[index],
                    rationale="逐段人工确认。",
                )
                for index, segment in enumerate(segments)
            ),
        )
        assert submitted["submitted"] is True
        assert len(cast_list(submitted["human_decisions"])) == len(segments)

        canon_segment_id = str(segments[0]["segment_id"])
        reference_segment_id = str(segments[1]["segment_id"])
        unknown_segment_id = str(segments[2]["segment_id"])
        rejected_segment_id = str(segments[3]["segment_id"])
        repository.create_lineage_anchor(
            seed.scope,
            actor_subject=seed.actor,
            run_id=run_id,
            segment_id=canon_segment_id,
            lineage_kind="SOURCE_EVIDENCE",
            target_ref="canon:gate-rule",
        )
        with_lineage = repository.create_lineage_anchor(
            seed.scope,
            actor_subject=seed.actor,
            run_id=run_id,
            segment_id=reference_segment_id,
            lineage_kind="INSPIRATION_LINEAGE",
            target_ref="draft:night-gate-variant",
        )
        lineage = cast_list(with_lineage["lineage"])
        by_kind = {str(item["lineage_kind"]): str(item["target_ref"]) for item in lineage}
        assert by_kind == {
            "SOURCE_EVIDENCE": "canon:gate-rule",
            "INSPIRATION_LINEAGE": "draft:night-gate-variant",
        }
        invalid_lineage = (
            (reference_segment_id, "SOURCE_EVIDENCE"),
            (canon_segment_id, "INSPIRATION_LINEAGE"),
            (unknown_segment_id, "SOURCE_EVIDENCE"),
            (unknown_segment_id, "INSPIRATION_LINEAGE"),
            (rejected_segment_id, "SOURCE_EVIDENCE"),
            (rejected_segment_id, "INSPIRATION_LINEAGE"),
        )
        for invalid_segment_id, invalid_kind in invalid_lineage:
            with pytest.raises(TeamIntakeConflict):
                repository.create_lineage_anchor(
                    seed.scope,
                    actor_subject=seed.actor,
                    run_id=run_id,
                    segment_id=invalid_segment_id,
                    lineage_kind=invalid_kind,
                    target_ref="forbidden:lineage",
                )

        snapshot = {
            "artifact": with_lineage["artifact"],
            "segments": with_lineage["segments"],
            "raw": with_lineage["raw_triage_outputs"],
            "verdicts": with_lineage["triage_verdicts"],
        }
        repository.logical_delete_source_artifact(
            seed.scope,
            actor_subject=seed.actor,
            run_id=run_id,
        )
        after_delete = service.read_intake_run(
            seed.scope,
            subject_id=seed.actor,
            run_id=run_id,
        )
        assert after_delete["logical_deleted"] is True
        artifact = after_delete["artifact"]
        assert isinstance(artifact, dict) and artifact["immutable_bytes_retained"] is True
        assert artifact["content_hash"] == cast_dict(snapshot["artifact"])["content_hash"]
        assert after_delete["segments"] == snapshot["segments"]
        assert after_delete["raw_triage_outputs"] == snapshot["raw"]
        assert after_delete["triage_verdicts"] == snapshot["verdicts"]
    finally:
        database.close()


def test_replacement_version_preserves_v1_and_delete_revokes_effective_drafting(
    pg_environment: _PgEnvironment,
) -> None:
    seed = _seed_scope(pg_environment)
    database, repository, service = _service(pg_environment)
    try:
        v1 = service.create_intake_run(
            seed.scope,
            actor_subject=seed.actor,
            original_filename="version-one.docx",
            mime_type=DOCX_MIME,
            chunks=(_docx_bytes(),),
        )
        v1_run_id = str(v1["run_id"])
        artifact_id = str(cast_dict(v1["artifact"])["artifact_id"])
        with_rights = service.declare_rights(
            seed.scope,
            actor_subject=seed.actor,
            run_id=v1_run_id,
            rights_provenance="OWNED",
            declaration_text="人工允许此版本用于草拟，逻辑删除后必须立即失效。",
            allowed_uses={"DRAFTING": True},
        )
        v1_segments = cast_list(with_rights["segments"])
        service.submit_human_decisions(
            seed.scope,
            actor_subject=seed.actor,
            run_id=v1_run_id,
            decisions=(
                SegmentDecision(
                    segment_id=str(segment["segment_id"]),
                    decision="OVERRIDE",
                    content_role="CANON_CANDIDATE",
                    rationale="人工确认第一版证据。",
                )
                for segment in v1_segments
            ),
        )
        v1_with_lineage = repository.create_lineage_anchor(
            seed.scope,
            actor_subject=seed.actor,
            run_id=v1_run_id,
            segment_id=str(v1_segments[0]["segment_id"]),
            lineage_kind="SOURCE_EVIDENCE",
            target_ref="canon:v1-evidence",
        )
        assert repository.is_use_allowed(
            seed.scope,
            subject_id=seed.actor,
            run_id=v1_run_id,
            use_kind="DRAFTING",
        )
        v1_snapshot = {
            "artifact": v1_with_lineage["artifact"],
            "segments": v1_with_lineage["segments"],
            "lineage": v1_with_lineage["lineage"],
        }

        v2_bytes = _replacement_docx_bytes()
        v2_upload = validate_upload_chunks(
            (v2_bytes,),
            original_filename="version-two.docx",
            mime_type=DOCX_MIME,
            policy=IntakePolicy(),
        )
        v2_version_id = str(uuid4())
        v2_run_id = str(uuid4())
        repository.create_replacement_uploaded_run(
            seed.scope,
            actor_subject=seed.actor,
            artifact_id=artifact_id,
            artifact_version_id=v2_version_id,
            run_id=v2_run_id,
            upload=v2_upload,
        )
        v2 = _complete_replacement_run(
            repository,
            seed,
            run_id=v2_run_id,
            artifact_version_id=v2_version_id,
            upload=v2_upload,
        )
        v1_artifact = cast_dict(v1_snapshot["artifact"])
        v2_artifact = cast_dict(v2["artifact"])
        assert v2_artifact["artifact_id"] == artifact_id == v1_artifact["artifact_id"]
        assert v2_artifact["artifact_version_id"] != v1_artifact["artifact_version_id"]
        assert v2_artifact["content_hash"] == hashlib.sha256(v2_bytes).hexdigest()
        assert v2_artifact["content_hash"] != v1_artifact["content_hash"]

        v1_after_v2 = service.read_intake_run(
            seed.scope,
            subject_id=seed.actor,
            run_id=v1_run_id,
        )
        assert v1_after_v2["artifact"] == v1_snapshot["artifact"]
        assert v1_after_v2["segments"] == v1_snapshot["segments"]
        assert v1_after_v2["lineage"] == v1_snapshot["lineage"]

        with pytest.raises(TeamIntakeConflict, match="exact bytes"):
            repository.create_replacement_uploaded_run(
                seed.scope,
                actor_subject=seed.actor,
                artifact_id=artifact_id,
                artifact_version_id=str(uuid4()),
                run_id=str(uuid4()),
                upload=v2_upload,
            )

        deleted = repository.logical_delete_source_artifact(
            seed.scope,
            actor_subject=seed.actor,
            run_id=v1_run_id,
        )
        assert deleted["drafting_allowed"] is False
        assert (
            repository.is_use_allowed(
                seed.scope,
                subject_id=seed.actor,
                run_id=v1_run_id,
                use_kind="DRAFTING",
            )
            is False
        )
        v2_after_delete = service.read_intake_run(
            seed.scope,
            subject_id=seed.actor,
            run_id=v2_run_id,
        )
        assert v2_after_delete["logical_deleted"] is True
        assert v2_after_delete["drafting_allowed"] is False
        assert v2_after_delete["segments"] == v2["segments"]
    finally:
        database.close()


def test_cross_container_and_missing_membership_fail_closed(
    pg_environment: _PgEnvironment,
) -> None:
    seed = _seed_scope(pg_environment)
    other_container = _seed_scope(
        pg_environment,
        tenant_id=seed.scope.tenant_id,
        actor=seed.actor,
    )
    database, _repository, service = _service(pg_environment)
    try:
        state = service.create_intake_run(
            seed.scope,
            actor_subject=seed.actor,
            original_filename="isolated.xlsx",
            mime_type=XLSX_MIME,
            chunks=(_xlsx_bytes(),),
        )
        run_id = str(state["run_id"])
        with pytest.raises(TeamNotFoundError):
            service.read_intake_run(
                other_container.scope,
                subject_id=seed.actor,
                run_id=run_id,
            )
        with pytest.raises(TeamAuthorizationError):
            service.read_intake_run(
                seed.scope,
                subject_id=str(uuid4()),
                run_id=run_id,
            )
    finally:
        database.close()


def cast_list(value: object) -> list[dict[str, object]]:
    assert isinstance(value, list)
    assert all(isinstance(item, dict) for item in value)
    return value


def cast_dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value
