"""WS-P · platform HTTP surface: tenant bootstrap, dev-token mint, principal resolution, RBAC gate.

Uses the module app via TestClient (no network). The loopback (no OWCOPILOT_API_KEY) resolves to the
single-tenant LOCAL owner, so existing endpoints are unchanged; these endpoints add the SaaS plane.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="install with: pip install -e '.[serve]'")

from fastapi.testclient import TestClient  # noqa: E402

from owcopilot.service.api import app  # noqa: E402

client = TestClient(app)


def test_loopback_is_local_owner() -> None:
    me = client.get("/platform/me").json()
    assert me["tenant_id"] == "local" and me["role"] == "owner" and me["is_loopback"] is True


def test_tenant_bootstrap_token_and_rbac(monkeypatch) -> None:
    monkeypatch.setenv("OWCOPILOT_JWT_SECRET", "api-test-secret")

    created = client.post(
        "/platform/tenants",
        json={"tenant_id": "t_api", "name": "Studio", "owner_email": "o@x.com"},
    )
    assert created.status_code == 201

    # mint an EDITOR token for the tenant (owner-gated mint; loopback is owner)
    tok = client.post(
        "/platform/auth/dev-token",
        json={"user_id": "u_api", "tenant_id": "t_api", "role": "editor"},
    ).json()["token"]
    headers = {"Authorization": f"Bearer {tok}"}

    me = client.get("/platform/me", headers=headers).json()
    assert me["tenant_id"] == "t_api" and me["role"] == "editor" and me["is_loopback"] is False

    # an editor cannot create tenants (owner-only) -> 403, proving the RBAC gate over HTTP
    forbidden = client.post(
        "/platform/tenants",
        headers=headers,
        json={"tenant_id": "t_x", "name": "X", "owner_email": "x@x.com"},
    )
    assert forbidden.status_code == 403

    # the new tenant's audit log shows the create (scoped to that tenant)
    owner_tok = client.post(
        "/platform/auth/dev-token",
        json={"user_id": "u_api", "tenant_id": "t_api", "role": "owner"},
    ).json()["token"]
    audit = client.get("/platform/audit", headers={"Authorization": f"Bearer {owner_tok}"}).json()
    assert any(e["action"] == "tenant.create" for e in audit["entries"])


def test_bad_bearer_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("OWCOPILOT_JWT_SECRET", "api-test-secret")
    r = client.get("/platform/me", headers={"Authorization": "Bearer not.a.token"})
    assert r.status_code == 401
