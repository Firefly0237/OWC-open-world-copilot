"""HTTP-boundary tests for the N2.1 team intake surface."""

from __future__ import annotations

from collections.abc import AsyncIterable, Mapping
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from owcopilot.contracts import OwnershipScope  # noqa: E402
from owcopilot.intake import DOCX_MIME, PDF_MIME, IntakeValidationError  # noqa: E402
from owcopilot.platform import Principal, Role, mint_token  # noqa: E402
from owcopilot.service.api import create_app  # noqa: E402
from owcopilot.teamdb import TeamIntakeConflict  # noqa: E402

TENANT_ID = "11111111-1111-4111-8111-111111111111"
SUBJECT_ID = "22222222-2222-4222-8222-222222222222"
CONTAINER_ID = "33333333-3333-4333-8333-333333333333"
RUN_ID = "44444444-4444-4444-8444-444444444444"
SEGMENT_ID = "55555555-5555-4555-8555-555555555555"


class _Health:
    ready = True

    def as_dict(self) -> dict[str, object]:
        return {"ready": True, "schema_head": "n21_0001"}


class _Database:
    def health(self) -> _Health:
        return _Health()

    def close(self) -> None:
        return None


class _Repository:
    pass


class _IntakeService:
    def __init__(self) -> None:
        self.intake_policy = SimpleNamespace(max_upload_bytes=1024)
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.error: Exception | None = None
        self.use_allowed = False
        self.create_result: Mapping[str, object] = {
            "run_id": RUN_ID,
            "status": "AWAITING_HUMAN",
        }
        self.read_result: Mapping[str, object] | None = None

    async def create_intake_run_async(
        self,
        scope: OwnershipScope,
        *,
        actor_subject: str,
        original_filename: str,
        mime_type: str,
        chunks: AsyncIterable[bytes],
    ) -> Mapping[str, object]:
        data = b"".join([chunk async for chunk in chunks])
        values: dict[str, object] = {
            "scope": scope,
            "actor_subject": actor_subject,
            "original_filename": original_filename,
            "mime_type": mime_type,
            "content": data,
        }
        self.calls.append(("create", values))
        if self.error is not None:
            raise self.error
        return self.create_result

    async def create_replacement_intake_run_async(
        self,
        scope: OwnershipScope,
        *,
        artifact_id: str,
        actor_subject: str,
        original_filename: str,
        mime_type: str,
        chunks: AsyncIterable[bytes],
    ) -> Mapping[str, object]:
        data = b"".join([chunk async for chunk in chunks])
        values: dict[str, object] = {
            "scope": scope,
            "artifact_id": artifact_id,
            "actor_subject": actor_subject,
            "original_filename": original_filename,
            "mime_type": mime_type,
            "content": data,
        }
        self.calls.append(("replace", values))
        if self.error is not None:
            raise self.error
        return {"run_id": RUN_ID, "status": "AWAITING_HUMAN"}

    def read_intake_run(self, scope: OwnershipScope, **values: object) -> Mapping[str, object]:
        self.calls.append(("read", {"scope": scope, **values}))
        if self.error is not None:
            raise self.error
        return self.read_result or {
            "run_id": values["run_id"],
            "status": "AWAITING_HUMAN",
        }

    def declare_rights(self, scope: OwnershipScope, **values: object) -> Mapping[str, object]:
        self.calls.append(("rights", {"scope": scope, **values}))
        if self.error is not None:
            raise self.error
        return {"run_id": values["run_id"], "allowed_uses": values["allowed_uses"]}

    def submit_human_decisions(
        self, scope: OwnershipScope, **values: object
    ) -> Mapping[str, object]:
        self.calls.append(("submit", {"scope": scope, **values}))
        if self.error is not None:
            raise self.error
        return {"run_id": values["run_id"], "submitted": True}

    def create_lineage_anchor(
        self, scope: OwnershipScope, **values: object
    ) -> Mapping[str, object]:
        self.calls.append(("lineage", {"scope": scope, **values}))
        if self.error is not None:
            raise self.error
        return {"run_id": values["run_id"], "lineage": [values]}

    def logical_delete_source_artifact(
        self, scope: OwnershipScope, **values: object
    ) -> Mapping[str, object]:
        self.calls.append(("delete", {"scope": scope, **values}))
        if self.error is not None:
            raise self.error
        return {"run_id": values["run_id"], "logical_deleted": True}

    def is_use_allowed(self, scope: OwnershipScope, **values: object) -> bool:
        self.calls.append(("use", {"scope": scope, **values}))
        if self.error is not None:
            raise self.error
        return self.use_allowed


