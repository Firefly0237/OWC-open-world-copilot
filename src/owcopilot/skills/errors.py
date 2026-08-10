# 技能层统一响应信封 + 通用错误码枚举（E-1）：把技能出口的失败从自由文本升级为机器可读信封
#   （status/error_code/message），供 agent 恢复层（E-2 系统提示）、观测压缩与控制台按码处置。
# D-9 落地后 classify_error 已改为按 SkillError 的类型子类（isinstance）判定，不再擦报错文本；
#   非技能异常仍按 stdlib 类型兜底归类（FileNotFound→NOT_FOUND，输入型→INVALID_ARG，余→INTERNAL）。
"""Unified skill-exit response envelope + a general error-code vocabulary.

The agent's skill surface has two exits:

* **success** — the skill returns its result payload dict; the loop serializes it with the
  canonical :func:`~owcopilot.skills.langchain_tools.dumps_observation` and carries it on a
  ``ToolMessage(status="success")``. That payload *is* the envelope's ``data`` and its observation
  string is a protocol invariant (byte-parity with the transcript serialization, pinned by test),
  so success is **not** re-wrapped — it already conforms.
* **failure** — historically an unstructured ``"Error: ..."`` string, from which a model could not
  reliably read *why* it failed. This module gives that exit a machine-readable envelope
  ``{"status": "error", "error_code": <ErrorCode>, "message": ...}`` so the recovery layer in the
  agent system prompt can pick its next move from the ``error_code`` alone.

The two exits are therefore *isomorphic at the envelope level*: every ``ToolMessage`` carries a
``status`` (``ToolMessage.status`` + an ``owc_status`` metadata mirror); a failure additionally
carries an ``error_code`` and a human ``message``; a success carries its ``data`` payload. The
serialization differs only because the success string must stay byte-stable while the failure
string must instead *show the model the code*.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from . import SkillConflictError, SkillError, SkillNotFoundError


class ErrorCode(str, Enum):
    """Machine-readable failure mode of a skill call — the vocabulary the recovery layer keys off.

    General on purpose (never a mirror of one skill's schema): each value names a *failure mode*,
    not a skill, so the set stays small and stable as skills come and go.
    """

    NOT_FOUND = "NOT_FOUND"  # a named target/tool/id does not exist or is out of scope
    INVALID_ARG = "INVALID_ARG"  # arguments missing / wrong type / wrong enum / wrong shape
    EMPTY_RESULT = "EMPTY_RESULT"  # ran but matched nothing — broaden the query or conclude
    CONFLICT = "CONFLICT"  # the action is not allowed in this state/policy (do not repeat it)
    RUN_CONTRACT_MISMATCH = "RUN_CONTRACT_MISMATCH"  # resume tried to change frozen limits/policy
    NO_PROGRESS = "NO_PROGRESS"  # exact repeat returned the same observation twice; change course
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"  # a cost/step budget is exhausted — stop and conclude now
    EXTERNAL_TIMEOUT = "EXTERNAL_TIMEOUT"  # downstream timed out before a known completion
    EXTERNAL_UNAVAILABLE = "EXTERNAL_UNAVAILABLE"  # dependency/transport is unavailable
    EXTERNAL_PROTOCOL = "EXTERNAL_PROTOCOL"  # schema/protocol/result contract is not trustworthy
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"  # dispatch may have happened; replay needs idempotency
    INTERNAL = "INTERNAL"  # an unclassified internal failure (the catch-all)


class ExternalSkillError(RuntimeError):
    """Sanitized failure raised by a checked external-tool boundary.

    ``retryable`` is a policy fact established at the boundary, not a suggestion inferred by the
    model. ``outcome_unknown`` is kept separate because a lost acknowledgement after dispatch is
    materially different from a connection failure before dispatch: replay is safe only when the
    tool contract is idempotent (or an idempotency key/reconciliation path exists).
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: ErrorCode,
        retryable: bool = False,
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = bool(retryable)
        self.outcome_unknown = bool(outcome_unknown)


def classify_error(exc: BaseException) -> ErrorCode:
    """Map an exception raised during skill dispatch to one :class:`ErrorCode`.

    Type-first (D-9): the :class:`~owcopilot.skills.SkillError` subtypes name the failure *mode* at
    the raise site, so this reads their type rather than scraping message text —
    :class:`~owcopilot.skills.SkillNotFoundError` → ``NOT_FOUND`` (unknown skill / out-of-scope /
    unknown ``obs_id``), :class:`~owcopilot.skills.SkillConflictError` → ``CONFLICT`` (WRITES_CANON
    ban / double-registration / out-of-run), and any remaining ``SkillError`` (the argument subtype
    :class:`~owcopilot.skills.SkillArgumentError`, or a bare ``SkillError``) → ``INVALID_ARG``.
    Non-skill exceptions still fall back by stdlib type: ``FileNotFoundError`` → ``NOT_FOUND``,
    input-shape errors (``KeyError`` / ``ValueError`` / ``TypeError``) → ``INVALID_ARG``, anything
    else → ``INTERNAL``.
    """
    if isinstance(exc, ExternalSkillError):
        return exc.error_code
    if isinstance(exc, SkillNotFoundError):
        return ErrorCode.NOT_FOUND
    if isinstance(exc, SkillConflictError):
        return ErrorCode.CONFLICT
    if isinstance(exc, SkillError):
        # SkillArgumentError (missing required / wrong type / enum) and any bare SkillError.
        return ErrorCode.INVALID_ARG
    if isinstance(exc, FileNotFoundError):
        # Both raise sites are genuine "not found": a missing content root, and a missing issue_id.
        return ErrorCode.NOT_FOUND
    if isinstance(exc, (KeyError, ValueError, TypeError)):
        # Bad input shape reaching a handler: an unknown change_type enum, a missing change spec
        # key, a path that escapes the export sandbox — all argument-level.
        return ErrorCode.INVALID_ARG
    return ErrorCode.INTERNAL


def error_envelope(
    error_code: ErrorCode,
    message: str,
    *,
    retryable: bool | None = None,
    outcome_unknown: bool | None = None,
) -> dict[str, Any]:
    """The canonical FAILURE envelope dict: ``{status, error_code, message}`` (no ``data``).

    The caller serializes it with the same canonical serializer as a success observation, so an
    error observation is now *also* canonical JSON rather than free text. ``message`` keeps the
    caller's human wording verbatim (including its ``Error:`` lead-in) — that lead-in is what marks
    an error round as a compaction checkpoint (``context_compressor`` protects ``\\bError\\s*:``
    turns from being summarized away), so preserving it keeps that behaviour intact.
    """
    envelope: dict[str, Any] = {
        "status": "error",
        "error_code": error_code.value,
        "message": message,
    }
    # Preserve existing envelope byte shape for internal/local errors. External boundaries carry
    # both booleans explicitly so the recovery policy never guesses replay safety from prose.
    if retryable is not None:
        envelope["retryable"] = bool(retryable)
    if outcome_unknown is not None:
        envelope["outcome_unknown"] = bool(outcome_unknown)
    return envelope
