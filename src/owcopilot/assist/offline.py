"""Deterministic offline providers for assist tasks.

Like `qa.offline.OfflineQAProvider`, these are not language models: they emit minimal structured
output so the surrounding machinery — constrained prompts, parsing, audit, lint, review queue —
can be exercised end-to-end at $0. Swap in `OpenAICompatProvider` (`--llm-mode real`) for real
generation; nothing else in the flow changes.
"""

from __future__ import annotations

import json
import re

from ..content.normalize import slug_id
from .critic import BARK_CRITIQUE_MARKER, DETERMINISTIC_PROBLEMS_HEADER


def _offline_quality_critique(user: str) -> str:
    """Pass once the deterministic (lint) problems are gone — the same flip-when-clean behaviour as
    `_offline_critique`, shared by the bark and flavor offline doubles so their refine loops
    converge at $0 (and never fake a pass while lint still complains)."""
    if DETERMINISTIC_PROBLEMS_HEADER in user:
        result: dict[str, object] = {
            "verdict": "revise",
            "score": 0.4,
            "summary": "存在确定性问题，需修正。",
            "dimensions": [
                {
                    "dimension": "craft",
                    "severity": "blocker",
                    "issue": "lint 标记的条目未通过。",
                    "fix": "修正被标记的条目。",
                }
            ],
        }
    else:
        result = {
            "verdict": "pass",
            "score": 0.9,
            "summary": "质量达标。",
            "dimensions": [{"dimension": "craft", "severity": "ok", "issue": "", "fix": ""}],
        }
    return json.dumps(result, ensure_ascii=False)


