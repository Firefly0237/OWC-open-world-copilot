# 结构化输出统一层：约束 → 校验 → 带错因的修复重试。把散落各处的 extract+validate+重试收敛成一处。
# 关键：修复循环只捕 ValueError 家族(JSON 解析+pydantic 校验);截断/传输错误(LLMGatewayError/
#   OutputTruncatedError,均非 ValueError)一律穿透不吞;重试用尽抛 StructuredOutputError,不静默降级。
"""Client-side structured-output layer: constrain → validate → repair-retry.

Our OpenAI-compatible provider sends ``response_format={"type": "json_object"}`` (json_object mode,
not provider-side ``json_schema`` strict decoding), so the schema guarantee must be enforced in
code. This is the canonical client-side pattern (OpenAI/Anthropic both say "your code still enforces
constraints through validation"; Instructor's reask loop is the de-facto implementation): run the
model, validate against the Pydantic model, and on a validation failure re-ask ONCE (bounded) with
the *specific* validation error appended, rather than fabricating a default. It underpins OWASP
LLM01 mitigation #2 (validate expected output formats deterministically).

Discipline: the repair loop catches only the ``ValueError`` family (JSON-parse + pydantic
``ValidationError``, which subclasses ``ValueError``). Truncation and transport failures surface
from the gateway as ``LLMGatewayError`` / ``OutputTruncatedError`` (``RuntimeError`` family) and are
deliberately NOT caught — they must propagate, never be swallowed into a repair burn. When the retry
budget is exhausted the failure is raised as :class:`StructuredOutputError`, never a silent default.
"""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from .gateway import LLMGateway
from .jsonio import extract_validated

M = TypeVar("M", bound=BaseModel)


#: Cap on how much of the offending raw reply is echoed back into the repair prompt.
_SNIPPET_CAP = 500

#: Short, GENERIC repair suffix — only {error}/{snippet} slots, nothing case-specific (kept generic
#: so it never overfits one call site). Named without SYSTEM/PROMPT/SHAPE so it is an internal
#: scaffold, not a registry-tracked case-specific model-facing prompt.
_REPAIR_INSTRUCTION = (
    "Your previous reply did not match the required schema.\n"
    "Validation errors: {error}\n"
    "Your previous output (truncated): {snippet}\n"
    "Reply with ONLY the corrected JSON matching the schema — no prose, no markdown fences."
)


class StructuredOutputError(ValueError):
    """Raised when structured output cannot be validated within the repair budget.

    Subclasses ``ValueError`` for consistency with :mod:`owcopilot.llm.jsonio`'s error contract. The
    message carries the model class name and the final validation-error text, so callers/tests can
    assert the concrete cause. Never raised for truncation/transport failures (those propagate)."""

    def __init__(
        self, *, model_cls: type[BaseModel], attempts: int, last_error: ValueError | None, raw: str
    ) -> None:
        self.model_cls = model_cls
        self.attempts = attempts
        self.last_error = last_error
        self.raw = raw
        super().__init__(
            f"{model_cls.__name__} structured output failed after {attempts} attempt(s): "
            f"{last_error}"
        )


def complete_structured(
    gateway: LLMGateway,
    *,
    task: str,
    system: str,
    user: str,
    model_cls: type[M],
    tier: str | None = None,
    max_repair_retries: int = 1,
) -> M:
    """Complete ``task`` and return a validated ``model_cls`` instance.

    On a schema-invalid reply, re-asks up to ``max_repair_retries`` times, each time appending the
    specific validation error + a capped snippet of the bad output. Raises
    :class:`StructuredOutputError` if the budget is exhausted (never returns a default). Truncation
    and transport errors from the gateway propagate untouched (the except is narrowed to
    ``ValueError`` precisely so they are not swallowed)."""
    retries = max(0, max_repair_retries)

    raw = gateway.complete(task=task, system=system, user=user, tier=tier)
    last_error: ValueError | None = None
    for attempt in range(retries + 1):
        try:
            return extract_validated(raw, model_cls)
        except ValueError as err:  # JSON-parse + pydantic ValidationError only; transport passes
            last_error = err
            if attempt == retries:
                break
            repair_user = f"{user}\n\n" + _REPAIR_INSTRUCTION.format(
                error=str(err), snippet=raw[:_SNIPPET_CAP]
            )
            raw = gateway.complete(task=task, system=system, user=repair_user, tier=tier)
    raise StructuredOutputError(
        model_cls=model_cls, attempts=retries + 1, last_error=last_error, raw=raw
    ) from last_error
