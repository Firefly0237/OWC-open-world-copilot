"""Unit boundary tests for the PostgreSQL-only N1.2 team HTTP surface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from owcopilot.platform import Principal, Role, mint_token  # noqa: E402
from owcopilot.service import api  # noqa: E402
from owcopilot.service.api import create_app  # noqa: E402
from owcopilot.teamdb.errors import (  # noqa: E402
    TeamAuthorizationError,
    TeamDatabaseUnavailable,
    TeamSchemaMismatch,
)

TENANT_ID = "11111111-1111-4111-8111-111111111111"
SUBJECT_ID = "22222222-2222-4222-8222-222222222222"
CONTAINER_ID = "33333333-3333-4333-8333-333333333333"


@dataclass(frozen=True)
class _Health:
    ready: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "database_available": self.ready,
            "schema_head": "n12_0002" if self.ready else None,
            "expected_schema_head": "n12_0002",
            "detail": "ready" if self.ready else "unavailable",
        }


class _Database:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self.closed = False

    def health(self) -> _Health:
        return _Health(ready=self.ready)

    def close(self) -> None:
        self.closed = True


@dataclass(frozen=True)
class _Grant:
    role: str


class _Repository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.auth_error: Exception | None = None
        self.read_error: Exception | None = None

    def authenticate_membership(self, *, tenant_id: str, subject_id: str) -> _Grant:
        self.calls.append(("authenticate", (tenant_id, subject_id)))
        if self.auth_error is not None:
            raise self.auth_error
        return _Grant(role="owner")

    def read_canonical(
        self,
        scope: object,
        *,
        subject_id: str,
        head: str,
    ) -> dict[str, Any]:
        self.calls.append(("read", (scope, subject_id, head)))
        if self.read_error is not None:
            raise self.read_error
        return {
            "selected_head": head,
            "objects": [
                {
                    "logical_id": "character:alice",
                    "object_type": "character",
                    "payload_json": '{"source_ref":"characters/alice.json"}',
                },
                {
                    "logical_id": "relation_set:abc",
                    "object_type": "relation_set",
                    "payload_json": '{"relations":[{"source_ref":"relations.json"}]}',
                },
            ],
            "memberships": [
                {"logical_id": "character:alice", "tombstone": False},
                {"logical_id": "character:deleted", "tombstone": True},
            ],
        }


def _token(*, role: Role = Role.VIEWER, tenant_id: str = TENANT_ID) -> str:
    return mint_token(
        Principal(user_id=SUBJECT_ID, tenant_id=tenant_id, role=role),
        secret="n12-team-secret",
    )


def _client(monkeypatch, repository: _Repository | None = None) -> tuple[TestClient, _Repository]:
    monkeypatch.setenv("OWCOPILOT_PROFILE", "team")
    monkeypatch.setenv("OWCOPILOT_JWT_SECRET", "n12-team-secret")
    resolved = repository or _Repository()
    return (
        TestClient(
            create_app(
                team_database=_Database(),
                team_repository=resolved,
            )
        ),
        resolved,
    )


def test_team_composition_never_constructs_local_truth_dependencies(monkeypatch) -> None:
    monkeypatch.setenv("OWCOPILOT_PROFILE", "team")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("team composition touched a local-only dependency")

    monkeypatch.setattr(api, "_build_cache", forbidden)
    monkeypatch.setattr(api, "JobManager", forbidden)
    app = create_app(team_database=_Database(), team_repository=_Repository())

    assert app.state.deployment_profile == "team"
    for local_attribute in ("platform_store", "jobs_manager", "projects", "v2_issues"):
        assert not hasattr(app.state, local_attribute)


def test_team_health_is_unhealthy_when_postgresql_is_not_ready(monkeypatch) -> None:
    monkeypatch.setenv("OWCOPILOT_PROFILE", "team")
    app = create_app(team_database=_Database(ready=False), team_repository=_Repository())

    response = TestClient(app).get("/health")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["database"]["ready"] is False


def test_team_canonical_uses_database_membership_not_jwt_role(monkeypatch) -> None:
    client, repository = _client(monkeypatch)

    response = client.get(
        f"/team/containers/{CONTAINER_ID}/canonical?head=authoring",
        headers={"Authorization": f"Bearer {_token(role=Role.VIEWER)}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope"] == {"tenant_id": TENANT_ID, "container_id": CONTAINER_ID}
    assert body["authorization"] == {
        "subject_id": SUBJECT_ID,
        "membership_role": "owner",
        "jwt_role_is_authoritative": False,
    }
    assert body["summary"] == {
        "object_count": 2,
        "membership_count": 2,
        "tombstone_count": 1,
        "relation_set_count": 1,
        "source_ref_count": 2,
    }
    assert [name for name, _payload in repository.calls] == ["authenticate", "read"]


def test_team_canonical_pages_on_server_and_rejects_stale_cursor(monkeypatch) -> None:
    client, repository = _client(monkeypatch)
    headers = {"Authorization": f"Bearer {_token()}"}
    url = f"/team/containers/{CONTAINER_ID}/canonical?head=authoring&limit=1"

    first = client.get(url, headers=headers)
    assert first.status_code == 200, first.text
    body = first.json()
    assert len(body["objects"]) == 1
    assert body["memberships"] == []
    assert body["pagination"]["total"] == 2
    cursor = body["pagination"]["next_cursor"]
    assert cursor

    second = client.get(f"{url}&cursor={cursor}", headers=headers)
    assert second.status_code == 200, second.text
    assert second.json()["objects"][0]["logical_id"] == "relation_set:abc"

    original = repository.read_canonical

    def changed(*args, **kwargs):
        state = original(*args, **kwargs)
        state["objects"].append(
            {"logical_id": "quest:new", "object_type": "quest", "payload_json": "{}"}
        )
        return state

    repository.read_canonical = changed  # type: ignore[method-assign]
    stale = client.get(f"{url}&cursor={cursor}", headers=headers)
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "STALE_CURSOR"


def test_team_canonical_memberships_are_independently_bounded(monkeypatch) -> None:
    client, _repository = _client(monkeypatch)
    response = client.get(
        f"/team/containers/{CONTAINER_ID}/canonical?collection=memberships&limit=1",
        headers={"Authorization": f"Bearer {_token()}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["objects"] == []
    assert len(body["memberships"]) == 1
    assert body["pagination"]["collection"] == "memberships"
    assert body["pagination"]["total"] == 2


def test_team_canonical_rejects_loopback_and_invalid_identifiers_before_repository(
    monkeypatch,
) -> None:
    client, repository = _client(monkeypatch)

    no_bearer = client.get(
        f"/team/containers/{CONTAINER_ID}/canonical",
        headers={"X-API-Key": "loopback-must-not-authorize-team"},
    )
    bad_container = client.get(
        "/team/containers/not-a-uuid/canonical",
        headers={"Authorization": f"Bearer {_token()}"},
    )

    assert no_bearer.status_code == 401
    assert bad_container.status_code == 422
    assert repository.calls == []


def test_team_canonical_fails_closed_for_membership_and_database_faults(monkeypatch) -> None:
    repository = _Repository()
    client, _ = _client(monkeypatch, repository)
    headers = {"Authorization": f"Bearer {_token()}"}
    url = f"/team/containers/{CONTAINER_ID}/canonical"

    repository.auth_error = TeamAuthorizationError("no active membership")
    denied = client.get(url, headers=headers)
    assert denied.status_code == 403

    repository.auth_error = None
    repository.read_error = TeamDatabaseUnavailable("postgres stopped")
    unavailable = client.get(url, headers=headers)
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["code"] == "DATABASE_UNAVAILABLE"

    repository.read_error = TeamSchemaMismatch("wrong alembic head")
    mismatch = client.get(url, headers=headers)
    assert mismatch.status_code == 503
    assert mismatch.json()["detail"]["code"] == "SCHEMA_NOT_READY"


def test_all_unmigrated_team_routes_are_explicitly_disabled(monkeypatch) -> None:
    client, repository = _client(monkeypatch)

    response = client.post("/projects/demo/audits", json={"content": {}, "persist": False})

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "NOT_AVAILABLE",
        "profile": "team",
        "capability": "team_unmigrated_route:/projects/demo/audits",
        "message": (
            "team_unmigrated_route:/projects/demo/audits is not available in team profile "
            "until it has shared ownership and durable storage"
        ),
    }
    assert repository.calls == []
