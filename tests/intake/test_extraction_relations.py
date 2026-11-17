# 关系抽取叠加：证明块内抽取当 proposer、五道守卫在书稿抽取链内真生效。
"""Station-1 relation-extraction overlay: prove the §8 guards run *inside* the manuscript chain.

The weak in-chunk relation path (free ``kind``, no evidence) is replaced by an overlay that treats
block extraction as the *proposer* and runs its candidates through
``recognize.llm_relations.propose_relations_guarded`` — closed world / verbatim evidence / kind
vocabulary / confidence floor / de-dup. These tests wire a fake extraction provider that emits one
candidate per gate and assert:

* the grounded candidate lands in the draft bundle carrying its evidence span + source_ref (A1 —
  the overlay is actually invoked by the chain, not merely importable);
* every rejected candidate is absent from canon and surfaced in ``draft.suspected_relations`` with a
  reason (honest "疑似·缺证据" report, never a silent drop).
"""

from __future__ import annotations

import json

from owcopilot.content.models import ContentBundle
from owcopilot.extraction import ExtractionService
from owcopilot.extraction.service import EXTRACTION_GLEAN_MARKER
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.router import StaticRouter

# The source text every evidence span below is (or is not) a verbatim quote of.
_TEXT = "赵云与马超在长坂坡并肩作战。张飞独自守桥。"


def _gateway(provider: object, task: str = "extract_lore") -> LLMGateway:
    return LLMGateway(providers={"cheap": provider}, router=StaticRouter(mapping={task: "cheap"}))


class _FiveGateProvider:
    """Emit three characters and one relation candidate per guard, so a single extraction run
    exercises all five gates. A gleaning pass is answered empty so it stops without re-proposing."""

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        if EXTRACTION_GLEAN_MARKER in system:
            return json.dumps({}, ensure_ascii=False), 5, 5
        payload = {
            "characters": [
                {"name": "赵云", "description": "常山赵子龙"},
                {"name": "马超", "description": "西凉锦马超"},
                {"name": "张飞", "description": "燕人张翼德"},
            ],
            "relations": [
                # 1) valid: grounded evidence, controlled kind, both endpoints entities → KEPT
                {
                    "source": "赵云",
                    "target": "马超",
                    "kind": "companion_of",
                    "evidence": "赵云与马超",
                    "confidence": 0.9,
                    "description": "长坂坡并肩",
                },
                # 2) evidence gate: quote not present verbatim in the source → DROPPED
                {
                    "source": "赵云",
                    "target": "张飞",
                    "kind": "companion_of",
                    "evidence": "赵云与张飞情同手足",
                    "confidence": 0.9,
                },
                # 3) kind gate: kind not in the controlled vocabulary → DROPPED
                {
                    "source": "赵云",
                    "target": "马超",
                    "kind": "好朋友",
                    "evidence": "赵云与马超",
                    "confidence": 0.9,
                },
                # 4) closed-world gate: 诸葛亮 was not extracted as an entity → DROPPED
                {
                    "source": "赵云",
                    "target": "诸葛亮",
                    "kind": "companion_of",
                    "evidence": "赵云与马超",
                    "confidence": 0.9,
                },
                # 5) confidence gate: below the 0.5 floor → DROPPED
                {
                    "source": "马超",
                    "target": "张飞",
                    "kind": "companion_of",
                    "evidence": "张飞独自守桥",
                    "confidence": 0.2,
                },
                # 6) de-dup gate: same (source, target, kind) as #1 → DROPPED
                {
                    "source": "赵云",
                    "target": "马超",
                    "kind": "companion_of",
                    "evidence": "赵云与马超",
                    "confidence": 0.95,
                },
            ],
        }
        return json.dumps(payload, ensure_ascii=False), 5, 5


def _draft():
    service = ExtractionService(
        gateway=_gateway(_FiveGateProvider()), bundle=ContentBundle()
    )
    return service.extract(title="三国", text=_TEXT)


def test_overlay_lands_only_the_grounded_relation_with_provenance() -> None:
    # A1: the guarded overlay is invoked by the manuscript chain. Only the grounded candidate
    # reaches canon, and it carries the verbatim evidence span + a source_ref back-link.
    draft = _draft()
    assert len(draft.bundle.relations) == 1
    relation = draft.bundle.relations[0]
    names = {e.id: e.name for e in draft.bundle.entities.values()}
    assert names[relation.source] == "赵云"
    assert names[relation.target] == "马超"
    assert relation.kind == "companion_of"  # controlled-vocabulary id, not a free string
    assert relation.metadata["evidence"] == "赵云与马超"
    assert relation.metadata["evidence"] in _TEXT  # grounded verbatim
    assert relation.metadata["source_locator"] == "chunk:1"
    assert relation.source_ref is not None
    assert relation.metadata["description"] == "长坂坡并肩"  # HOW/WHY preserved through the guard


def test_ungrounded_relation_is_reported_not_silently_dropped() -> None:
    # A1 (the negative half): a relation with real endpoints + a real kind but NO verbatim evidence
    # would have landed under the old weak path; the overlay keeps it out of canon AND surfaces it.
    draft = _draft()
    kept_pairs = {(r.source, r.target, r.kind) for r in draft.bundle.relations}
    id_by_name = {e.name: e.id for e in draft.bundle.entities.values()}
    assert (id_by_name["赵云"], id_by_name["张飞"], "companion_of") not in kept_pairs
    assert any("证据未在原文中逐字命中" in reason for reason in draft.suspected_relations)


def test_every_gate_records_a_reason_in_the_suspected_report() -> None:
    # The five rejected candidates are all surfaced (honest count + list), one reason each.
    draft = _draft()
    reasons = draft.suspected_relations
    assert draft.stats["suspected_relations"] == len(reasons) == 5
    assert any("闭世界" in r for r in reasons)  # closed-world
    assert any("受控词表" in r for r in reasons)  # kind vocabulary
    assert any("逐字命中" in r for r in reasons)  # evidence grounding
    assert any("置信" in r for r in reasons)  # confidence floor
    assert any("重复" in r for r in reasons)  # de-duplication


class _NoConfidenceProvider:
    """A grounded relation with NO confidence field — recall-first: absent confidence must not
    abstain, since evidence grounding (not a self-reported score) is the precision gate."""

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        if EXTRACTION_GLEAN_MARKER in system:
            return json.dumps({}, ensure_ascii=False), 5, 5
        payload = {
            "characters": [{"name": "赵云"}, {"name": "马超"}],
            "relations": [
                {
                    "source": "赵云",
                    "target": "马超",
                    "kind": "companion_of",
                    "evidence": "赵云与马超",
                }
            ],
        }
        return json.dumps(payload, ensure_ascii=False), 5, 5


def test_missing_confidence_defaults_to_recall_first() -> None:
    service = ExtractionService(gateway=_gateway(_NoConfidenceProvider()), bundle=ContentBundle())
    draft = service.extract(title="三国", text=_TEXT)
    assert len(draft.bundle.relations) == 1
    assert draft.suspected_relations == []