def _token() -> str:
    return mint_token(
        Principal(user_id=SUBJECT_ID, tenant_id=TENANT_ID, role=Role.OWNER),
        secret="n21-team-secret",
    )


def _client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, _IntakeService]:
    monkeypatch.setenv("OWCOPILOT_PROFILE", "team")
    monkeypatch.setenv("OWCOPILOT_JWT_SECRET", "n21-team-secret")
    service = _IntakeService()
    app = create_app(
        team_database=_Database(),
        team_repository=_Repository(),
        team_intake_service=service,
    )
    return TestClient(app), service


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token()}"}


def test_raw_upload_passes_only_server_identity_and_exact_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, service = _client(monkeypatch)
    data = b"synthetic-ooxml"
    response = client.post(
        f"/team/containers/{CONTAINER_ID}/intake-runs?filename=source.docx",
        content=data,
        headers={**_headers(), "Content-Type": DOCX_MIME},
    )
    assert response.status_code == 201, response.text
    call = service.calls[0][1]
    assert call["scope"] == OwnershipScope(tenant_id=TENANT_ID, container_id=CONTAINER_ID)
    assert call["actor_subject"] == SUBJECT_ID
    assert call["content"] == data
    assert "artifact_id" not in call
    assert "created_by" not in call


def test_pdf_ocr_handoff_state_and_code_cross_the_rest_contract_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, service = _client(monkeypatch)
    service.create_result = {
        "run_id": RUN_ID,
        "status": "OCR_REQUIRED",
        "processing_code": "PDF_OCR_REQUIRED",
        "artifact": {"immutable_bytes_retained": True},
        "segments": [],
    }

    response = client.post(
        f"/team/containers/{CONTAINER_ID}/intake-runs?filename=scan.pdf",
        content=b"%PDF-1.4\nfixture",
        headers={**_headers(), "Content-Type": PDF_MIME},
    )

    assert response.status_code == 201
    assert response.json() == service.create_result


def test_replacement_upload_uses_existing_artifact_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, service = _client(monkeypatch)
    response = client.post(
        f"/team/containers/{CONTAINER_ID}/intake-runs"
        f"?filename=replacement.docx&artifact_id={SEGMENT_ID}",
        content=b"replacement-ooxml",
        headers={**_headers(), "Content-Type": DOCX_MIME},
    )

    assert response.status_code == 201, response.text
    assert service.calls[0][0] == "replace"
    assert service.calls[0][1]["artifact_id"] == SEGMENT_ID


@pytest.mark.parametrize("forged", ["rights", "authority", "approved", "declared_by"])
def test_forged_governance_fields_never_reach_service(
    monkeypatch: pytest.MonkeyPatch,
    forged: str,
) -> None:
    client, service = _client(monkeypatch)
    response = client.post(
        f"/team/containers/{CONTAINER_ID}/intake-runs/{RUN_ID}/rights-declarations",
        headers=_headers(),
        json={
            "rights_provenance": "OWNED",
            "declaration_text": "人工声明",
            "allowed_uses": {},
            forged: True,
        },
    )
    assert response.status_code == 422
    assert service.calls == []


