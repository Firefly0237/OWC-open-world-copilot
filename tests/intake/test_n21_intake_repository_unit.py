"""No-PostgreSQL boundary tests for the N2.1 intake repository."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.engine import RowMapping

from owcopilot.contracts import OwnershipScope
from owcopilot.intake import DOCX_MIME, DocxParagraphLocator, SourceSegment, ValidatedUpload
from owcopilot.teamdb import (
    TeamAuthorizationError,
    TeamIntakeRepository,
    TeamIntegrityError,
)

TENANT_ID = "11111111-1111-4111-8111-111111111111"
CONTAINER_ID = "22222222-2222-4222-8222-222222222222"
SUBJECT_ID = "33333333-3333-4333-8333-333333333333"
ARTIFACT_ID = "44444444-4444-4444-8444-444444444444"
VERSION_ID = "55555555-5555-4555-8555-555555555555"
RUN_ID = "66666666-6666-4666-8666-666666666666"
SEGMENT_ID = "77777777-7777-4777-8777-777777777777"
_NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


class _MembershipResult:
    def __init__(self, row: dict[str, object] | None) -> None:
        self._row = row

    def mappings(self) -> _MembershipResult:
        return self

    def one_or_none(self) -> dict[str, object] | None:
        return self._row


class _MembershipConnection:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row
        self.execute_count = 0

    def execute(self, _statement: object) -> _MembershipResult:
        self.execute_count += 1
        return _MembershipResult(self.row)


class _Database:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.connection = _MembershipConnection(row)
        self.scopes: list[OwnershipScope] = []

    @contextmanager
    def transaction(self, scope: OwnershipScope):  # type: ignore[no-untyped-def]
        self.scopes.append(scope)
        yield self.connection


def _scope() -> OwnershipScope:
    return OwnershipScope(tenant_id=TENANT_ID, container_id=CONTAINER_ID)


def _membership(role: str, *, active: bool = True) -> dict[str, object]:
    return {"role": role, "active": active}


def _upload(content: bytes = b"PK\x03\x04synthetic-docx") -> ValidatedUpload:
    return ValidatedUpload(
        original_filename="source.docx",
        extension=".docx",
        mime_type=DOCX_MIME,
        byte_size=len(content),
        content_hash=hashlib.sha256(content).hexdigest(),
        content_bytes=content,
    )


def _run_row(content: bytes = b"immutable source bytes") -> RowMapping:
    return cast(
        RowMapping,
        {
            "run_id": UUID(RUN_ID),
            "artifact_version_id": UUID(VERSION_ID),
            "original_filename": "source.docx",
            "status": "AWAITING_HUMAN",
            "run_created_by": UUID(SUBJECT_ID),
            "started_at": _NOW,
            "finished_at": _NOW,
            "rejection_code": None,
            "processing_code": None,
            "artifact_id": UUID(ARTIFACT_ID),
            "content_hash": hashlib.sha256(content).hexdigest(),
            "mime_type": DOCX_MIME,
            "extension": ".docx",
            "byte_size": len(content),
            "content_bytes": content,
            "version_created_by": UUID(SUBJECT_ID),
            "version_created_at": _NOW,
            "display_name": "source.docx",
            "artifact_created_by": UUID(SUBJECT_ID),
            "artifact_created_at": _NOW,
            "logical_deleted_at": None,
        },
    )


def test_every_member_transaction_rechecks_database_membership() -> None:
    database = _Database(_membership("viewer"))
    repository = TeamIntakeRepository(cast(Any, database))

    with repository._member_transaction(  # noqa: SLF001 - authorization boundary unit test
        _scope(), subject_id=SUBJECT_ID, write=False
    ):
        pass

    assert database.scopes == [_scope()]
    assert database.connection.execute_count == 1


@pytest.mark.parametrize(
    "membership",
    [None, _membership("viewer", active=False), _membership("unknown")],
)
def test_read_membership_fails_closed_when_not_active_and_known(
    membership: dict[str, object] | None,
) -> None:
    repository = TeamIntakeRepository(cast(Any, _Database(membership)))

    with pytest.raises(TeamAuthorizationError):
        with repository._member_transaction(  # noqa: SLF001
            _scope(), subject_id=SUBJECT_ID, write=False
        ):
            pass


@pytest.mark.parametrize("role", ["viewer", "reviewer"])
def test_write_membership_is_reloaded_and_requires_owner_or_editor(role: str) -> None:
    repository = TeamIntakeRepository(cast(Any, _Database(_membership(role))))

    with pytest.raises(TeamAuthorizationError, match="editor or owner"):
        with repository._member_transaction(  # noqa: SLF001
            _scope(), subject_id=SUBJECT_ID, write=True
        ):
            pass


def test_upload_boundary_recomputes_bytes_hash_and_size() -> None:
    valid = _upload()
    assert TeamIntakeRepository._validate_upload(valid) is valid  # noqa: SLF001

    forged_hash = replace(valid, content_hash="0" * 64)
    with pytest.raises(TeamIntegrityError, match="hash"):
        TeamIntakeRepository._validate_upload(forged_hash)  # noqa: SLF001

    forged_size = replace(valid, byte_size=valid.byte_size + 1)
    with pytest.raises(TeamIntegrityError, match="size"):
        TeamIntakeRepository._validate_upload(forged_size)  # noqa: SLF001


def test_segments_are_version_bound_and_contiguous() -> None:
    first = SourceSegment.from_text(
        segment_id=SEGMENT_ID,
        artifact_version_id=VERSION_ID,
        ordinal=0,
        locator=DocxParagraphLocator(paragraph_index=0),
        text="第一段",
    )
    second = SourceSegment.from_text(
        segment_id="88888888-8888-4888-8888-888888888888",
        artifact_version_id=VERSION_ID,
        ordinal=2,
        locator=DocxParagraphLocator(paragraph_index=1),
        text="第二段",
    )

    with pytest.raises(TeamIntegrityError, match="contiguous"):
        TeamIntakeRepository._validate_segments(  # noqa: SLF001
            (first, second), artifact_version_id=VERSION_ID
        )


def test_rights_map_is_complete_and_defaults_every_omitted_use_to_false() -> None:
    normalized = TeamIntakeRepository._normalize_allowed_uses(  # noqa: SLF001
        {"ANALYSIS": True, "DRAFTING": False}
    )

    assert normalized == {
        "ANALYSIS": True,
        "RETRIEVAL": False,
        "DRAFTING": False,
        "TRAINING": False,
        "PUBLICATION": False,
    }


def test_run_reconstruction_recomputes_immutable_bytes_hash() -> None:
    TeamIntakeRepository._verify_run_row(_run_row())  # noqa: SLF001
    corrupted = dict(_run_row())
    corrupted["content_hash"] = "0" * 64

    with pytest.raises(TeamIntegrityError, match="content_hash"):
        TeamIntakeRepository._verify_run_row(cast(RowMapping, corrupted))  # noqa: SLF001


def test_team_composition_uses_dedicated_intake_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("fastapi")
    from owcopilot.service.api import create_app

    class _Health:
        ready = True

        def as_dict(self) -> dict[str, object]:
            return {"ready": True, "schema_head": "n21_0001"}

    class _DatabaseStub:
        def health(self) -> _Health:
            return _Health()

        def close(self) -> None:
            return None

    class _CanonicalRepository:
        pass

    monkeypatch.setenv("OWCOPILOT_PROFILE", "team")
    database = _DatabaseStub()
    canonical = _CanonicalRepository()
    app = create_app(team_database=database, team_repository=canonical)

    assert app.state.team_repository is canonical
    assert isinstance(app.state.team_intake_repository, TeamIntakeRepository)
    assert app.state.team_intake_repository.database is database
    assert app.state.team_intake_service.repository is app.state.team_intake_repository