class OfflineQuestDraftProvider:
    """Deterministic stand-in for the draft generator AND the refine-loop critic.

    Three behaviours, keyed off the prompt so one provider can drive the whole
    generate→critique→refine loop at $0 (real mode swaps in a real model and nothing else changes):
      * critic request (system carries the reviewer sentinel) → a critique JSON that flips from
        "revise" to "pass" once the deterministic completeness check finds nothing missing;
      * refine request (user carries the `[REFINE]` marker) → a fuller draft that adds the stages,
        rewards and giver/location the feedback asked for, grounded in the context refs;
      * otherwise → the original minimal, reference-free draft built from the brief.
    """

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        if "STRICT_QUEST_REVIEWER" in system:
            text = _offline_critique(user)
        elif "[REFINE]" in user:
            text = _offline_refined_draft(user, system)
        else:
            text = _offline_minimal_draft(user)
        return text, max(1, (len(system) + len(user)) // 4), max(1, len(text) // 4)


def _offline_minimal_draft(user: str) -> str:
    brief = user.strip() or "untitled quest"
    quest_id = slug_id(brief[:48], prefix="quest")
    return json.dumps(
        {
            "id": quest_id,
            "title": brief[:60],
            "objective": brief,
            "localization_keys": [f"quest.{quest_id}.objective"],
        },
        ensure_ascii=False,
    )


def _offline_refined_draft(user: str, system: str) -> str:
    brief = user.split("[REFINE]", 1)[0].strip() or "untitled quest"
    quest_id = slug_id(brief[:48], prefix="quest")
    npc_ref = _first_ref(system, "npc")
    loc_ref = (
        _first_ref(system, "location") or _first_ref(system, "region") or _first_ref(system, "loc")
    )
    payload: dict[str, object] = {
        "id": quest_id,
        "title": brief[:60],
        "objective": f"{brief}（细化目标，便于量产）",
        "stages": [
            {"id": "stage_1", "summary": f"接受任务：{brief[:40]}"},
            {"id": "stage_2", "summary": "完成关键行动并回报"},
        ],
        "rewards": [{"kind": "item", "value": "纪念物"}],
        "localization_keys": [f"quest.{quest_id}.objective"],
    }
    if npc_ref:
        payload["giver_npc"] = npc_ref
    if loc_ref:
        payload["location"] = loc_ref
    return json.dumps(payload, ensure_ascii=False)


def _offline_critique(user: str) -> str:
    # The caller only lists "completeness blockers" when the deterministic readiness check found
    # something missing; their absence means the draft is complete enough to pass.
    if "completeness blockers" in user:
        result = {
            "verdict": "revise",
            "score": 0.4,
            "summary": "草稿缺少可量产要素。",
            "dimensions": [
                {
                    "dimension": "completeness",
                    "severity": "blocker",
                    "issue": "缺少阶段/奖励等必备结构。",
                    "fix": "补全阶段、奖励、发布者与地点。",
                }
            ],
        }
    else:
        result = {
            "verdict": "pass",
            "score": 0.9,
            "summary": "结构完整、接地良好。",
            "dimensions": [{"dimension": "completeness", "severity": "ok", "issue": "", "fix": ""}],
        }
    return json.dumps(result, ensure_ascii=False)


class OfflineLogicDraftProvider:
    """Deterministic stand-in for the B7 quest-logic drafter. Drives the audit→refine loop at $0:
    the FIRST draft is deliberately broken (a branch condition referencing an undeclared variable),
    so the deterministic logic audit must catch it; on the refine pass (marker in the user prompt)
    it declares that variable and the audit goes clean. Real mode swaps in a model unchanged.
    """

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        stage_ids = re.findall(r"(?m)(?:^|、)([A-Za-z_]\w*)（", user)
        first = stage_ids[0] if stage_ids else "stage_1"
        second = stage_ids[1] if len(stage_ids) > 1 else first
        refining = "[LOGIC_REFINE]" in user
        variables = [{"id": "has_token", "name": "持有令牌", "type": "bool", "default": False}]
        if refining:  # fix: declare the variable the first draft referenced but never defined
            variables.append(
                {"id": "spoke_to_elder", "name": "见过长老", "type": "bool", "default": False}
            )
        payload = {
            "variables": variables,
            "precondition": "",
            "stage_logic": [
                {
                    "stage_id": first,
                    "precondition": "",
                    "effects_on_complete": [{"var": "has_token", "op": "set", "value": True}],
                }
            ],
            "branches": [
                {
                    "id": "b_gate",
                    "from_stage": first,
                    # first draft references an UNDECLARED var -> audit flags LOGIC_UNDEFINED_VAR
                    "condition": "spoke_to_elder" if not refining else "has_token",
                    "to_stage": second,
                    "outcome": "",
                }
            ],
            "unlocks": [],
        }
        text = json.dumps(payload, ensure_ascii=False)
        return text, max(1, (len(system) + len(user)) // 4), max(1, len(text) // 4)


def _first_ref(system: str, needle: str) -> str | None:
    """Pull the first `entity:<id>` context ref whose id mentions `needle` (so the offline refine
    draft grounds its giver/location in something the world actually contains)."""
    for match in re.finditer(r"\[entity:([A-Za-z0-9_]+)\]", system):
        entity_id = match.group(1)
        if needle in entity_id:
            return entity_id
    return None


class OfflineBarksProvider:
    """Deterministic stand-in for the bark generator AND its refine-loop critic. A critique request
    (marker in the system prompt) returns a verdict; otherwise it emits short variants — so one
    provider drives the whole generate→critique→refine loop at $0."""

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        if BARK_CRITIQUE_MARKER in system:
            text = _offline_quality_critique(user)
            return text, max(1, (len(system) + len(user)) // 4), max(1, len(text) // 4)
        count = _requested_variants(user)
        max_chars = _max_chars(system)
        name = _voice_name(system)
        topic = _topic(user)
        variants = []
        for index in range(1, count + 1):
            text = f"{name}: {topic} ({index})" if name else f"{topic} ({index})"
            variants.append(text[:max_chars])
        text = json.dumps({"variants": variants}, ensure_ascii=False)
        return text, max(1, (len(system) + len(user)) // 4), max(1, len(text) // 4)


def _requested_variants(user: str) -> int:
    match = re.search(r"Variants:\s*(\d+)", user)
    return max(1, int(match.group(1))) if match else 1


def _max_chars(system: str) -> int:
    match = re.search(r"<=\s*(\d+)\s*characters", system)
    return max(8, int(match.group(1))) if match else 40


def _voice_name(system: str) -> str:
    match = re.search(r'"name"\s*:\s*"([^"]*)"', system)
    return match.group(1) if match else ""


def _topic(user: str) -> str:
    match = re.search(r"Topic:\s*(.+)", user)
    return match.group(1).strip() if match else user.strip()
