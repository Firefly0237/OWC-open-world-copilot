"""N0.2 controls for the untrusted HTTP extraction run -> submit hand-off."""

from __future__ import annotations

import copy
import json

import pytest
from fastapi.testclient import TestClient

from owcopilot.content.models import ContentBundle
from owcopilot.content.store import ContentStore
from owcopilot.inspiration.models import Provenance
from owcopilot.service.api import create_app
from owcopilot.service.intake_tokens import (
    IntakeTokenError,
    IntakeTokenSigner,
    canonical_draft_digest,
)

_SECRET = b"n02-intake-test-secret-material!"
_NONCE = "0123456789abcdef0123456789abcdef"
_TEXT = "沈青澜说道：灯不对。沈青澜与陆惊鸿前往枯叶林。"


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    roots: dict[str, str] = {}
    for project in ("demo", "other"):
        root = tmp_path / project
        ContentStore(root).save(ContentBundle())
        roots[project] = str(root).replace("\\", "/")
    monkeypatch.setenv("OWCOPILOT_PROJECTS_JSON", json.dumps(roots))
    monkeypatch.setenv("OWCOPILOT_PROFILE", "local")
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    with TestClient(create_app()) as test_client:
        yield test_client


def _run(client: TestClient, *, provenance: str = "own_ip") -> dict:
    response = client.post(
        "/projects/demo/extractions:run",
        json={
            "title": "第一章",
            "text": _TEXT,
            "glean_rounds": 0,
            "provenance": provenance,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_stateless_token_binds_project_draft_and_server_accepted_provenance() -> None:
    now = [1_000.0]
    signer = IntakeTokenSigner(
        _SECRET,
        ttl_seconds=10,
        clock=lambda: now[0],
        nonce_factory=lambda: _NONCE,
    )
    draft = {"z": [1, 2], "a": {"text": "灯不对"}}
    token = signer.issue(project="demo", draft=draft, provenance=Provenance.LICENSED)

    # A second process with the same secret can verify; JSON object ordering is canonicalised.
    verifier = IntakeTokenSigner(_SECRET, ttl_seconds=10, clock=lambda: now[0])
    assert (
        verifier.verify(token, project="demo", draft={"a": draft["a"], "z": [1, 2]})
        is Provenance.LICENSED
    )
    assert "licensed" not in token
    assert canonical_draft_digest(draft) not in token

    with pytest.raises(IntakeTokenError, match="invalid"):
        verifier.verify(token, project="other", draft=draft)
    with pytest.raises(IntakeTokenError, match="invalid"):
        verifier.verify(token, project="demo", draft={**draft, "z": [1, 3]})
    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
    with pytest.raises(IntakeTokenError, match="invalid"):
        verifier.verify(tampered, project="demo", draft=draft)

    now[0] = 1_010.0
    with pytest.raises(IntakeTokenError, match="expired"):
        verifier.verify(token, project="demo", draft=draft)


def test_token_issuer_refuses_third_party_canon_intake() -> None:
    signer = IntakeTokenSigner(_SECRET, nonce_factory=lambda: _NONCE)
    with pytest.raises(ValueError, match="第三方参考材料"):
        signer.issue(
            project="demo",
            draft={"id": "draft"},
            provenance=Provenance.THIRD_PARTY_REFERENCE,
        )


def test_http_submit_requires_token_and_forbids_client_provenance_override(
    client: TestClient,
) -> None:
    body = _run(client, provenance="licensed")
    missing = client.post(
        "/projects/demo/extractions:submit",
        json={"draft": body["draft"]},
    )
    assert missing.status_code == 422

    override = client.post(
        "/projects/demo/extractions:submit",
        json={
            "draft": body["draft"],
            "intake_token": body["intake_token"],
            "provenance": "own_ip",
        },
    )
    assert override.status_code == 422
    assert "extra_forbidden" in override.text


def test_http_token_rejects_cross_project_and_forged_human_approved_draft(
    client: TestClient,
) -> None:
    body = _run(client, provenance="licensed")
    cross_project = client.post(
        "/projects/other/extractions:submit",
        json={"draft": body["draft"], "intake_token": body["intake_token"]},
    )
    assert cross_project.status_code == 422
    assert "invalid extraction intake token" in cross_project.text

    forged = copy.deepcopy(body["draft"])
    entity = next(iter(forged["bundle"]["entities"].values()))
    entity["origin"] = "human"
    entity["review_status"] = "approved"
    tampered = client.post(
        "/projects/demo/extractions:submit",
        json={"draft": forged, "intake_token": body["intake_token"]},
    )
    assert tampered.status_code == 422
    assert "invalid extraction intake token" in tampered.text

    accepted = client.post(
        "/projects/demo/extractions:submit",
        json={"draft": body["draft"], "intake_token": body["intake_token"]},
    )
    assert accepted.status_code == 200, accepted.text


def test_http_submit_rejects_expired_token(client: TestClient) -> None:
    now = [2_000.0]
    client.app.state.intake_tokens = IntakeTokenSigner(
        _SECRET,
        ttl_seconds=1,
        clock=lambda: now[0],
        nonce_factory=lambda: _NONCE,
    )
    body = _run(client)
    now[0] = 2_002.0
    response = client.post(
        "/projects/demo/extractions:submit",
        json={"draft": body["draft"], "intake_token": body["intake_token"]},
    )
    assert response.status_code == 422
    assert "expired extraction intake token" in response.text


def test_local_same_app_run_then_submit_remains_available(client: TestClient) -> None:
    body = _run(client)
    submitted = client.post(
        "/projects/demo/extractions:submit",
        json={"draft": body["draft"], "intake_token": body["intake_token"]},
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["review_item_id"]


def test_local_intake_token_is_honestly_process_bound(client: TestClient) -> None:
    body = _run(client)
    # create_app represents a second process/restart: its fresh local-only signer must not accept
    # a capability issued by the first process. Team avoids this ambiguity by disabling both APIs.
    with TestClient(create_app()) as restarted:
        submitted = restarted.post(
            "/projects/demo/extractions:submit",
            json={"draft": body["draft"], "intake_token": body["intake_token"]},
        )
    assert submitted.status_code == 422
    assert "invalid extraction intake token" in submitted.text
