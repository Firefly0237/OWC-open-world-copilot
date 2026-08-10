"""Short-lived service attestations for the HTTP extraction run -> submit boundary.

The token is deliberately stateless: it carries only a version, expiry, random nonce and MAC.
The MAC binds the project, the exact canonical draft digest and the provenance accepted by the
service.  Clients must treat the token as opaque.  This is an M0 stopgap, not an extraction-run
ledger and not a rights-verification system.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable, Mapping
from typing import Any

from ..inspiration.models import Provenance, ensure_canon_intake_allowed

_VERSION = "v1"
_DEFAULT_TTL_SECONDS = 15 * 60
_MAX_TOKEN_LENGTH = 256
_NONCE_BYTES = 16
_ALLOWED_PROVENANCES = (Provenance.OWN_IP, Provenance.LICENSED)


class IntakeTokenError(ValueError):
    """The attestation is malformed, expired or does not bind this submission."""


def canonical_draft_digest(draft: Mapping[str, Any]) -> str:
    """Hash the exact JSON value while making object-key order irrelevant."""
    try:
        payload = json.dumps(
            draft,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise IntakeTokenError("extraction draft is not canonical JSON") from exc
    return hashlib.sha256(payload).hexdigest()


class IntakeTokenSigner:
    """Issue and verify opaque, stateless extraction intake attestations."""

    def __init__(
        self,
        secret: bytes,
        *,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
        nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        if len(secret) < 32:
            raise ValueError("intake token secret must contain at least 32 bytes")
        if ttl_seconds < 1:
            raise ValueError("intake token ttl must be positive")
        self._secret = bytes(secret)
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._nonce_factory = nonce_factory or (lambda: secrets.token_hex(_NONCE_BYTES))

    def issue(
        self,
        *,
        project: str,
        draft: Mapping[str, Any],
        provenance: str | Provenance,
    ) -> str:
        accepted = ensure_canon_intake_allowed(provenance)
        digest = canonical_draft_digest(draft)
        expires_at = int(self._clock()) + self._ttl_seconds
        nonce = self._nonce_factory()
        self._validate_nonce(nonce)
        mac = self._mac(
            project=project,
            digest=digest,
            provenance=accepted,
            expires_at=expires_at,
            nonce=nonce,
        )
        return f"{_VERSION}.{expires_at}.{nonce}.{mac}"

    def verify(
        self,
        token: str,
        *,
        project: str,
        draft: Mapping[str, Any],
    ) -> Provenance:
        if not token or len(token) > _MAX_TOKEN_LENGTH:
            raise IntakeTokenError("invalid extraction intake token")
        parts = token.split(".")
        if len(parts) != 4 or parts[0] != _VERSION:
            raise IntakeTokenError("invalid extraction intake token")
        _, expires_text, nonce, supplied_mac = parts
        try:
            expires_at = int(expires_text)
            self._validate_nonce(nonce)
            if len(supplied_mac) != hashlib.sha256().digest_size * 2:
                raise ValueError
            bytes.fromhex(supplied_mac)
        except ValueError as exc:
            raise IntakeTokenError("invalid extraction intake token") from exc
        digest = canonical_draft_digest(draft)
        matched: Provenance | None = None
        for provenance in _ALLOWED_PROVENANCES:
            expected = self._mac(
                project=project,
                digest=digest,
                provenance=provenance,
                expires_at=expires_at,
                nonce=nonce,
            )
            if hmac.compare_digest(supplied_mac, expected):
                matched = provenance
        if matched is None:
            raise IntakeTokenError("invalid extraction intake token")
        # Only an authentic token earns the more specific expiry result.  Structurally valid
        # attacker input with an old timestamp must not become a token-validity oracle.
        if int(self._clock()) >= expires_at:
            raise IntakeTokenError("expired extraction intake token")
        return matched

    def _mac(
        self,
        *,
        project: str,
        digest: str,
        provenance: Provenance,
        expires_at: int,
        nonce: str,
    ) -> str:
        message = json.dumps(
            [_VERSION, project, digest, provenance.value, expires_at, nonce],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()

    @staticmethod
    def _validate_nonce(nonce: str) -> None:
        if len(nonce) != _NONCE_BYTES * 2:
            raise ValueError("invalid nonce")
        bytes.fromhex(nonce)
