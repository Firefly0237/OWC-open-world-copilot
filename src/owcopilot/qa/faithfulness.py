"""Opt-in, $0-skippable LLM-judge *faithfulness* (entailment) verifier.

This is the **separate, opt-in verifier** that ``qa/verify.py`` reserves room for in its module
docstring: *"If an entailment backend is ever added, it belongs in a separate, opt-in verifier — do
not quietly upgrade this function's promise."* It does **not** touch ``verify_qa_answer`` (which
remains a pure *citation-existence* check); it lives alongside it and is only consulted when a judge
is explicitly supplied.

**What it measures (RAGAS-style faithfulness).** Split the answer into atomic claims and, for each
claim, ask an LLM judge whether the *retrieved evidence* (the cited / retrieved hit text in the
context pack) actually **supports** that claim. ``faithfulness = supported_claims / total_claims``.
This catches the classic "entity is in canon but this *specific* fact is not in the evidence"
hallucination that the existence check, by design, lets through (e.g. asking for 铁卫军团的军歌歌词
and citing the faction's real description, which says nothing about a song).

**$0 / offline by default.** ``judge=None`` (or a judge that probes as unavailable — no API key /
offline) returns ``{"skipped": True, "reason": ...}`` and never raises, never calls a model. The
default acceptance run passes no judge, so this stays free and deterministic. Supplying a real
``LLMGateway`` (with a connected provider) opts in.

**Fail-closed parsing.** A claim whose judge reply cannot be parsed as the required JSON is counted
as **unsupported** (tagged ``parse_error``), never silently treated as supported — an unparseable
verdict must not inflate the faithfulness score.
"""

from __future__ import annotations

import re
from typing import Any

from ..llm.gateway import LLMGateway
from ..llm.jsonio import extract_json_object
from ..retrieval.models import ContextPack
from .models import QAAnswer

#: Router task label for the judge call, so telemetry / routing can target it explicitly.
FAITHFULNESS_JUDGE_TASK = "faithfulness_judge"

_JUDGE_SYSTEM = (
    "You are a strict factual-entailment judge for a game-lore QA system. "
    "You are given a QUESTION, ONE atomic CLAIM extracted from a candidate answer, and the "
    "EVIDENCE text that was retrieved from the canon knowledge base. Decide whether the EVIDENCE "
    "*actually supports* the CLAIM. "
    "supported=true ONLY when the evidence states or directly entails the claim. "
    "If the evidence merely mentions the same entity but does NOT contain the specific fact the "
    "claim asserts, that is supported=false (existence is not support). When in doubt, answer "
    "supported=false. "
    'Return ONLY one JSON object, no prose: {"supported": true|false, "reason": "<short reason>"}.'
)


def judge_qa_faithfulness(
    answer: QAAnswer,
    *,
    pack: ContextPack,
    judge: LLMGateway | None = None,
) -> dict[str, Any]:
    """Judge whether each claim in ``answer`` is entailed by the retrieved evidence in ``pack``.

    Mirrors the ``run_semantic_retrieval_benchmark(skip_if_no_semantic=...)`` skip contract:
    when no usable judge is available this returns ``{"skipped": True, "reason": ...}`` and never
    raises — so the $0/offline default is preserved.

    Args:
        answer: the QA answer to evaluate. A refused answer makes no factual claims, so it is
            vacuously faithful (``faithfulness=1.0``, no claims).
        pack: the retrieved context pack whose hit text is the evidence the claims must be
            supported by.
        judge: an :class:`LLMGateway` with a connected provider. ``None`` (or an offline/keyless
            judge) → skipped.

    Returns:
        Skipped:  ``{"skipped": True, "reason": str}``.
        Evaluated: ``{"skipped": False, "faithfulness": float, "claims": [...],
        "unsupported": [...]}`` where each entry in ``claims`` is
        ``{"claim": str, "supported": bool, "reason": str}`` and ``unsupported`` is the subset
        (including ``parse_error`` verdicts) that were not supported.
    """
    if not _judge_available(judge):
        return {
            "skipped": True,
            "reason": (
                "no LLM judge available for faithfulness entailment "
                "(pass a connected LLMGateway as judge=...; default offline/keyless runs skip "
                "this to keep the gate $0 and deterministic)"
            ),
        }
    assert judge is not None  # narrowed by _judge_available

    claims = _split_claims(answer)
    if not claims:
        # A refusal / empty answer asserts no facts → nothing to contradict the evidence.
        return {"skipped": False, "faithfulness": 1.0, "claims": [], "unsupported": []}

    evidence = _evidence_text(pack)
    results: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    for claim in claims:
        verdict = _judge_claim(judge, question=pack.query, claim=claim, evidence=evidence)
        results.append(verdict)
        if not verdict["supported"]:
            unsupported.append(verdict)

    supported = sum(1 for verdict in results if verdict["supported"])
    faithfulness = supported / len(results)
    return {
        "skipped": False,
        "faithfulness": faithfulness,
        "claims": results,
        "unsupported": unsupported,
    }


