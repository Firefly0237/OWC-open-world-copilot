"""Credential admission must match the documented provider configuration."""

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from owcopilot.service import api


@pytest.mark.parametrize("key_name", ["OWCOPILOT_OPENAI_API_KEY", "OPENAI_API_KEY"])
def test_local_admission_accepts_current_and_legacy_credentials(monkeypatch, key_name):
    monkeypatch.setattr(api, "load_dotenv", lambda: None)
    monkeypatch.delenv("OWCOPILOT_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv(key_name, "controlled-test-key")
    request = Request({"type": "http", "client": ("127.0.0.1", 1234)})
    api._require_real_allowed(request)


def test_missing_credentials_remain_unavailable(monkeypatch):
    monkeypatch.setattr(api, "load_dotenv", lambda: None)
    monkeypatch.delenv("OWCOPILOT_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    request = Request({"type": "http", "client": ("127.0.0.1", 1234)})
    with pytest.raises(HTTPException) as caught:
        api._require_real_allowed(request)
    assert caught.value.status_code == 503


def test_provider_key_does_not_authorize_remote_access(monkeypatch):
    monkeypatch.setenv("OWCOPILOT_OPENAI_API_KEY", "controlled-test-key")
    monkeypatch.delenv("OWCOPILOT_API_KEY", raising=False)
    request = Request({"type": "http", "client": ("203.0.113.7", 1234)})
    with pytest.raises(HTTPException) as caught:
        api._require_real_allowed(request)
    assert caught.value.status_code == 403
