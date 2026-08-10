"""N0.2a stopgap: hostile identifiers fail closed at trust and filesystem boundaries."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

pytest.importorskip("fastapi")
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from owcopilot.app import workspaces as workspace_app  # noqa: E402
from owcopilot.app.workspaces import (  # noqa: E402
    create_managed_world,
    export_world_zip,
    managed_world_path,
)
from owcopilot.content.models import ContentBundle, Entity, EntityType  # noqa: E402
from owcopilot.content.store import ContentStore  # noqa: E402
from owcopilot.platform import (  # noqa: E402
    AuditEntry,
    AuthError,
    Membership,
    Principal,
    Role,
    Tenant,
    mint_token,
    tenant_world_root,
    verify_token,
)
from owcopilot.service.api import create_app  # noqa: E402
from owcopilot.service.dependencies import (  # noqa: E402
    _project_content_root,
    _project_sqlite_path,
    _validate_project_id,
)
from owcopilot.service.routers import exports as exports_router  # noqa: E402
from owcopilot.service.routers import workspaces as workspaces_router  # noqa: E402
from owcopilot.trust.identifiers import (  # noqa: E402
    resolve_path_segment,
    validate_path_segment,
    validate_tenant_id,
)
from owcopilot.trust.security import PathSecurityError  # noqa: E402

HOSTILE_IDENTIFIERS = (
    "../evil",
    "C:/Windows",
    "C:\\Windows",
    "/etc/passwd",
    "tenant/child",
    "tenant\\child",
    "tenant:child",
    "",
    ".",
    "..",
    "tenant\x00evil",
    "tenant\x1fevil",
    "tenant\x85evil",
    "%2e%2e%2fevil",
    "%2E%2E%5Cevil",
    "%252e%252e%252fevil",
    "tenant／child",  # NFKC compatibility slash
)


@pytest.fixture()
def local_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    content_root = tmp_path / "registered-content"
    ContentStore(content_root).save(
        ContentBundle(
            entities={
                "npc_guard": Entity(
                    id="npc_guard",
                    name="守门人",
                    type=EntityType.NPC,
                    description="Guards the boundary.",
                )
            }
        )
    )
    monkeypatch.setenv("OWCOPILOT_PROFILE", "local")
    monkeypatch.setenv("OWCOPILOT_PLATFORM_DB", ":memory:")
    monkeypatch.setenv("OWCOPILOT_JWT_SECRET", "n02-test-secret")
    monkeypatch.setenv("OWCOPILOT_WORLDS_HOME", str(tmp_path / "worlds"))
    monkeypatch.setenv(
        "OWCOPILOT_PROJECTS_JSON",
        json.dumps({"demo": str(content_root).replace("\\", "/")}, ensure_ascii=False),
    )
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    app = create_app()
    with TestClient(app) as client:
        yield client, content_root
    app.state.platform_store.close()


@pytest.mark.parametrize("hostile", HOSTILE_IDENTIFIERS)
def test_shared_path_segment_validator_rejects_hostile_forms(hostile: str) -> None:
    with pytest.raises(ValueError):
        validate_path_segment(hostile)
    with pytest.raises(ValueError):
        validate_tenant_id(hostile)


def test_shared_validator_preserves_legal_cjk_and_internal_space_exactly() -> None:
    for legal in ("tenant_a", "租户甲.v2", "盐汐 群岛"):
        assert validate_path_segment(legal) == legal


@pytest.mark.parametrize("hostile", HOSTILE_IDENTIFIERS)
def test_tenant_models_reject_hostile_identifiers_at_construction(hostile: str) -> None:
    constructors = (
        lambda: Tenant(id=hostile, name="Studio"),
        lambda: Membership(tenant_id=hostile, user_id="u1", role=Role.EDITOR),
        lambda: Principal(user_id="u1", tenant_id=hostile, role=Role.EDITOR),
        lambda: AuditEntry(
            tenant_id=hostile,
            user_id="u1",
            action="test",
        ),
    )
    for construct in constructors:
        with pytest.raises(ValidationError):
            construct()


@pytest.mark.parametrize("hostile", ("../other", "%252e%252e%252fother", 123))
def test_validly_signed_jwt_with_hostile_tenant_claim_is_rejected(hostile: object) -> None:
    # model_construct simulates a claim produced by an old/foreign issuer while keeping a valid
    # HMAC signature. Signature trust must not bypass identifier validation.
    untrusted = Principal.model_construct(
        user_id="u1",
        tenant_id=hostile,
        role=Role.OWNER,
        is_loopback=False,
    )
    token = mint_token(untrusted, secret="n02-test-secret")
    with pytest.raises(AuthError, match="tenant_id"):
        verify_token(token, secret="n02-test-secret")


def test_validly_signed_jwt_with_non_object_claims_is_auth_error() -> None:
    def b64url(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    header = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64url(json.dumps(["not", "an", "object"]).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = b64url(hmac.new(b"n02-test-secret", signing_input, hashlib.sha256).digest())
    with pytest.raises(AuthError, match="JSON 对象"):
        verify_token(f"{header}.{payload}.{signature}", secret="n02-test-secret")


def test_tenant_world_boundary_revalidates_bypassed_models_and_preserves_legal_names(
    tmp_path: Path,
) -> None:
    unsafe = Principal.model_construct(
        user_id="u1",
        tenant_id="../tenant_b",
        role=Role.OWNER,
        is_loopback=False,
    )
    with pytest.raises(ValueError):
        tenant_world_root(unsafe, "world", worlds_home=tmp_path)

    legal = Principal(user_id="u1", tenant_id="租户甲", role=Role.OWNER)
    target = tenant_world_root(legal, "盐汐 群岛", worlds_home=tmp_path)
    assert target == tmp_path.resolve() / "租户甲" / "盐汐 群岛"


def test_platform_rest_dtos_reject_hostile_tenant_values_and_accept_legal_cjk(
    local_client,
) -> None:
    client, _ = local_client
    cases = (
        (
            "/platform/tenants",
            lambda value: {
                "tenant_id": value,
                "name": "Studio",
                "owner_email": "owner@example.test",
            },
        ),
        (
            "/platform/memberships",
            lambda value: {
                "tenant_id": value,
                "user_id": "u1",
                "email": "u1@example.test",
                "role": "editor",
            },
        ),
        (
            "/platform/auth/dev-token",
            lambda value: {
                "tenant_id": value,
                "user_id": "u1",
                "role": "editor",
            },
        ),
    )
    for hostile in HOSTILE_IDENTIFIERS:
        for endpoint, payload in cases:
            response = client.post(endpoint, json=payload(hostile))
            assert response.status_code == 422, (endpoint, hostile, response.text)

    legal = "租户甲.v2"
    created = client.post(
        "/platform/tenants",
        json={
            "tenant_id": legal,
            "name": "Studio",
            "owner_email": "owner@example.test",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["tenant_id"] == legal
    member = client.post(
        "/platform/memberships",
        json={
            "tenant_id": legal,
            "user_id": "reviewer",
            "email": "reviewer@example.test",
            "role": "reviewer",
        },
    )
    assert member.status_code == 201, member.text
    assert member.json()["tenant_id"] == legal
    token_response = client.post(
        "/platform/auth/dev-token",
        json={"tenant_id": legal, "user_id": "reviewer", "role": "reviewer"},
    )
    assert token_response.status_code == 200, token_response.text
    principal = verify_token(token_response.json()["token"], secret="n02-test-secret")
    assert principal.tenant_id == legal


@pytest.mark.parametrize("hostile", HOSTILE_IDENTIFIERS)
def test_project_id_boundary_rejects_hostile_forms(hostile: str) -> None:
    with pytest.raises(HTTPException) as exc_info:
        _validate_project_id(hostile)
    assert exc_info.value.status_code == 400


def test_admin_project_and_runtime_paths_remain_explicit_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = "盐汐 群岛"
    admin_content_root = tmp_path / "admin-content-location"
    admin_content_root.mkdir()
    monkeypatch.setenv(
        "OWCOPILOT_PROJECTS_JSON",
        json.dumps({project_id: str(admin_content_root)}, ensure_ascii=False),
    )
    assert _project_content_root(project_id) == admin_content_root.resolve()

    admin_runtime = tmp_path / "admin-runtime-location"
    monkeypatch.setenv("OWCOPILOT_RUNTIME_DIR", str(admin_runtime))
    sqlite_path = _project_sqlite_path(project_id, admin_content_root)
    assert sqlite_path.parent == admin_runtime.resolve()
    assert sqlite_path.name.startswith(f"{project_id}-")


def test_resolve_path_segment_rejects_external_symlink_resolution_before_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    lexical_target = root.resolve() / "world"
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_resolved = outside.resolve()
    real_resolve = Path.resolve

    def resolve_as_external(self: Path, *args, **kwargs) -> Path:
        if self == lexical_target:
            return outside_resolved
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_as_external)
    with pytest.raises(PathSecurityError, match="escapes allowed root"):
        resolve_path_segment(root, "world")


def test_world_pack_checks_every_resolved_file_before_opening_archive_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "world"
    leaked_entry = source / "world" / "entities" / "npc_guard.json"
    leaked_entry.parent.mkdir(parents=True)
    leaked_entry.write_text("{}", encoding="utf-8")
    outside = tmp_path / "secret.json"
    outside.write_text('{"secret": true}', encoding="utf-8")
    outside_resolved = outside.resolve()
    real_resolve = Path.resolve
    writer_opened = False

    def resolve_as_external(self: Path, *args, **kwargs) -> Path:
        if self == leaked_entry:
            return outside_resolved
        return real_resolve(self, *args, **kwargs)

    def forbidden_writer(*args, **kwargs):
        nonlocal writer_opened
        writer_opened = True
        raise AssertionError("archive writer opened before containment preflight")

    monkeypatch.setattr(Path, "resolve", resolve_as_external)
    monkeypatch.setattr(workspace_app.zipfile, "ZipFile", forbidden_writer)
    with pytest.raises(PathSecurityError, match="escapes allowed root"):
        export_world_zip(source)
    assert writer_opened is False
    assert outside.read_text(encoding="utf-8") == '{"secret": true}'


def test_workspace_rest_rejects_hostile_names_without_outside_changes(local_client) -> None:
    client, content_root = local_client
    outside = content_root.parent / "outside.txt"
    outside.write_bytes(b"unchanged")
    for hostile in HOSTILE_IDENTIFIERS:
        created = client.post("/workspaces", json={"name": hostile})
        assert created.status_code in {400, 422}, (hostile, created.text)
        imported = client.post(
            "/workspaces:import",
            json={
                "name": hostile,
                "zip_base64": base64.b64encode(b"not-a-zip").decode(),
            },
        )
        assert imported.status_code in {400, 422}, (hostile, imported.text)
    assert outside.read_bytes() == b"unchanged"

    legal = "盐汐 群岛"
    created = client.post("/workspaces", json={"name": legal})
    assert created.status_code == 201, created.text
    assert created.json()["name"] == legal
    assert managed_world_path(legal).name == legal


def test_workspace_download_maps_containment_failure_to_bad_request(
    local_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = local_client
    assert client.post("/workspaces", json={"name": "安全世界"}).status_code == 201

    def reject_pack(_root: Path) -> bytes:
        raise PathSecurityError("simulated world symlink escape")

    monkeypatch.setattr(workspaces_router, "export_world_zip", reject_pack)
    response = client.get("/workspaces/安全世界/pack")
    assert response.status_code == 400


def test_rest_export_and_lorebook_guard_runs_before_any_export_writer(
    local_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, content_root = local_client
    writer_calls: list[str] = []
    export_root = content_root.resolve() / ".owcopilot" / "exports"
    guarded_targets = {export_root / "generic", export_root / "lorebook"}
    outside = content_root.parent / "outside-export"
    outside.mkdir()
    outside_resolved = outside.resolve()
    real_resolve = Path.resolve

    def resolve_as_external(self: Path, *args, **kwargs) -> Path:
        if self in guarded_targets:
            return outside_resolved
        return real_resolve(self, *args, **kwargs)

    def forbidden_engine_writer(*args, **kwargs):
        writer_calls.append("engine")
        raise AssertionError("engine writer ran before containment guard")

    def forbidden_lorebook_writer(*args, **kwargs):
        writer_calls.append("lorebook")
        raise AssertionError("lorebook writer ran before containment guard")

    # Simulate both final output directories being symlinks to the same external directory.
    # The real router containment helper must reject them before either writer is invoked.
    monkeypatch.setattr(Path, "resolve", resolve_as_external)
    monkeypatch.setattr(exports_router, "run_project_export_action", forbidden_engine_writer)
    monkeypatch.setattr(exports_router, "run_lorebook_export_action", forbidden_lorebook_writer)

    exported = client.post("/projects/demo/exports", json={"target_engine": "generic"})
    assert exported.status_code == 400, exported.text
    lorebook = client.get("/projects/demo/lorebook", params={"fmt": "md"})
    assert lorebook.status_code == 400, lorebook.text
    assert writer_calls == []


def test_project_runtime_parent_alias_is_rest_400_before_export_writers(
    local_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, content_root = local_client
    runtime_parent = content_root.resolve() / ".owcopilot"
    outside = content_root.parent / "external-runtime"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_bytes(b"unchanged")
    outside_resolved = outside.resolve()
    real_resolve = Path.resolve
    writer_calls: list[str] = []

    def resolve_as_external(self: Path, *args, **kwargs) -> Path:
        if self == runtime_parent:
            return outside_resolved
        return real_resolve(self, *args, **kwargs)

    def forbidden_engine_writer(*args, **kwargs):
        writer_calls.append("engine")
        raise AssertionError("engine writer ran after unsafe runtime parent")

    def forbidden_lorebook_writer(*args, **kwargs):
        writer_calls.append("lorebook")
        raise AssertionError("lorebook writer ran after unsafe runtime parent")

    monkeypatch.setattr(Path, "resolve", resolve_as_external)
    monkeypatch.setattr(exports_router, "run_project_export_action", forbidden_engine_writer)
    monkeypatch.setattr(exports_router, "run_lorebook_export_action", forbidden_lorebook_writer)

    engine = client.post("/projects/demo/exports", json={"target_engine": "generic"})
    lorebook = client.get("/projects/demo/lorebook", params={"fmt": "md"})

    for response in (engine, lorebook):
        assert response.status_code == 400, response.text
        assert "unsafe project runtime path" in response.text
    assert writer_calls == []
    assert sentinel.read_bytes() == b"unchanged"


def test_managed_world_path_rejects_a_real_external_symlink_when_supported(
    tmp_path: Path,
) -> None:
    worlds = tmp_path / "worlds"
    worlds.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = worlds / "linked-world"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlinks are unavailable to this test account")
    with pytest.raises(PathSecurityError):
        managed_world_path("linked-world", base=worlds)


def test_legal_world_create_uses_exact_public_identifier(tmp_path: Path) -> None:
    name = "盐汐 群岛.v2"
    created = create_managed_world(name, base=tmp_path / "worlds")
    assert created.name == name
    assert created.parent == (tmp_path / "worlds").resolve()
