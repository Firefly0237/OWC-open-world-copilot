# 有界 agentic 检索：retrieve→assess→re-query 循环。
# 关键：两级充分性判据 + IRCoT 每跳下限，既防欠检索也防无限扩检索；跳数/预算都封顶。
"""Agentic retrieval — a bounded retrieve→assess→re-query loop over the hybrid pipeline.

The fixed pipeline (``ContextPackBuilder.build``) retrieves once. Multi-hop lore questions
("盟友的领袖驻在哪里？") often need evidence the first pass cannot surface because the bridging
entity is only named INSIDE the first pass's results. This module adds the standard agentic-RAG
skeleton (Self-RAG / CRAG family):

    round 1: pack = build(query)
    loop:    sufficient(query, pack)?  → stop
             propose follow-up queries aimed at the evidence GAP
             pack = build_expanded(query, all_followups)   # RRF-merge, rerank vs the ANCHOR

Two design inheritances keep it honest and safe:
- ``build_expanded`` reranks against the ORIGINAL query, so follow-ups can only add recall,
  never steer the answer (same principle as one-shot query expansion).
- The loop is BOUNDED (``max_rounds``) and every decision is recorded in the result, so a run
  is auditable: which rounds fired, what was asked, why it stopped.

Sufficiency is two-level, faithful to CRAG's retrieval-evaluator-triggers-corrective-action
shape (Yan et al., 2024): level-1 is the deterministic lexical coverage signal (below threshold ≈
CRAG 'Incorrect' → corrective re-retrieval); level-2 is an explicit LLM judge, consulted only when
lexical is satisfied and a gateway/budget exist, that decides 'Correct' (accept) vs 'Ambiguous'
(sufficient=false → one more corrective round steered by the judge's `missing`). The judge encodes
Self-RAG's ISSUP support critique — does the evidence CONTAIN the asked-for fact, or merely mention
the same entities. Follow-up generation is likewise deterministic by default, and an optional LLM
gateway upgrades it to gap-aware rewriting (steered by the judge's `missing` when present).

Known boundary (honest): bridging can only reuse entity names that appear IN the retrieved
text. A link that exists only as a structured Relation (enemy_of ids) with no textual mention
in either endpoint's description gives the rewriter nothing to extract — surfacing those needs
relation-to-text rendering in the evidence (a separate retrieval improvement), not more loop
rounds. Corpora whose descriptions cross-reference names (the normal lore-writing convention)
bridge fine.
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import BaseModel

from ..llm.jsonio import extract_json
from ..llm.structured import complete_structured
from .context_pack import ContextPackBuilder
from .models import ContextPack
from .text_match import query_terms

if TYPE_CHECKING:
    from ..llm.gateway import LLMGateway

logger = logging.getLogger(__name__)

#: Router task label for the follow-up rewriter.
AGENTIC_FOLLOWUP_TASK = "agentic_rag_followup"

#: Router task label for the level-2 LLM sufficiency judge.
AGENTIC_SUFFICIENCY_TASK = "agentic_rag_sufficiency"

#: A query counts as sufficiently evidenced when this fraction of its informative-term weight
#: is covered by the pack text. Below it, another retrieval round (if budget remains) fires.
#: 0.8 empirically separates the regimes on the test scenarios: a direct entity question covers
#: ~1.0; a multi-hop question whose bridge is present but whose target fact is missing sits
#: ~0.7; an unanswerable query sits far lower. A lexical signal remains a coarse proxy — it
#: cannot tell "the word 领袖 appears" from "the leader's location appears"; the LLM follow-up
#: path (and a future LLM sufficiency judge) is the precision upgrade.
_SUFFICIENCY_THRESHOLD = 0.8

_FOLLOWUP_SYSTEM = (
    "You improve retrieval for a game-lore QA system. Given a QUESTION and the EVIDENCE "
    "retrieved so far, identify what the evidence is still missing to answer the question, "
    "and write 1-3 short follow-up search queries targeting exactly that gap. Reuse concrete "
    "entity NAMES you see in the evidence when they help bridge to the missing fact — the "
    "display name as written in the text (e.g. 黄沙客栈), NEVER internal ids like fac_sand: "
    "ids do not appear in the searchable text, so an id query retrieves nothing. Write the "
    "queries in the same language as the evidence. "
    # An OBJECT contract (not a bare array): providers' json_object modes force an object
    # reply, so an array contract fails nondeterministically under json_mode.
    'Reply with ONLY one JSON object: {"queries": ["query one", "query two"]}.'
)

#: Level-2 sufficiency judge (Self-RAG ISSUP / CRAG retrieval-evaluator shape). Short and generic —
#: no case-specific content — mirroring the register of ``qa.faithfulness:_JUDGE_SYSTEM``. Decides
#: whether the evidence actually CONTAINS the asked-for fact (support), not merely mentions the same
#: entities (existence), and names the still-missing fact so the corrective round can steer on it.
_SUFFICIENCY_SYSTEM = (
    "You are a strict retrieval-sufficiency judge for a game-lore QA system. "
    "You are given a QUESTION and the EVIDENCE retrieved so far. Decide whether the EVIDENCE "
    "actually CONTAINS the specific fact needed to answer the question, not merely mentions the "
    "same entities. "
    "sufficient=true ONLY when the evidence states the specific asked-for fact; if it merely "
    "mentions the same entities without that fact, sufficient=false (mention is not support). "
    "When in doubt, answer sufficient=false. "
    'When sufficient=false, set "missing" to a short phrase naming the still-missing fact (used '
    'to steer the next search); otherwise set "missing" to an empty string. '
    'Reply with ONLY one JSON object, no prose: {"sufficient": true|false, "missing": "<phrase>"}.'
)


class SufficiencyVerdict(BaseModel):
    """Validated shape of the level-2 judge reply (see :data:`_SUFFICIENCY_SYSTEM`).

    ``sufficient`` is REQUIRED: a reply that omits it fails validation and triggers
    ``complete_structured``'s bounded repair-retry rather than defaulting to a verdict.
    """

    sufficient: bool
    missing: str = ""


@dataclass
class AgenticRetrievalResult:
    """The pack plus the loop's full decision record (auditable, test-assertable)."""

    pack: ContextPack
    rounds: int  # retrieval rounds actually run (1 = the plain single pass)
    sufficient: bool  # did the loop stop because evidence was judged sufficient?
    coverage: float  # final content-term coverage of the query by the pack text
    followup_queries: list[str] = field(default_factory=list)  # everything asked after round 1
    # Why the loop ended: "coverage" (level-1 lexical sufficiency), "llm_sufficient" (level-2 judge
    # accepted the evidence — CRAG 'Correct'), "llm_no_gap" (the rewriter replied with an explicit
    # empty query list — the model's own sufficiency verdict, more precise than the lexical proxy),
    # "no_new_queries" (nothing new to ask), "max_rounds".
    stop_reason: str = "coverage"