def _judge_available(judge: LLMGateway | None) -> bool:
    """A judge is usable only when it is supplied and has at least one registered provider.

    This intentionally does NOT inspect provider type: the offline/keyless decision is the
    caller's (the default acceptance run simply passes ``judge=None``). An empty-provider gateway
    cannot complete a call, so treat it as unavailable rather than letting it raise mid-run.
    """
    if judge is None:
        return False
    providers = getattr(judge, "providers", None)
    return bool(providers)


def _judge_claim(
    judge: LLMGateway, *, question: str, claim: str, evidence: str
) -> dict[str, Any]:
    """Ask the judge about one claim. Parse failures fail closed (unsupported + parse_error)."""
    user = (
        f"QUESTION:\n{question}\n\n"
        f"CLAIM:\n{claim}\n\n"
        f"EVIDENCE:\n{evidence if evidence else '(no evidence text retrieved)'}"
    )
    try:
        raw = judge.complete(task=FAITHFULNESS_JUDGE_TASK, system=_JUDGE_SYSTEM, user=user)
    except Exception as exc:  # noqa: BLE001 - a judge failure must not crash or pass silently
        return {
            "claim": claim,
            "supported": False,
            "reason": f"judge_error: {exc}",
            "parse_error": True,
        }
    try:
        payload = extract_json_object(raw)
    except ValueError:
        # Fail closed: an unparseable verdict is NOT a pass.
        return {
            "claim": claim,
            "supported": False,
            "reason": "parse_error: judge reply was not valid JSON",
            "parse_error": True,
        }
    supported = payload.get("supported")
    if not isinstance(supported, bool):
        # "supported" missing or not a real boolean → fail closed.
        return {
            "claim": claim,
            "supported": False,
            "reason": "parse_error: judge reply had no boolean 'supported' field",
            "parse_error": True,
        }
    reason = payload.get("reason")
    return {
        "claim": claim,
        "supported": supported,
        "reason": str(reason) if reason is not None else "",
    }


def _evidence_text(pack: ContextPack) -> str:
    """Concatenate the retrieved hit text — title + body — as the evidence corpus."""
    lines: list[str] = []
    for hit in pack.hits:
        parts = [part for part in (hit.title, hit.body) if part]
        line = " ".join(parts).strip()
        if line:
            lines.append(f"[{hit.ref}] {line}")
    return "\n".join(lines)


# Split on CJK and ASCII sentence terminators; keep it dependency-free (no NLTK / spaCy).
_SENTENCE_SPLIT = re.compile(r"[。！？；\n]+|(?<=[.!?])\s+")


def _split_claims(answer: QAAnswer) -> list[str]:
    """Split the answer text into atomic claims (one per sentence-ish span).

    Deliberately simple and offline: real RAGAS uses an LLM to decompose claims, but a
    sentence split is a fair, $0 approximation and keeps the verifier free of an extra model
    round-trip per answer. A refusal contributes no claims.
    """
    if answer.refused:
        return []
    text = (answer.answer or "").strip()
    if not text:
        return []
    claims = [segment.strip() for segment in _SENTENCE_SPLIT.split(text) if segment.strip()]
    return claims or [text]
