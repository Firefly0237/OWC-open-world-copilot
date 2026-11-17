"""N2.1 reachability and browser-boundary tests for the team intake UI."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from owcopilot.service import api  # noqa: E402
from owcopilot.service.api import create_app  # noqa: E402

CONTAINER_ID = "33333333-3333-4333-8333-333333333333"
RUN_ID = "44444444-4444-4444-8444-444444444444"


class _Health:
    ready = True

    def as_dict(self) -> dict[str, object]:
        return {"ready": True, "schema_head": "n21_0001"}


class _Database:
    def health(self) -> _Health:
        return _Health()

    def close(self) -> None:
        return None


class _IntakeService:
    intake_policy = SimpleNamespace(max_upload_bytes=1024)

    def read_intake_run(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("unauthenticated request reached the intake service")


def _team_app(monkeypatch: pytest.MonkeyPatch, *, frontend_dist) -> object:
    monkeypatch.setenv("OWCOPILOT_PROFILE", "team")
    monkeypatch.setenv("OWCOPILOT_CORS_ORIGINS", "http://localhost:5173")
    monkeypatch.setattr(api, "_frontend_dist_dir", lambda: frontend_dist)
    return create_app(
        team_database=_Database(),
        team_repository=object(),
        team_intake_service=_IntakeService(),
    )


def test_team_profile_serves_built_spa_after_api_routes(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frontend_dist = tmp_path / "dist"
    frontend_dist.mkdir()
    assets = frontend_dist / "assets"
    assets.mkdir()
    (frontend_dist / "index.html").write_text(
        "<!doctype html><title>N2.1 team intake</title>", encoding="utf-8"
    )
    (assets / "index-deadbeef.js").write_text("const team = true;\n" * 80, encoding="utf-8")
    client = TestClient(_team_app(monkeypatch, frontend_dist=frontend_dist))

    root = client.get("/")
    history_route = client.get("/intake")
    canonical_route = client.get("/canonical")
    unmigrated_get = client.get("/settings/connection")
    health = client.get("/health")
    docs = client.get("/docs")
    asset = client.get("/assets/index-deadbeef.js", headers={"Accept-Encoding": "gzip"})

    assert root.status_code == 200
    assert "N2.1 team intake" in root.text
    assert history_route.status_code == 200
    assert "N2.1 team intake" in history_route.text
    assert history_route.headers["cache-control"] == "no-cache, must-revalidate"
    assert history_route.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in history_route.headers["content-security-policy"]
    assert canonical_route.status_code == 200
    assert "N2.1 team intake" in canonical_route.text
    assert unmigrated_get.status_code == 503
    assert unmigrated_get.json()["detail"]["code"] == "NOT_AVAILABLE"
    assert health.status_code == 200
    assert health.json()["profile"] == "team"
    assert docs.status_code == 200
    assert "text/html" in docs.headers["content-type"]
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert asset.headers["content-encoding"] == "gzip"


def test_team_profile_without_bundle_reports_api_only_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(_team_app(monkeypatch, frontend_dist=None))

    root = client.get("/")
    missing = client.get("/intake")

    assert root.status_code == 200
    assert root.json()["ui"] == "NOT_AVAILABLE"
    assert missing.status_code == 503
    assert missing.json()["detail"]["code"] == "NOT_AVAILABLE"


def test_team_cors_allows_only_configured_origin_and_never_bypasses_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(_team_app(monkeypatch, frontend_dist=None))
    path = f"/team/containers/{CONTAINER_ID}/intake-runs/{RUN_ID}"

    allowed = client.get(path, headers={"Origin": "http://localhost:5173"})
    denied_origin = client.get(path, headers={"Origin": "https://unconfigured.example"})
    preflight = client.options(
        path,
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        },
    )

    assert allowed.status_code == 401
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert denied_origin.status_code == 401
    assert "access-control-allow-origin" not in denied_origin.headers
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "authorization" in preflight.headers["access-control-allow-headers"].lower()


def test_team_profile_rejects_wildcard_cors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OWCOPILOT_PROFILE", "team")
    monkeypatch.setenv("OWCOPILOT_CORS_ORIGINS", "*")
    monkeypatch.setattr(api, "_frontend_dist_dir", lambda: None)

    with pytest.raises(ValueError, match="requires explicit"):
        create_app(
            team_database=_Database(),
            team_repository=object(),
            team_intake_service=_IntakeService(),
        )
