from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from owcopilot.platform import AuditEntry  # noqa: E402
from owcopilot.service.api import create_app  # noqa: E402


@pytest.fixture
def platform_client(monkeypatch) -> tuple[TestClient, object]:
    monkeypatch.setenv("OWCOPILOT_JWT_SECRET", "platform-control-test-secret")
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    monkeypatch.delenv("OWCOPILOT_PLATFORM_DB", raising=False)
    app = create_app()
    return TestClient(app), app


def _create_tenant(client: TestClient, tenant_id: str = "studio") -> None:
    response = client.post(
        "/platform/tenants",
        json={
            "tenant_id": tenant_id,
            "name": "Studio",
            "owner_email": f"owner@{tenant_id}.example",
        },
    )
    assert response.status_code == 201, response.text


def _mint_owner(client: TestClient, tenant_id: str = "studio") -> tuple[str, dict]:
    response = client.post(
        "/platform/auth/dev-token",
        json={"user_id": f"{tenant_id}_owner", "tenant_id": tenant_id, "role": "owner"},
    )
    assert response.status_code == 200, response.text
    return response.json()["token"], response.json()


def test_platform_members_tokens_and_revocation_close_the_control_loop(platform_client) -> None:
    client, _app = platform_client
    _create_tenant(client)

    created_member = client.post(
        "/platform/memberships",
        json={
            "tenant_id": "studio",
            "user_id": "writer",
            "email": "writer@example.test",
            "role": "editor",
        },
    )
    assert created_member.status_code == 201, created_member.text

    members = client.get("/platform/memberships", params={"tenant_id": "studio", "limit": 20})
    assert members.status_code == 200, members.text
    assert members.json()["total"] == 2
    assert {row["user_id"] for row in members.json()["memberships"]} == {
        "studio_owner",
        "writer",
    }

    updated = client.post(
        "/platform/memberships",
        json={
            "tenant_id": "studio",
            "user_id": "writer",
            "email": "writer-renamed@example.test",
            "role": "reviewer",
        },
    )
    assert updated.status_code == 201, updated.text
    refreshed = client.get(
        "/platform/memberships", params={"tenant_id": "studio", "limit": 20}
    ).json()
    assert (
        next(row for row in refreshed["memberships"] if row["user_id"] == "writer")["role"]
        == "reviewer"
    )
    assert (
        next(row for row in refreshed["memberships"] if row["user_id"] == "writer")["email"]
        == "writer-renamed@example.test"
    )

    removed = client.delete("/platform/memberships/studio/writer")
    assert removed.status_code == 200, removed.text
    assert removed.json()["removed_user_id"] == "writer"
    last_owner = client.delete("/platform/memberships/studio/studio_owner")
    assert last_owner.status_code == 409

    raw_token, token_meta = _mint_owner(client)
    assert token_meta["id"].startswith("dt_")
    assert token_meta["expires_at"]
    bearer = {"Authorization": f"Bearer {raw_token}"}
    me = client.get("/platform/me", headers=bearer)
    assert me.status_code == 200, me.text
    assert me.json()["tenant_id"] == "studio" and me.json()["role"] == "owner"

    tokens = client.get("/platform/auth/dev-tokens", params={"tenant_id": "studio", "limit": 20})
    assert tokens.status_code == 200, tokens.text
    assert tokens.json()["tokens"][0]["id"] == token_meta["id"]
    assert tokens.json()["tokens"][0]["revoked_at"] is None

    revoked = client.post(
        f"/platform/auth/dev-tokens/{token_meta['id']}:revoke",
        params={"tenant_id": "studio"},
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked"] is True
    rejected = client.get("/platform/me", headers=bearer)
    assert rejected.status_code == 401
    assert "撤销" in rejected.text

    tokens_after = client.get(
        "/platform/auth/dev-tokens", params={"tenant_id": "studio", "limit": 20}
    ).json()
    assert tokens_after["tokens"][0]["revoked_at"]


def test_platform_audit_keeps_legacy_shape_and_uses_snapshot_cursors(platform_client) -> None:
    client, app = platform_client
    _create_tenant(client, "audit_tenant")
    owner_token, _meta = _mint_owner(client, "audit_tenant")
    headers = {"Authorization": f"Bearer {owner_token}"}
    store = app.state.platform_store
    for index in range(45):
        store.record(
            AuditEntry(
                tenant_id="audit_tenant",
                user_id="auditor",
                action="fixture.event",
                target=f"target-{index:03d}",
            )
        )

    legacy = client.get("/platform/audit", headers=headers)
    assert legacy.status_code == 200, legacy.text
    assert set(legacy.json()) == {"tenant_id", "entries"}
    assert len(legacy.json()["entries"]) >= 47

    first = client.get("/platform/audit", headers=headers, params={"limit": 20})
    assert first.status_code == 200, first.text
    body = first.json()
    assert len(body["entries"]) == 20
    assert body["total"] == len(legacy.json()["entries"])
    assert body["entries"][0]["target"] == "target-044"
    assert body["next_cursor"] and body["snapshot_etag"]

    second = client.get(
        "/platform/audit",
        headers=headers,
        params={"limit": 20, "cursor": body["next_cursor"]},
    )
    assert second.status_code == 200, second.text
    assert second.json()["snapshot_etag"] == body["snapshot_etag"]

    invalid = client.get("/platform/audit", headers=headers, params={"limit": 20, "cursor": "bad"})
    assert invalid.status_code == 422

    store.record(
        AuditEntry(
            tenant_id="audit_tenant",
            user_id="auditor",
            action="fixture.changed",
            target="new",
        )
    )
    stale = client.get(
        "/platform/audit",
        headers=headers,
        params={"limit": 20, "cursor": body["next_cursor"]},
    )
    assert stale.status_code == 409

    selected = client.get(
        "/platform/audit",
        params={"tenant_id": "audit_tenant", "limit": 20},
    )
    assert selected.status_code == 200, selected.text
    assert selected.json()["tenant_id"] == "audit_tenant"
    assert selected.json()["entries"][0]["target"] == "new"


def test_non_owner_and_cross_tenant_management_are_rejected(platform_client) -> None:
    client, _app = platform_client
    _create_tenant(client, "tenant_a")
    _create_tenant(client, "tenant_b")
    editor = client.post(
        "/platform/auth/dev-token",
        json={"user_id": "editor", "tenant_id": "tenant_a", "role": "editor"},
    ).json()["token"]
    editor_headers = {"Authorization": f"Bearer {editor}"}
    forbidden = client.get(
        "/platform/memberships",
        headers=editor_headers,
        params={"tenant_id": "tenant_a", "limit": 20},
    )
    assert forbidden.status_code == 403

    owner, _meta = _mint_owner(client, "tenant_a")
    cross_tenant = client.get(
        "/platform/memberships",
        headers={"Authorization": f"Bearer {owner}"},
        params={"tenant_id": "tenant_b", "limit": 20},
    )
    assert cross_tenant.status_code == 403
    cross_tenant_audit = client.get(
        "/platform/audit",
        headers={"Authorization": f"Bearer {owner}"},
        params={"tenant_id": "tenant_b", "limit": 20},
    )
    assert cross_tenant_audit.status_code == 403

    invalid_role = client.post(
        "/platform/memberships",
        json={
            "tenant_id": "tenant_a",
            "user_id": "bad",
            "email": "bad@example.test",
            "role": "superadmin",
        },
    )
    assert invalid_role.status_code == 422
