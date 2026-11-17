"""N0.2 controls for the untrusted HTTP extraction run -> submit hand-off."""

from __future__ import annotations

import pytest

from owcopilot.inspiration.models import Provenance
from owcopilot.service.intake_tokens import (
    IntakeTokenError,
    IntakeTokenSigner,
    canonical_draft_digest,
)

_SECRET = b"n02-intake-test-secret-material!"
_NONCE = "0123456789abcdef0123456789abcdef"
_TEXT = "沈青澜说道：灯不对。沈青澜与陆惊鸿前往枯叶林。"


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
