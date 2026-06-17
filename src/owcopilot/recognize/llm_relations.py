"""§8 — proactively shrink LLM hallucination *before* a human ever sees it.

The product's DNA is a deterministic gate around the model ("no source, no answer", post-verified).
We reuse it for relation recognition so the human is the *last* line of defence, not the only one.
A pluggable ``proposer`` (a real LLM, or a test double) suggests relations from free text; then
deterministic guards run, each rejection recorded with a reason:

1. **Closed world** — both endpoints must be ids we already extracted; the LLM may relate known
   entities, never invent one. (Self-loops dropped.)
2. **Evidence grounding** — every proposal must quote a verbatim span that actually occurs in the
   source text; no quote, or a quote not found → dropped. Mirrors "no source, no answer".
3. **Kind vocabulary** — the relation kind must be in the allowed vocabulary, when one is given.
4. **Confidence floor** — below-threshold proposals dropped (abstention preferred over guessing).
5. **De-duplication** — one (source, target, kind) survives.

The proposer stays default-OFF in the pipeline; even when on, every survivor is still marked
``method="llm"`` and carries its evidence into review. An optional ``critic`` adds a second pass.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .models import ProposedRelation, SourceRef

# The closed-world, evidence-required system prompt — the §8 discipline stated to the model itself,
# so it abstains by design; the deterministic guards below are the real enforcement.
_RELATION_SYSTEM = (
    "你是严格的关系抽取器。只能在【给定实体 id 列表】之间提出关系，"
    "**禁止发明任何新实体或新 id**。每条关系必须给出来自【原文】的**逐字证据片段**"
    "（evidence：原样照抄一小段能证明该关系的话，不得改写）。拿不准、原文没明说，就**不要提**。"
    '只输出 JSON 数组，元素形如 {"source":"id","target":"id","kind":"关系类型",'
    '"evidence":"原文片段","confidence":0.0到1.0}；不要输出 JSON 数组以外的任何内容。'
)

# proposer(text, known_ids) -> raw proposals, each ~ {source, target, kind, evidence, confidence}
RelationProposer = Callable[[str, list[str]], Sequence[Mapping[str, Any]]]
# critic(text, relation) -> True to keep, False to veto (a second, independent check)
RelationCritic = Callable[[str, ProposedRelation], bool]

_WS = re.compile(r"\s+")


def _collapse(text: str) -> str:
    return _WS.sub(" ", text).strip()


def evidence_grounded(text: str, span: str) -> bool:
    """True iff ``span`` is a non-empty verbatim quote of ``text`` (whitespace-normalized)."""
    span = _collapse(span)
    return bool(span) and span in _collapse(text)


def propose_relations_guarded(
    text: str,
    known_entity_ids: Sequence[str],
    *,
    proposer: RelationProposer,
    allowed_kinds: Sequence[str] | None = None,
    min_confidence: float = 0.5,
    critic: RelationCritic | None = None,
    source_file: str = "",
) -> tuple[list[ProposedRelation], list[str]]:
    """Run ``proposer`` then the §8 guards. Returns (kept proposals, human-readable drop reasons).

    The proposer is supplied by the caller and stays default-off in the pipeline."""
    known = {str(i) for i in known_entity_ids}
    kinds = {str(k) for k in allowed_kinds} if allowed_kinds is not None else None
    kept: list[ProposedRelation] = []
    dropped: list[str] = []
    seen: set[tuple[str, str, str]] = set()

    raw = proposer(text, sorted(known)) or []
    for item in raw:
        src = str(item.get("source", "")).strip()
        tgt = str(item.get("target", "")).strip()
        kind = str(item.get("kind", "")).strip()
        evidence = str(item.get("evidence", "")).strip()
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        tag = f"{src or '∅'}→{tgt or '∅'} [{kind or '∅'}]"

        if src not in known or tgt not in known:
            dropped.append(f"{tag}：端点不在已识别实体集合内（闭世界约束）")
            continue
        if src == tgt:
            dropped.append(f"{tag}：自指关系，丢弃")
            continue
        if kinds is not None and kind not in kinds:
            dropped.append(f"{tag}：关系类型不在受控词表内")
            continue
        if not evidence_grounded(text, evidence):
            dropped.append(f"{tag}：证据未在原文中逐字命中（无据不立）")
            continue
        if confidence < min_confidence:
            dropped.append(f"{tag}：置信 {confidence:.2f} < 阈值 {min_confidence:.2f}（宁可弃权）")
            continue
        key = (src, tgt, kind)
        if key in seen:
            dropped.append(f"{tag}：重复，已合并")
            continue

        relation = ProposedRelation(
            source=src,
            target=tgt,
            kind=kind,
            evidence=evidence,
            confidence=confidence,
            method="llm",
            source_ref=SourceRef(file=source_file, locator="llm"),
        )
        if critic is not None and not critic(text, relation):
            dropped.append(f"{tag}：批判复核否决")
            continue
        seen.add(key)
        kept.append(relation)

    return kept, dropped


def _parse_relations(raw: str) -> list[dict[str, Any]]:
    """Pull a JSON array of relation objects out of model text (tolerating prose / code fences)."""
    start = raw.find("[")
    end = raw.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        data = json.loads(raw[start : end + 1])
    except ValueError:
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def build_llm_relation_proposer(
    gateway: Any,
    *,
    task: str = "recognize_relations",
    allowed_kinds: Sequence[str] | None = None,
) -> RelationProposer:
    """Wrap an LLM gateway as a ``RelationProposer``. The model only *proposes*; the deterministic
    guards in ``propose_relations_guarded`` (closed-world / evidence / kind / confidence) decide."""

    def proposer(text: str, known_ids: list[str]) -> list[dict[str, Any]]:
        kinds = ("\n允许的关系类型：" + "、".join(allowed_kinds)) if allowed_kinds else ""
        user = "实体 id 列表：" + ", ".join(known_ids) + kinds + "\n\n原文：\n" + text
        return _parse_relations(gateway.complete(task=task, system=_RELATION_SYSTEM, user=user))

    return proposer
