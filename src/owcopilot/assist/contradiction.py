"""WS-M · Batch 2 — semantic contradiction detection over canon.

The deterministic audit (WS-A + the 26 rules) catches *structural* problems — dangling refs, bad
timeline order, logic deadlocks. It cannot see *semantic* contradictions: two pieces of canon that
assert conflicting facts ("A and B are allies" somewhere vs "A and B are sworn enemies" elsewhere),
which is exactly the "管理已有设定不自相矛盾" job the user cares most about.

Design (mirrors the theme-sweep layering, kept conservative — recall candidates, let a judge
confirm, never auto-assert):
  1. structural candidates ($0, always on): two relations on the SAME entity pair with different
     kinds — the strongest deterministic smell of a contradiction.
  2. semantic candidates (needs a real bge-m3 embedder): multiple statements *about the same
     subject* whose meanings are close enough to be talking about the same thing.
  3. LLM judge (real, optional): reads each candidate pair and decides whether it is a genuine
     contradiction + names the conflicting point. Without a judge the candidates are surfaced as
     "待人工确认" (review) — never reported as a confirmed contradiction.

Also absorbs the batch-0 L1 finding: an LLM judge can catch "unstated identity merge / inferred
attribute" contradictions that the lexical faithfulness check structurally cannot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

import numpy as np

from ..content.models import ContentBundle
from ..llm.gateway import LLMGateway
from ..llm.jsonio import extract_json_object
from ..retrieval.embedding import Embedder

_EMBED_CHARS = 800
_JUDGE_TEXT_CHARS = 400
_JUDGE_BATCH = 8

_JUDGE_SYSTEM = (
    "你是世界观一致性审校官。下面每一项是一对来自同一设定库的陈述，可能互相矛盾，也可能并不矛盾"
    "（只是各说一面、或本就兼容）。只输出 JSON，不要解释。\n"
    'markdown: {"judgements": [{"index": <编号>, "contradicts": true|false, '
    '"point": "若矛盾，一句话点出冲突在哪，否则留空"}]}\n'
    "判定从严：只有两条陈述在事实层面无法同时为真才 contradicts=true（如一对关系既盟友又死敌、"
    "同一属性取互斥值）。各自补充、视角不同、时间先后不算矛盾。每项恰好出现一次。"
)


@dataclass
class ContradictionFinding:
    refs: list[str]  # the canon refs involved (2)
    subjects: list[str]  # entity ids the conflict is about
    statements: list[str]  # the conflicting statements, in order
    verdict: str  # "contradiction" (judge-confirmed) | "review" (candidate, unjudged)
    point: str  # the conflict point (judge) or the candidate reason
    layer: str  # "relation" | "semantic"


@dataclass
class ContradictionReport:
    findings: list[ContradictionFinding] = field(default_factory=list)
    candidate_count: int = 0
    judged_count: int = 0
    semantic_used: bool = False
    llm_used: bool = False

    @property
    def contradictions(self) -> list[ContradictionFinding]:
        return [f for f in self.findings if f.verdict == "contradiction"]

    @property
    def review_suggested(self) -> list[ContradictionFinding]:
        return [f for f in self.findings if f.verdict == "review"]


def _is_semantic(embedder: Embedder | None) -> bool:
    """A real neural embedder (bge-m3) tags its model_id ``st:*``; the hashing stub does not.

    Reads ``model_id`` live so a runtime degrade (semantic model failed to load → hashing
    fallback) is seen as non-semantic — never snapshot this, or a degraded process would keep
    claiming ``st:`` (same live-read rule as ``VectorRetriever.is_semantic``)."""
    return embedder is not None and embedder.model_id.startswith("st:")


@dataclass
class _Candidate:
    refs: list[str]
    subjects: list[str]
    statements: list[str]
    layer: str
    reason: str


class ContradictionDetector:
    def __init__(
        self,
        *,
        bundle: ContentBundle,
        gateway: LLMGateway | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        self.bundle = bundle
        self.gateway = gateway
        self.embedder = embedder if _is_semantic(embedder) else None

    def detect(
        self,
        *,
        use_llm: bool = False,
        semantic_threshold: float = 0.6,
        max_judge: int = 200,
    ) -> ContradictionReport:
        candidates = self._relation_candidates() + self._semantic_candidates(semantic_threshold)
        report = ContradictionReport(
            candidate_count=len(candidates),
            # Read the embedder's live backend AFTER the semantic layer has embedded: a model that
            # degraded to the hashing stub mid-run now reads ``hashing-*`` and reports False — never
            # the construction-time snapshot, so the report can't claim semantic when it ran on the
            # stub (mirrors VectorRetriever.is_semantic).
            semantic_used=_is_semantic(self.embedder),
            llm_used=bool(use_llm and self.gateway is not None),
        )
        if not candidates:
            return report
        if use_llm and self.gateway is not None:
            to_judge = candidates[:max_judge]
            report.judged_count = len(to_judge)
            for start in range(0, len(to_judge), _JUDGE_BATCH):
                batch = to_judge[start : start + _JUDGE_BATCH]
                for index, point in self._judge(batch):
                    cand = batch[index]
                    report.findings.append(
                        ContradictionFinding(
                            refs=cand.refs,
                            subjects=cand.subjects,
                            statements=cand.statements,
                            verdict="contradiction",
                            point=point,
                            layer=cand.layer,
                        )
                    )
        else:  # no judge: surface candidates for a human, never assert a contradiction
            for cand in candidates:
                report.findings.append(
                    ContradictionFinding(
                        refs=cand.refs,
                        subjects=cand.subjects,
                        statements=cand.statements,
                        verdict="review",
                        point=cand.reason,
                        layer=cand.layer,
                    )
                )
        report.findings.sort(key=lambda f: (f.verdict != "contradiction", f.refs))
        return report

    def _name(self, entity_id: str) -> str:
        ent = self.bundle.entities.get(entity_id)
        return ent.name if ent else entity_id

    def _relation_candidates(self) -> list[_Candidate]:
        """Two relations on the same unordered entity pair with DIFFERENT kinds — the strongest
        deterministic smell of a contradiction (allies here, enemies there)."""
        by_pair: dict[frozenset[str], list[Any]] = {}
        for rel in self.bundle.relations:
            by_pair.setdefault(frozenset({rel.source, rel.target}), []).append(rel)
        out: list[_Candidate] = []
        for pair, rels in by_pair.items():
            kinds = {r.kind for r in rels}
            if len(rels) < 2 or len(kinds) < 2:
                continue
            members = sorted(pair)
            for a, b in combinations(rels, 2):
                if a.kind == b.kind:
                    continue
                out.append(
                    _Candidate(
                        refs=[
                            f"relation:{a.source}:{a.kind}:{a.target}",
                            f"relation:{b.source}:{b.kind}:{b.target}",
                        ],
                        subjects=members,
                        statements=[self._relation_text(a), self._relation_text(b)],
                        layer="relation",
                        reason=f"同一对（{self._name(members[0])}↔{self._name(members[1])}）"
                        f"存在不同关系：{a.kind} / {b.kind}",
                    )
                )
        return out

    def _relation_text(self, rel: Any) -> str:
        desc = (rel.metadata or {}).get("description") if hasattr(rel, "metadata") else ""
        base = f"{self._name(rel.source)} —{rel.kind}→ {self._name(rel.target)}"
        return f"{base}：{desc}" if desc else base

    def _statements_by_entity(self) -> dict[str, list[tuple[str, str]]]:
        """For each entity, the (ref, statement) pairs describing it: its own description plus any
        relation description that names it."""
        out: dict[str, list[tuple[str, str]]] = {}
        for eid, ent in self.bundle.entities.items():
            if ent.description.strip():
                out.setdefault(eid, []).append((f"entity:{eid}", ent.description.strip()))
        for rel in self.bundle.relations:
            desc = (rel.metadata or {}).get("description") if hasattr(rel, "metadata") else ""
            if not desc:
                continue
            ref = f"relation:{rel.source}:{rel.kind}:{rel.target}"
            for eid in (rel.source, rel.target):
                if eid in self.bundle.entities:
                    out.setdefault(eid, []).append((ref, str(desc).strip()))
        return out

    def _semantic_candidates(self, threshold: float) -> list[_Candidate]:
        """Pairs of statements about the SAME entity whose meanings are close (so they are talking
        about the same thing and could conflict). Needs a real embedder; $0/no-op without one."""
        if self.embedder is None:
            return []
        out: list[_Candidate] = []
        seen: set[frozenset[str]] = set()
        for eid, statements in self._statements_by_entity().items():
            if len(statements) < 2:
                continue
            vectors = np.asarray(
                self.embedder.embed_many([s[:_EMBED_CHARS] for _, s in statements]),
                dtype=np.float32,
            )
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            unit = vectors / np.clip(norms, 1e-9, None)
            sims = unit @ unit.T
            for i, j in combinations(range(len(statements)), 2):
                if statements[i][0] == statements[j][0]:
                    continue
                key = frozenset({statements[i][0], statements[j][0]})
                if key in seen or float(sims[i, j]) < threshold:
                    continue
                seen.add(key)
                reason = f"「{self._name(eid)}」有两条相近陈述（{sims[i, j]:.2f}），请核对。"
                out.append(
                    _Candidate(
                        refs=[statements[i][0], statements[j][0]],
                        subjects=[eid],
                        statements=[statements[i][1], statements[j][1]],
                        layer="semantic",
                        reason=reason,
                    )
                )
        return out

    def _judge(self, batch: list[_Candidate]) -> list[tuple[int, str]]:
        def _line(i: int, c: _Candidate) -> str:
            a = c.statements[0][:_JUDGE_TEXT_CHARS]
            b = c.statements[1][:_JUDGE_TEXT_CHARS]
            return f"{i}. A：{a}　｜　B：{b}".replace("\n", " ")

        listing = "\n".join(_line(i, c) for i, c in enumerate(batch))
        raw = self.gateway.complete(  # type: ignore[union-attr]
            task="contradiction_judge", system=_JUDGE_SYSTEM, user=f"PAIRS:\n{listing}"
        )
        results: list[tuple[int, str]] = []
        try:
            payload = extract_json_object(raw)
        except ValueError:
            return results  # one bad batch must not sink the run; never fabricate a contradiction
        for item in payload.get("judgements") or []:
            if not isinstance(item, dict) or not item.get("contradicts"):
                continue
            try:
                index = int(item.get("index", -1))
            except (TypeError, ValueError):
                continue
            if 0 <= index < len(batch):
                results.append((index, str(item.get("point") or "两条陈述在事实层面冲突")))
        return results


class OfflineContradictionJudge:
    """Deterministic judge double: declares a pair contradictory when the two statements contain a
    known antonym pair (盟友/死敌 etc.). $0, lets tests observe the confirm path."""

    _ANTONYMS = [
        ("盟友", "死敌"),
        ("结盟", "敌对"),
        ("结盟", "死敌"),
        ("活着", "已死"),
        ("活着", "死亡"),
    ]

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        import json

        lines = [ln for ln in user.splitlines() if ln.strip() and ln[0].isdigit()]
        judgements = []
        for ln in lines:
            index = int(ln.split(".", 1)[0])
            contradicts = any(a in ln and b in ln for a, b in self._ANTONYMS)
            judgements.append(
                {
                    "index": index,
                    "contradicts": contradicts,
                    "point": "盟友与死敌不可并存" if contradicts else "",
                }
            )
        text = json.dumps({"judgements": judgements}, ensure_ascii=False)
        return text, max(1, len(user) // 4), max(1, len(text) // 4)
