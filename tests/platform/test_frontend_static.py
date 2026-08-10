from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from owcopilot.service.dependencies import _SpaStaticFiles  # noqa: E402


def _client(tmp_path) -> TestClient:
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text('<div id="app"></div>', encoding="utf-8")
    (assets / "index-deadbeef.js").write_text(
        "const answer = 42;\n" * 80,
        encoding="utf-8",
    )
    (assets / "manual.js").write_text("console.log('manual')", encoding="utf-8")
    app = FastAPI()

    @app.get("/events")
    def events() -> StreamingResponse:
        return StreamingResponse(iter(["data: ready\n\n"]), media_type="text/event-stream")

    app.mount("/", _SpaStaticFiles(directory=str(dist), html=True), name="spa")
    return TestClient(app)


def test_spa_navigation_falls_back_but_fetch_and_missing_assets_stay_404(tmp_path) -> None:
    client = _client(tmp_path)

    navigation = client.get("/overview", headers={"Accept": "text/html"})
    fetch_typo = client.get("/unknown-api", headers={"Accept": "application/json"})
    missing_asset = client.get("/assets/missing.js", headers={"Accept": "*/*"})

    assert navigation.status_code == 200
    assert 'id="app"' in navigation.text
    assert navigation.headers["cache-control"] == "no-cache, must-revalidate"
    assert navigation.headers["x-frame-options"] == "DENY"
    assert navigation.headers["x-robots-tag"] == "noindex, nofollow, noarchive"
    assert "frame-ancestors 'none'" in navigation.headers["content-security-policy"]
    assert "camera=()" in navigation.headers["permissions-policy"]
    assert fetch_typo.status_code == 404
    assert missing_asset.status_code == 404


def test_hashed_assets_are_immutable_and_keep_javascript_mime(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.get("/assets/index-deadbeef.js")

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_static_path_normalization_handles_starlette_windows_paths(tmp_path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    static = _SpaStaticFiles(directory=str(dist), html=True)

    assert static._clean_path(r"assets\index-deadbeef.js") == "assets/index-deadbeef.js"


def test_static_compression_does_not_wrap_sse(tmp_path) -> None:
    client = _client(tmp_path)

    asset = client.get(
        "/assets/index-deadbeef.js",
        headers={"Accept-Encoding": "gzip"},
    )
    events = client.get("/events", headers={"Accept-Encoding": "gzip"})

    assert asset.status_code == 200
    assert asset.headers["content-encoding"] == "gzip"
    assert "Accept-Encoding" in asset.headers["vary"]
    assert "const answer = 42" in asset.text
    assert events.status_code == 200
    assert events.headers["content-type"].startswith("text/event-stream")
    assert "content-encoding" not in events.headers


def test_unversioned_assets_revalidate_instead_of_becoming_immutable(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.get("/assets/manual.js")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