def test_submit_dto_accepts_only_human_decision_differences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, service = _client(monkeypatch)
    accepted = client.post(
        f"/team/containers/{CONTAINER_ID}/intake-runs/{RUN_ID}:submit",
        headers=_headers(),
        json={
            "decisions": [
                {
                    "segment_id": SEGMENT_ID,
                    "decision": "CONFIRM",
                    "content_role": "REFERENCE",
                    "rationale": "人工确认",
                }
            ]
        },
    )
    forged = client.post(
        f"/team/containers/{CONTAINER_ID}/intake-runs/{RUN_ID}:submit",
        headers=_headers(),
        json={
            "decisions": [],
            "artifact_version_id": SEGMENT_ID,
            "approved": True,
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert forged.status_code == 422
    assert [name for name, _ in service.calls] == ["submit"]


def test_upload_limit_and_domain_errors_have_stable_http_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, service = _client(monkeypatch)
    too_large = client.post(
        f"/team/containers/{CONTAINER_ID}/intake-runs?filename=source.docx",
        content=b"x" * 1025,
        headers={**_headers(), "Content-Type": DOCX_MIME},
    )
    assert too_large.status_code == 413
    assert service.calls == []

    service.error = IntakeValidationError(
        "PACKAGE_KIND_MISMATCH",
        "OOXML package kind differs",
        status_code=415,
    )
    mismatch = client.post(
        f"/team/containers/{CONTAINER_ID}/intake-runs?filename=source.docx",
        content=b"small",
        headers={**_headers(), "Content-Type": DOCX_MIME},
    )
    assert mismatch.status_code == 415
    assert mismatch.json()["detail"]["code"] == "PACKAGE_KIND_MISMATCH"


def test_repository_intake_conflicts_map_to_http_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, service = _client(monkeypatch)
    service.error = TeamIntakeConflict("run state is stale")

    response = client.get(
        f"/team/containers/{CONTAINER_ID}/intake-runs/{RUN_ID}",
        headers=_headers(),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "INTAKE_CONFLICT"


def test_team_intake_segments_are_bounded_and_stale_cursor_recovers_from_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, service = _client(monkeypatch)
    segments = [
        {
            "segment_id": f"55555555-5555-4555-8555-55555555555{index}",
            "ordinal": index,
            "text": f"segment {index}",
        }
        for index in range(3)
    ]
    service.read_result = {
        "run_id": RUN_ID,
        "status": "AWAITING_HUMAN",
        "segments": segments,
        "triage_verdicts": [
            {"segment_id": segment["segment_id"], "content_role": "REFERENCE"}
            for segment in segments
        ],
        "human_decisions": [],
        "lineage": [],
    }
    url = f"/team/containers/{CONTAINER_ID}/intake-runs/{RUN_ID}?limit=2"

    first = client.get(url, headers=_headers())
    assert first.status_code == 200, first.text
    body = first.json()
    assert len(body["segments"]) == 2
    assert len(body["triage_verdicts"]) == 2
    assert body["segment_pagination"]["total"] == 3
    cursor = body["segment_pagination"]["next_cursor"]
    assert cursor

    second = client.get(f"{url}&cursor={cursor}", headers=_headers())
    assert second.status_code == 200, second.text
    assert [item["ordinal"] for item in second.json()["segments"]] == [2]

    service.read_result = {
        **service.read_result,
        "segments": [
            *segments,
            {"segment_id": "66666666-6666-4666-8666-666666666666", "ordinal": 3, "text": "new"},
        ],
    }
    stale = client.get(f"{url}&cursor={cursor}", headers=_headers())
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "STALE_CURSOR"


def test_team_intake_rejects_unbounded_page_size_before_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, service = _client(monkeypatch)
    response = client.get(
        f"/team/containers/{CONTAINER_ID}/intake-runs/{RUN_ID}?limit=101",
        headers=_headers(),
    )

    assert response.status_code == 422
    assert service.calls == []


def test_lineage_delete_and_drafting_authorization_reach_governance_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, service = _client(monkeypatch)
    lineage = client.post(
        f"/team/containers/{CONTAINER_ID}/intake-runs/{RUN_ID}/lineage-anchors",
        headers=_headers(),
        json={
            "segment_id": SEGMENT_ID,
            "lineage_kind": "INSPIRATION_LINEAGE",
            "target_ref": "draft:quest-1",
        },
    )
    denied = client.post(
        f"/team/containers/{CONTAINER_ID}/intake-runs/{RUN_ID}:authorize-drafting",
        headers=_headers(),
    )
    service.use_allowed = True
    allowed = client.post(
        f"/team/containers/{CONTAINER_ID}/intake-runs/{RUN_ID}:authorize-drafting",
        headers=_headers(),
    )
    deleted = client.post(
        f"/team/containers/{CONTAINER_ID}/intake-runs/{RUN_ID}:logical-delete",
        headers=_headers(),
    )

    assert lineage.status_code == 201, lineage.text
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "DRAFTING_NOT_AUTHORIZED"
    assert allowed.status_code == 200
    assert allowed.json() == {"run_id": RUN_ID, "use_kind": "DRAFTING", "allowed": True}
    assert deleted.status_code == 200
    assert deleted.json()["logical_deleted"] is True
    assert [name for name, _ in service.calls] == ["lineage", "use", "use", "delete"]


def test_team_intake_is_bearer_only(monkeypatch: pytest.MonkeyPatch) -> None:
    client, service = _client(monkeypatch)
    response = client.get(
        f"/team/containers/{CONTAINER_ID}/intake-runs/{RUN_ID}",
        headers={"X-API-Key": "must-not-authorize-team"},
    )
    assert response.status_code == 401
    assert service.calls == []
