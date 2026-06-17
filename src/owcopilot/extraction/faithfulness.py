"""Source-faithfulness checks for extraction drafts (RAGAS/DeepEval-style, two tiers).

The original check only asked "does this *name* appear in the source?". That passes any RELATION
or ATTRIBUTE the model invents between two real entities — name two characters that both appear in
the manuscript, then claim they are siblings the text never mentioned, and a name-only check waves
it through. This module raises faithfulness to the *claim* level:

* **deterministic** ($0, always on): a name must appear in the source; a relation's two endpoints
  must *co-occur* within a proximity window — if the manuscript never mentions A and B near each
  other, the link is almost certainly the model's invention, not something it read.
* **LLM entailment** (opt-in, real mode): each structured claim (relation / attribute) is NLI-judged
  against the source — supported or not. Our content is *already* atomic structured claims, so we
  skip RAGAS's lossy LLM claim-decomposition step (which itself hallucinates) and verify directly.
"""

from __future__ import annotations

from ..content.models import ContentBundle
from ..content.relation_kinds import relation_kind_catalog
from ..llm.gateway import LLMGateway
from ..llm.jsonio import extract_json_object
from .models import UnsupportedItem

# endpoints within ~a paragraph of each other count as co-occurring (a necessary, not sufficient,
# condition for a real relation — the LLM tier judges whether the link is actually entailed).
_PROXIMITY_CHARS = 240
_JUDGE_MAX_CLAIMS = 40
_JUDGE_SOURCE_BUDGET = 6000


def _kind_labels() -> dict[str, str]:
    return {kind.id: kind.label for kind in relation_kind_catalog()}


def _occurrences(name: str, haystack_lower: str) -> list[int]:
    needle = name.strip().lower()
    if not needle:
        return []
    out: list[int] = []
    i = haystack_lower.find(needle)
    while i >= 0:
        out.append(i)
        i = haystack_lower.find(needle, i + 1)
    return out


def _co_occur(a: str, b: str, haystack_lower: str, window: int = _PROXIMITY_CHARS) -> bool:
    a_occ = _occurrences(a, haystack_lower)
    b_occ = _occurrences(b, haystack_lower)
    if not a_occ or not b_occ:
        return False
    return any(abs(i - j) <= window for i in a_occ for j in b_occ)


def check_deterministic(bundle: ContentBundle, source_text: str) -> list[UnsupportedItem]:
    """Tier 1 ($0): names must appear; relation endpoints must co-occur in the source."""
    if not source_text.strip():
        return []
    hay = source_text.lower()
    labels = _kind_labels()
    names = {eid: entity.name for eid, entity in bundle.entities.items()}
    items: list[UnsupportedItem] = []

    def grounded(*candidates: str) -> bool:
        return any(c.strip() and c.strip().lower() in hay for c in candidates)

    for entity in bundle.entities.values():
        if not grounded(entity.name, *entity.aliases):
            entity.metadata["unsupported_in_source"] = True
            items.append(
                UnsupportedItem(
                    ref=f"entity:{entity.id}",
                    name=entity.name,
                    kind=entity.type.value,
                    reason="name_not_in_source",
                    detail=entity.name,
                )
            )
    for term in bundle.terms.values():
        if not grounded(term.canonical, *term.aliases):
            items.append(
                UnsupportedItem(
                    ref=f"term:{term.id}",
                    name=term.canonical,
                    kind="term",
                    reason="name_not_in_source",
                    detail=term.canonical,
                )
            )
    # relations: both endpoints named in the source but never near each other = invented link.
    # (If an endpoint name is missing entirely, the entity flag above already covers it.)
    for rel in bundle.relations:
        a = names.get(rel.source)
        b = names.get(rel.target)
        if not a or not b or not grounded(a) or not grounded(b):
            continue
        if not _co_occur(a, b, hay):
            label = labels.get(rel.kind, rel.kind)
            items.append(
                UnsupportedItem(
                    ref=f"relation:{rel.source}|{rel.kind}|{rel.target}",
                    name=f"{a} → {b}",
                    kind="relation",
                    reason="relation_not_in_source",
                    detail=f"「{a}」与「{b}」：{label}（原文未见两者关联）",
                )
            )
    return items


def relation_claims(bundle: ContentBundle) -> list[tuple[str, str]]:
    """Each relation rendered as one atomic claim string for the entailment judge."""
    labels = _kind_labels()
    names = {eid: entity.name for eid, entity in bundle.entities.items()}
    claims: list[tuple[str, str]] = []
    for rel in bundle.relations:
        a = names.get(rel.source)
        b = names.get(rel.target)
        if a and b:
            label = labels.get(rel.kind, rel.kind)
            ref = f"relation:{rel.source}|{rel.kind}|{rel.target}"
            claims.append((ref, f"「{a}」与「{b}」的关系是「{label}」"))
    return claims


_JUDGE_SYSTEM = (
    "你是严谨的原文校对。给你一段原文，和若干条从中提炼出的「断言」。逐条判断每个断言是否能由原文"
    "直接得出或合理推断（entailment）。原文没有依据、或与原文相矛盾的，判为不支持。"
    '只输出一个 JSON 对象：{"verdicts":[{"ref":"...","supported":true或false}]}，'
    "不要任何解释或围栏。"
)


def judge_faithfulness(
    claims: list[tuple[str, str]],
    source_text: str,
    gateway: LLMGateway,
    *,
    max_claims: int = _JUDGE_MAX_CLAIMS,
) -> dict[str, bool]:
    """Tier 2 (LLM entailment): NLI-judge each structured claim against the source. Returns
    ``{ref: supported}``. A parse failure yields an empty verdict map — we never fabricate a
    "unsupported" flag from a broken reply (that would wrongly discredit grounded content)."""
    claims = claims[:max_claims]
    if not claims:
        return {}
    listed = "\n".join(f"- ref={ref}｜断言：{text}" for ref, text in claims)
    user = f"原文：\n{source_text[:_JUDGE_SOURCE_BUDGET]}\n\n待核对的断言：\n{listed}"
    raw = gateway.complete(task="verify_faithfulness", system=_JUDGE_SYSTEM, user=user)
    try:
        obj = extract_json_object(raw)
    except ValueError:
        return {}  # an unparseable reply yields no verdicts — never fabricate an "unsupported" flag
    verdicts: dict[str, bool] = {}
    if isinstance(obj, dict):
        for entry in obj.get("verdicts", []):
            if isinstance(entry, dict) and "ref" in entry:
                verdicts[str(entry["ref"])] = bool(entry.get("supported", True))
    return verdicts


def llm_unsupported(
    bundle: ContentBundle,
    source_text: str,
    gateway: LLMGateway,
    *,
    already_flagged: set[str],
) -> list[UnsupportedItem]:
    """Run the entailment judge over relation claims and return the unsupported ones not already
    caught by the deterministic tier."""
    claims = relation_claims(bundle)
    text_by_ref = dict(claims)
    verdicts = judge_faithfulness(claims, source_text, gateway)
    names = {eid: entity.name for eid, entity in bundle.entities.items()}
    out: list[UnsupportedItem] = []
    for ref, supported in verdicts.items():
        if supported or ref in already_flagged or not ref.startswith("relation:"):
            continue
        _, src, _kind, tgt = ("relation", *ref[len("relation:") :].split("|"))
        a = names.get(src, src)
        b = names.get(tgt, tgt)
        out.append(
            UnsupportedItem(
                ref=ref,
                name=f"{a} → {b}",
                kind="relation",
                reason="relation_contradicted",
                detail=text_by_ref.get(ref, ref),
                source_check="llm",
            )
        )
    return out