@dataclass
class _SufficiencyDecision:
    """Outcome of a single sufficiency assessment (level-1 lexical and/or level-2 judge)."""

    sufficient: bool
    missing: str  # short phrase naming the still-missing fact (LLM-only; "" for the lexical path)
    reason: str  # the AgenticRetrievalResult.stop_reason to record if THIS verdict stops the loop


#: CJK function characters (copula/locative/aspect/conjunction). An n-gram containing one is a
#: cross-boundary artefact ("盟是什") or a grammatical shell ("是什么") — never the entity or
#: fact the retrieval must evidence — so it is excluded from the coverage signal. A heuristic
#: list, honestly: the lexical sufficiency signal is a coarse $0 proxy either way.
_FUNCTIONAL_CHARS = frozenset("的是在了吗呢么与和或从到把被对为")


def _informative_terms(query: str) -> list[str]:
    return [t for t in query_terms(query) if not set(t) & _FUNCTIONAL_CHARS]


def term_coverage(query: str, pack: ContextPack) -> float:
    """Length-weighted fraction of the query's INFORMATIVE terms present in the pack text.

    The deterministic ``$0`` sufficiency signal: longer (more specific) terms dominate, same
    weighting idea as the offline entailment judge; grammatical/cross-boundary n-grams are
    excluded (see ``_FUNCTIONAL_CHARS``). 1.0 when the query has no informative terms.
    """
    terms = _informative_terms(query)
    if not terms:
        return 1.0
    haystack = unicodedata.normalize("NFKC", _pack_text(pack)).lower()
    total = sum(len(t) for t in terms)
    present = sum(len(t) for t in terms if t.lower() in haystack)
    return present / total


def _pack_text(pack: ContextPack) -> str:
    return "\n".join(" ".join(part for part in (hit.title, hit.body) if part) for hit in pack.hits)


def _heuristic_followups(query: str, pack: ContextPack) -> list[str]:
    """$0 gap-aimed follow-ups: the query's UNCOVERED content terms, alone and bridged with the
    top evidence title (the entity most likely to connect to the missing fact)."""
    haystack = unicodedata.normalize("NFKC", _pack_text(pack)).lower()
    missing = [t for t in _informative_terms(query) if t.lower() not in haystack]
    if not missing:
        return []
    followups = [" ".join(missing[:3])]
    top_title = next((hit.title for hit in pack.hits if hit.title), "")
    if top_title:
        followups.append(f"{top_title} {missing[0]}")
    return followups


