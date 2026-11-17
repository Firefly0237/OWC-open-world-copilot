"""M0 capability firewall for explicit local/team deployment profiles."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from owcopilot.audit.runner import AuditRunner  # noqa: E402
from owcopilot.service import api  # noqa: E402
from owcopilot.service.api import create_app  # noqa: E402
from owcopilot.service.routers import settings as settings_router  # noqa: E402


@dataclass(frozen=True)
class _TeamHealth:
    ready: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "database_available": self.ready,
            "schema_head": "n12_0002",
            "expected_schema_head": "n12_0002",
            "detail": "ready" if self.ready else "database unavailable",
        }


class _TeamDatabaseStub:
    def health(self) -> _TeamHealth:
        return _TeamHealth()

    def close(self) -> None:
        return None


class _TeamRepositorySentinel:
    def __getattr__(self, name: str):
        raise AssertionError(f"unmigrated team route touched repository.{name}")


def _team_app():
    return create_app(
        team_database=_TeamDatabaseStub(),
        team_repository=_TeamRepositorySentinel(),
    )


def _assert_not_available(response, capability: str) -> None:
    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "NOT_AVAILABLE"
    assert detail["profile"] == "team"
    assert detail["capability"] == capability


def test_profile_defaults_to_local_and_explicit_local_preserves_state(monkeypatch) -> None:
    monkeypatch.delenv("OWCOPILOT_PROFILE", raising=False)
    default_app = create_app()
    assert default_app.state.deployment_profile == "local"
    assert hasattr(default_app.state, "jobs_manager")
    assert TestClient(default_app).get("/health").json()["profile"] == "local"
    monkeypatch.setenv("OWCOPILOT_PROFILE", "local")
    explicit_app = create_app()
    assert explicit_app.state.deployment_profile == "local"
    assert hasattr(explicit_app.state, "jobs_manager")
    assert TestClient(explicit_app).get("/health").json()["profile"] == "local"
    paths = explicit_app.openapi()["paths"]
    assert "/workspaces" in paths and "/settings/connection" in paths
    assert "/projects/{project}/creative/v2/tasks" in paths


@pytest.mark.parametrize("invalid", ["", "TEAM", "production", " local "])
def test_invalid_profile_fails_app_startup(monkeypatch, invalid: str) -> None:
    monkeypatch.setenv("OWCOPILOT_PROFILE", invalid)
    with pytest.raises(ValueError, match="OWCOPILOT_PROFILE must be one of local\\|team"):
        create_app()


def test_team_does_not_construct_job_manager_or_local_issue_dict(monkeypatch) -> None:
    monkeypatch.setenv("OWCOPILOT_PROFILE", "team")

    def forbidden_manager():
        raise AssertionError("team profile must not instantiate JobManager")

    monkeypatch.setattr(api, "JobManager", forbidden_manager)
    app = _team_app()
    assert app.state.deployment_profile == "team"
    assert not hasattr(app.state, "jobs_manager")
    assert not hasattr(app.state, "v2_issues")
    assert TestClient(app).get("/health").json()["profile"] == "team"


def test_explicit_local_profile_authorizes_settings_without_loopback_inference(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OWCOPILOT_PROFILE", "local")
    monkeypatch.delenv("OWCOPILOT_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OWCOPILOT_OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setattr(api, "_is_loopback", lambda _request: False)
    client = TestClient(create_app())

    updated = client.post(
        "/settings/connection",
        json={"api_key": "local-key", "base_url": "https://local-profile.example"},
    )

    assert updated.status_code == 200, updated.text
    assert updated.json()["configured"] is True
    assert os.environ["OPENAI_API_KEY"] == "local-key"
    assert os.environ["OPENAI_BASE_URL"] == "https://local-profile.example"
    assert os.environ["OWCOPILOT_OPENAI_API_KEY"] == "local-key"
    assert os.environ["OWCOPILOT_OPENAI_BASE_URL"] == "https://local-profile.example"
    assert client.get("/settings/connection").json()["base_url"] == "https://local-profile.example"


def test_team_job_create_get_and_sse_fail_before_action_or_thread(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OWCOPILOT_PROFILE", "team")
    monkeypatch.setenv("OWCOPILOT_PROJECTS_JSON", "{}")
    touched: list[str] = []

    def forbidden_submission(*_args, **_kwargs):
        touched.append("job_submission")
        raise AssertionError("team job submission must not run")

    monkeypatch.setattr(api.JobManager, "submit", forbidden_submission)
    app = _team_app()
    client = TestClient(app)
    job_threads_before = {
        thread.name for thread in threading.enumerate() if thread.name.startswith("owjob-")
    }

    created = client.post(
        "/projects/unregistered/jobs",
        json={"kind": "agent_diagnose", "params": {"goal": "audit"}},
    )
    status = client.get("/jobs/secret-from-another-process")
    events = client.get("/jobs/secret-from-another-process/events")

    for response in (created, status, events):
        _assert_not_available(
            response,
            f"team_unmigrated_route:{response.request.url.path}",
        )
    assert touched == []
    assert {
        thread.name for thread in threading.enumerate() if thread.name.startswith("owjob-")
    } == job_threads_before
    assert not (tmp_path / ".owcopilot" / "checkpoints.sqlite").exists()


def test_team_settings_posts_are_blocked_on_loopback_and_leave_environment_unchanged(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OWCOPILOT_PROFILE", "team")
    monkeypatch.setenv("OPENAI_API_KEY", "old-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://old.example")
    monkeypatch.setenv("OWCOPILOT_OPENAI_API_KEY", "old-prefixed-key")
    monkeypatch.setenv("OWCOPILOT_OPENAI_BASE_URL", "https://old-prefixed.example")

    def forbidden_probe(*_args, **_kwargs):
        raise AssertionError("team connection probe must not call provider")

    monkeypatch.setattr(settings_router, "probe_llm_connection_action", forbidden_probe)
    client = TestClient(_team_app())
    headers = {"Authorization": "Bearer arbitrary-role"}

    updated = client.post(
        "/settings/connection",
        json={"api_key": "new-key", "base_url": "https://new.example"},
        headers=headers,
    )
    probed = client.post(
        "/settings/connection:probe",
        json={
            "api_key": "new-key",
            "base_url": "https://new.example",
            "model": "test-model",
        },
        headers=headers,
    )

    _assert_not_available(updated, "team_unmigrated_route:/settings/connection")
    _assert_not_available(probed, "team_unmigrated_route:/settings/connection:probe")
    assert os.environ["OPENAI_API_KEY"] == "old-key"
    assert os.environ["OPENAI_BASE_URL"] == "https://old.example"
    assert os.environ["OWCOPILOT_OPENAI_API_KEY"] == "old-prefixed-key"
    assert os.environ["OWCOPILOT_OPENAI_BASE_URL"] == "https://old-prefixed.example"


def test_team_inline_audit_routes_are_disabled_before_any_compute_or_persistence(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OWCOPILOT_PROFILE", "team")
    calls = 0
    real_run = AuditRunner.run

    def counted_run(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return real_run(self, *args, **kwargs)

    monkeypatch.setattr(AuditRunner, "run", counted_run)
    app = _team_app()
    client = TestClient(app)

    persisted = client.post("/projects/inline/audits", json={"content": {}, "persist": True})
    computed = client.post("/projects/inline/audits", json={"content": {}, "persist": False})
    _assert_not_available(persisted, "team_unmigrated_route:/projects/inline/audits")
    _assert_not_available(computed, "team_unmigrated_route:/projects/inline/audits")
    assert calls == 0
    assert not hasattr(app.state, "v2_issues")

    missing = client.get("/projects/inline/issues")
    _assert_not_available(missing, "team_unmigrated_route:/projects/inline/issues")