class AgenticRetriever:
    """Bounded agentic retrieval over a :class:`ContextPackBuilder` (see module docstring)."""

    def __init__(
        self,
        builder: ContextPackBuilder,
        *,
        gateway: LLMGateway | None = None,
        max_rounds: int = 2,
        sufficiency_threshold: float = _SUFFICIENCY_THRESHOLD,
    ) -> None:
        if max_rounds < 1:
            raise ValueError("max_rounds must be >= 1")
        self.builder = builder
        self.gateway = gateway  # None = deterministic $0 follow-ups (CI default)
        self.max_rounds = max_rounds
        self.sufficiency_threshold = sufficiency_threshold

    def retrieve(
        self, query: str, *, budget_tokens: int = 800, limit: int = 10
    ) -> AgenticRetrievalResult:
        pack = self.builder.build(query, budget_tokens=budget_tokens, limit=limit)
        followups: list[str] = []
        rounds = 1
        coverage = term_coverage(query, pack)
        # Two-level sufficiency (CRAG retrieval-evaluator → corrective action): level-1 is lexical
        # coverage; level-2 is the explicit LLM judge, consulted only when lexical is satisfied AND
        # a gateway exists AND there is budget to act on an "insufficient" verdict.
        decision = self._assess_sufficiency(query, pack, coverage, can_act=rounds < self.max_rounds)
        while not decision.sufficient and rounds < self.max_rounds:
            # Steer the follow-up rewriter with the judge's `missing` hint (empty on the lexical
            # path, so the rewriter prompt stays byte-identical to before when no judge fired).
            proposed = self._propose_followups(query, pack, missing=decision.missing)
            if proposed == []:
                # An explicit empty reply from the LLM rewriter is its own sufficiency verdict
                # ("the evidence already answers this") — more precise than the lexical proxy,
                # which cannot see that e.g. 哪里 is answered by 听雾崖. Stop, sufficient.
                decision = _SufficiencyDecision(True, "", "llm_no_gap")
                break
            new_queries = [
                q for q in proposed if q.strip() and q.strip() != query and q not in followups
            ]
            if not new_queries:
                # nothing new to ask — more rounds would just repeat round 1
                decision = _SufficiencyDecision(False, "", "no_new_queries")
                break
            followups.extend(new_queries)
            pack = self.builder.build_expanded(
                query, followups, budget_tokens=budget_tokens, limit=limit
            )
            # Per-hop floor (IRCoT-style): the anchored rerank scores everything against the
            # ORIGINAL query, so a true 2nd-hop row — which by nature shares few terms with the
            # original question — ranks last and gets budget-cut. Guarantee each follow-up's top
            # hits a seat so the bridge evidence the loop just went looking for actually
            # survives into the pack even when anchored reranking would otherwise squeeze it out.
            pack = self._with_followup_floor(
                pack, new_queries, budget_tokens=budget_tokens, limit=limit
            )
            rounds += 1
            coverage = term_coverage(query, pack)
            decision = self._assess_sufficiency(
                query, pack, coverage, can_act=rounds < self.max_rounds
            )
        return AgenticRetrievalResult(
            pack=pack,
            rounds=rounds,
            sufficient=decision.sufficient,
            coverage=round(coverage, 4),
            followup_queries=followups,
            stop_reason=decision.reason,
        )

    def _assess_sufficiency(
        self, query: str, pack: ContextPack, coverage: float, *, can_act: bool
    ) -> _SufficiencyDecision:
        """Two-level sufficiency verdict for the current pack.

        Level-1 (lexical, always $0): coverage below threshold is a definite under-retrieval
        (CRAG 'Incorrect') → insufficient, NO judge call. Level-2 (explicit LLM judge, CRAG
        'Correct' vs 'Ambiguous'): only when lexical is satisfied AND a gateway exists AND there
        is budget to act on an insufficient verdict — otherwise the pure-lexical verdict stands,
        which preserves the gateway=None behavior and avoids a judge call we could not act on.
        """
        if coverage < self.sufficiency_threshold:
            return _SufficiencyDecision(False, "", "max_rounds")
        if self.gateway is None or not can_act:
            return _SufficiencyDecision(True, "", "coverage")
        return self._llm_sufficiency(query, pack)

    def _llm_sufficiency(self, query: str, pack: ContextPack) -> _SufficiencyDecision:
        """Level-2 judge: does the evidence CONTAIN the asked-for fact (Self-RAG ISSUP support),
        not merely mention the same entities? Degrades to the lexical verdict on any failure.

        The judge is a PRECISION enhancement, not a correctness gate: a bad model reply (repair
        exhausted → StructuredOutputError) or a gateway/transport error must not crash or stall
        retrieval. So it degrades to the lexical "sufficient" verdict — but NEVER silently: the
        cause is logged, the same honest-degrade discipline ``_propose_followups`` uses.
        """
        assert self.gateway is not None  # narrowed by the caller (_assess_sufficiency)
        evidence = _pack_text(pack) or "(no evidence retrieved)"
        user = f"QUESTION:\n{query}\n\nEVIDENCE:\n{evidence}"
        try:
            verdict = complete_structured(
                self.gateway,
                task=AGENTIC_SUFFICIENCY_TASK,
                system=_SUFFICIENCY_SYSTEM,
                user=user,
                model_cls=SufficiencyVerdict,
            )
        except Exception as exc:  # noqa: BLE001 — degrade to lexical, but leave a trace
            logger.warning("agentic sufficiency judge failed (%s) — lexical verdict", exc)
            return _SufficiencyDecision(True, "", "coverage")
        if verdict.sufficient:
            return _SufficiencyDecision(True, "", "llm_sufficient")
        return _SufficiencyDecision(False, verdict.missing.strip(), "max_rounds")

    #: Per-hop floor: how many of each follow-up's own top hits are guaranteed a pack seat.
    _FLOOR_PER_FOLLOWUP = 2

    def _with_followup_floor(
        self, pack: ContextPack, new_queries: list[str], *, budget_tokens: int, limit: int
    ) -> ContextPack:
        """Merge each follow-up's own top hits into the pack (deduped, capped at ``limit``).

        Floor hits are appended after the anchored ranking (they extend recall, they do not
        reorder the anchor's precision); when the merge exceeds ``limit``, the anchored tail —
        not the floor — is dropped, since the floor rows are exactly the bridge evidence this
        round retrieved on purpose.
        """
        seen = {hit.ref for hit in pack.hits}
        floor_hits = []
        for followup in new_queries:
            sub = self.builder.build(
                followup, budget_tokens=budget_tokens, limit=self._FLOOR_PER_FOLLOWUP
            )
            for hit in sub.hits[: self._FLOOR_PER_FOLLOWUP]:
                if hit.ref not in seen:
                    seen.add(hit.ref)
                    floor_hits.append(hit)
        if not floor_hits:
            return pack
        keep_anchor = max(1, limit - len(floor_hits))
        merged = [*pack.hits[:keep_anchor], *floor_hits][:limit]
        return ContextPack(query=pack.query, budget_tokens=pack.budget_tokens, hits=merged)

    def _propose_followups(self, query: str, pack: ContextPack, *, missing: str = "") -> list[str]:
        """Gap-aimed follow-up queries: LLM rewriting when a gateway is wired, else the
        deterministic heuristic. An unusable LLM reply falls back to the heuristic — the loop
        must never crash or stall on a bad model reply — but NEVER silently: every fallback
        logs its cause (an invisible degrade is how the A/B experiment got poisoned).

        ``missing`` is the level-2 judge's name for the still-missing fact; when set it steers the
        LLM rewriter at exactly that gap. The append is guarded by ``if missing:`` so that with
        ``missing=""`` (the lexical path, and the only path when gateway=None) the user string is
        byte-identical to before. ``_heuristic_followups`` computes its own gap and ignores it.
        """
        if self.gateway is None:
            return _heuristic_followups(query, pack)
        evidence = _pack_text(pack) or "(no evidence retrieved)"
        user = f"QUESTION:\n{query}\n\nEVIDENCE SO FAR:\n{evidence}"
        if missing:
            user += f"\n\nThe evidence is still MISSING: {missing}"
        try:
            raw = self.gateway.complete(
                task=AGENTIC_FOLLOWUP_TASK,
                system=_FOLLOWUP_SYSTEM,
                user=user,
            )
            parsed = extract_json(raw)
            queries = parsed.get("queries", []) if isinstance(parsed, dict) else parsed
            if not isinstance(queries, list):
                raise ValueError("follow-up reply must contain a query list")
            cleaned = [str(q) for q in queries if str(q).strip()][:3]
            if not cleaned:
                # A well-formed EMPTY reply is the model's sufficiency verdict, not a
                # failure: the caller stops the loop (stop_reason="llm_no_gap").
                logger.info("agentic followup: model judged the evidence sufficient (empty reply)")
            return cleaned
        except Exception as exc:  # noqa: BLE001 — degrade, not die; but leave a trace
            logger.warning("agentic followup: LLM call failed (%s) — heuristic fallback", exc)
            return _heuristic_followups(query, pack)
