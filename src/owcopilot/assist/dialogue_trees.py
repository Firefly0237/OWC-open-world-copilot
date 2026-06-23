"""Branching dialogue tree generation: voice-card constrained, integrity-checked, reviewable."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel, Field

from ..content.lang import detect_language, language_directive
from ..content.models import (
    ContentBundle,
    DialogueChoice,
    DialogueNode,
    DialogueTree,
    Origin,
    ReviewStatus,
)
from ..llm.gateway import LLMGateway
from ..llm.jsonio import extract_json_object
from ..util import slugify, unique_id
from .calibration import critic_from_trail
from .critic import DIALOGUE_CRITIQUE_MARKER, DialogueCritic
from .industry import DIALOGUE_RUBRIC_SOURCES, industry_source_block
from .lint import AssistLintIssue, lint_text
from .refine import RefineStep, run_refine_loop
from .review_queue import ReviewItem, ReviewQueue
from .voice import build_voice_card

_SYSTEM_PROMPT = (
    "You write branching game dialogue. Return ONE JSON object only (no markdown): "
    '{"title": str, "root": str, "nodes": [{"id": str, "speaker": str, "text": str, '
    '"next": str|null, "choices": [{"text": str, "next": str|null}]}]}. '
    "speaker must be one of the provided speaker ids. Use choices for player decisions "
    "(2-3 options) and next for linear flow; a node with neither ends the branch. "
    "Every next/choice target must be a node id defined in nodes. "
    "Keep each line within the given character budget and within the speakers' voice cards.\n"
    + industry_source_block(*DIALOGUE_RUBRIC_SOURCES)
    + "\n"
    # Quality bar (二游 dialogue rubric): the #1 failure is interchangeable voices + on-the-nose
    # exposition that explains the setting at each other.
    "QUALITY BAR — make it sound like two distinct people, not an info dump:\n"
    "1. DISTINCT VOICE: a reader should tell who speaks from word choice/rhythm alone, without the "
    "name. Honor each voice card (diction, tics, formality).\n"
    "2. SUBTEXT (潜台词): say it slant — through what is withheld, deflected, or implied. Avoid "
    "characters explaining lore or their own feelings outright.\n"
    "3. STAKES & STANCE: each speaker wants something opposed; the line pushes their want, not the "
    "author's exposition. Conflict escalates; no neutral Q&A.\n"
    "4. Concrete over abstract: use the world's objects/imagery, not generic sentiment."
)


class DialogueTreeResult(BaseModel):
    tree: DialogueTree
    lint_issues: list[AssistLintIssue] = Field(default_factory=list)
    structure_problems: list[str] = Field(default_factory=list)
    review_item: ReviewItem | None = None
    refine_trail: list[RefineStep] = Field(default_factory=list)
    auto_review_incomplete: bool = False


class DialogueTreeService:
    def __init__(
        self,
        *,
        gateway: LLMGateway,
        bundle: ContentBundle,
        review_queue: ReviewQueue | None = None,
        critic: DialogueCritic | None = None,
        max_refine_rounds: int = 0,
    ) -> None:
        self.gateway = gateway
        self.bundle = bundle
        self.review_queue = review_queue
        # Opt-in critique→refine loop: without a critic the service is the original single shot.
        self.critic = critic
        self.max_refine_rounds = max_refine_rounds if critic is not None else 0

    def generate(
        self,
        *,
        participant_ids: list[str],
        brief: str,
        quest_id: str | None = None,
        max_nodes: int = 12,
        max_chars: int = 120,
    ) -> DialogueTreeResult:
        unknown = [pid for pid in participant_ids if pid not in self.bundle.entities]
        if unknown:
            raise ValueError(f"unknown participant entities: {', '.join(unknown)}")
        known_entities = set(self.bundle.entities)

        def make(prior: DialogueTree | None, feedback: list[str] | None) -> DialogueTree:
            return self._compose(
                participant_ids,
                brief,
                quest_id=quest_id,
                max_nodes=max_nodes,
                max_chars=max_chars,
                prior=prior,
                feedback=feedback,
            )

        tree = make(None, None)
        trail: list[RefineStep] = []
        auto_review_incomplete = False
        if self.critic is not None:

            def assess(t: DialogueTree) -> tuple[list[str], Any]:
                assert self.critic is not None
                problems = tree_structure_problems(t, known_entities=known_entities)
                critique = self.critic.critique(
                    brief=brief,
                    nodes=t.model_dump(mode="json", exclude_none=True).get("nodes", {}),
                    speaker_ids=participant_ids,
                    structure_problems=problems,
                )
                return problems, critique

            outcome = run_refine_loop(
                initial=tree,
                max_rounds=self.max_refine_rounds,
                assess=assess,
                regenerate=lambda t, fixes: make(t, fixes),
            )
            tree = outcome.artifact
            trail = outcome.trail
            auto_review_incomplete = outcome.auto_review_incomplete

        lint_issues: list[AssistLintIssue] = []
        for node in tree.nodes.values():
            lint_issues.extend(
                lint_text(
                    node.text,
                    bundle=self.bundle,
                    max_chars=max_chars,
                    allowed_entity_ids=set(participant_ids),
                )
            )
        problems = tree_structure_problems(tree, known_entities=known_entities)
        result = DialogueTreeResult(
            tree=tree,
            lint_issues=lint_issues,
            structure_problems=problems,
            refine_trail=trail,
            auto_review_incomplete=auto_review_incomplete,
        )
        if self.review_queue is not None:
            verdict, score = critic_from_trail([r.model_dump(mode="json") for r in trail])
            result.review_item = self.review_queue.add_dialogue_tree(
                tree.model_dump(mode="json", exclude_none=True),
                critic_verdict=verdict,
                critic_score=score,
            )
        return result

    def _compose(
        self,
        participant_ids: list[str],
        brief: str,
        *,
        quest_id: str | None,
        max_nodes: int,
        max_chars: int,
        prior: DialogueTree | None,
        feedback: list[str] | None,
    ) -> DialogueTree:
        cards = [
            build_voice_card(self.bundle.entities[pid], self.bundle) for pid in participant_ids
        ]
        cards_json = json.dumps([c.model_dump(mode="json") for c in cards], ensure_ascii=False)
        # Keep dialogue in the brief's language — the English quality bar can otherwise drift the
        # model to English on a Chinese brief (same fix as the quest-draft prompt).
        lang = language_directive(detect_language(brief)) if brief.strip() else ""
        system = (
            f"{_SYSTEM_PROMPT}\nCharacter budget per line: {max_chars}. Max nodes: {max_nodes}.\n"
            f"{(lang + chr(10)) if lang else ''}Voice cards: {cards_json}"
        )
        user = _dialogue_user_message(brief, participant_ids, prior=prior, feedback=feedback)

        # name/id -> id, so a speaker the model writes as a display name still maps to its entity
        aliases: dict[str, str] = {}
        for pid in participant_ids:
            aliases[pid] = pid
            ent = self.bundle.entities.get(pid)
            if ent is not None and ent.name.strip():
                aliases[ent.name.strip()] = pid

        def _parse(text: str) -> DialogueTree:
            return parse_dialogue_tree(
                text,
                brief=brief,
                participant_ids=participant_ids,
                quest_id=quest_id,
                existing_ids=set(self.bundle.dialogue_trees),
                max_nodes=max_nodes,
                speaker_aliases=aliases,
            )

        raw = self.gateway.complete(task="dialogue_tree", system=system, user=user)
        try:
            return _parse(raw)
        except ValueError:
            # One honest retry with a strict JSON-only nudge before failing (richer dialogue can run
            # long / occasionally wrap in stray text). Still raises if the retry is unparseable too.
            strict = user + "\n\n严格只返回一个完整的 JSON 对象，不要任何额外文字、不要省略。"
            raw = self.gateway.complete(task="dialogue_tree", system=system, user=strict)
            return _parse(raw)

    def revise(
        self, prior: DialogueTree, feedback: str, *, max_nodes: int = 12, max_chars: int = 120
    ) -> DialogueTree:
        """Regenerate the dialogue to address reviewer feedback; the prior tree carries the content
        so retrieval grounding is unnecessary -- only the speakers and the prior matter."""
        participant_ids = [pid for pid in prior.participants if pid in self.bundle.entities]
        first_line = next(iter(prior.nodes.values())).text if prior.nodes else ""
        brief = (first_line or "改进这段对话").strip()
        revised = self._compose(
            participant_ids,
            brief,
            quest_id=prior.quest_id,
            max_nodes=max_nodes,
            max_chars=max_chars,
            prior=prior,
            feedback=[feedback.strip()],
        )
        revised.metadata["revised_from_feedback"] = "true"
        return revised


def _dialogue_user_message(
    brief: str,
    participant_ids: list[str],
    *,
    prior: DialogueTree | None = None,
    feedback: list[str] | None = None,
) -> str:
    base = f"Brief: {brief}\nSpeakers: {', '.join(participant_ids)}"
    if prior is None or not feedback:
        return base
    prior_nodes = json.dumps(
        prior.model_dump(mode="json", exclude_none=True).get("nodes", {}), ensure_ascii=False
    )
    fix_lines = "\n".join(f"- {fix}" for fix in feedback)
    return (
        f"{base}\n\n[REFINE] 这是上一版对话树。请产出改进后的完整 JSON，逐条解决下列意见、"
        f"修好结构问题、让选项是真正不同的抉择：\n上一版节点：\n{prior_nodes}\n\n必须解决：\n{fix_lines}"
    )


def parse_dialogue_tree(
    raw: str,
    *,
    brief: str,
    participant_ids: list[str],
    quest_id: str | None,
    existing_ids: set[str],
    max_nodes: int,
    speaker_aliases: dict[str, str] | None = None,
) -> DialogueTree:
    payload = extract_json_object(raw)
    tree_id = _unique_id("dlg", str(payload.get("title") or brief[:24]), set(existing_ids))
    speakers = set(participant_ids)
    aliases = speaker_aliases or {}
    nodes: dict[str, DialogueNode] = {}
    for raw_node in list(payload.get("nodes") or [])[:max_nodes]:
        if not isinstance(raw_node, dict):
            continue
        node_id = _slug(str(raw_node.get("id") or f"node_{len(nodes) + 1}")) or (
            f"node_{len(nodes) + 1}"
        )
        speaker = str(raw_node.get("speaker") or "").strip() or None
        if speaker is not None and speaker not in speakers:
            # Models often answer with the display NAME instead of the id ("npc_eldrin"). Resolve
            # via the name->id alias map first; only then fall back to a substring guess.
            speaker = aliases.get(speaker) or next(
                (pid for pid in participant_ids if speaker in pid or pid in speaker), None
            )  # unmatched speakers stay None and surface via the structure check
        choices = [
            DialogueChoice(
                text=str(choice.get("text") or ""),
                next_node=_optional_slug(choice.get("next")),
            )
            for choice in raw_node.get("choices") or []
            if isinstance(choice, dict) and str(choice.get("text") or "").strip()
        ]
        nodes[node_id] = DialogueNode(
            id=node_id,
            speaker_id=speaker,
            text=str(raw_node.get("text") or ""),
            choices=choices,
            next_node=_optional_slug(raw_node.get("next")),
        )
    root = _optional_slug(payload.get("root")) or (next(iter(nodes), ""))
    return DialogueTree(
        id=tree_id,
        title=str(payload.get("title") or brief[:40]),
        quest_id=quest_id,
        participants=list(participant_ids),
        root_node=root,
        nodes=nodes,
        metadata={"brief": brief},
        origin=Origin.AI_DRAFT,
        review_status=ReviewStatus.PENDING_REVIEW,
    )


def tree_structure_problems(tree: DialogueTree, *, known_entities: set[str]) -> list[str]:
    """Draft-time integrity check; the audit rules re-verify after any accept."""
    problems: list[str] = []
    if not tree.nodes:
        problems.append("对话树没有任何节点")
        return problems
    if tree.root_node not in tree.nodes:
        problems.append(f"根节点 '{tree.root_node}' 不存在")
    for node in tree.nodes.values():
        if node.speaker_id is None:
            problems.append(f"节点 '{node.id}' 缺少说话人")
        elif node.speaker_id not in known_entities:
            problems.append(f"节点 '{node.id}' 的说话人 '{node.speaker_id}' 不在实体档案中")
        if node.next_node and node.next_node not in tree.nodes:
            problems.append(f"节点 '{node.id}' 的 next 指向不存在的 '{node.next_node}'")
        for index, choice in enumerate(node.choices):
            if choice.next_node and choice.next_node not in tree.nodes:
                problems.append(
                    f"节点 '{node.id}' 选项 {index + 1} 指向不存在的 '{choice.next_node}'"
                )
    return problems


def _unique_id(prefix: str, raw: str, used: set[str]) -> str:
    return unique_id(prefix, raw, used, fallback="tree")


def _optional_slug(value: Any) -> str | None:
    if value is None:
        return None
    return slugify(str(value)) or None


def _slug(value: str) -> str:
    return slugify(value)


class OfflineDialogueTreeProvider:
    """Deterministic 4-node tree (greeting -> choice -> two endings) for $0 pipeline tests."""

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        if DIALOGUE_CRITIQUE_MARKER in system:
            text = _offline_dialogue_critique(user)
            return text, max(1, (len(system) + len(user)) // 4), max(1, len(text) // 4)
        speakers = _speakers_from_user(user)
        first = speakers[0] if speakers else "narrator"
        second = speakers[1] if len(speakers) > 1 else first
        brief = _brief_from_user(user)
        digest = hashlib.sha256(user.encode("utf-8")).hexdigest()[:6]
        payload = {
            "title": f"{brief[:16]}对话_{digest}",
            "root": "n1",
            "nodes": [
                {"id": "n1", "speaker": first, "text": f"{brief[:24]}……你怎么看？", "next": "n2"},
                {
                    "id": "n2",
                    "speaker": second,
                    "text": "这件事没那么简单。",
                    "choices": [
                        {"text": "继续追问", "next": "n3"},
                        {"text": "转身离开", "next": "n4"},
                    ],
                },
                {"id": "n3", "speaker": second, "text": "好吧，我把知道的都告诉你。"},
                {"id": "n4", "speaker": first, "text": "等等——这条线索你拿着。"},
            ],
        }
        text = json.dumps(payload, ensure_ascii=False)
        return text, max(1, (len(system) + len(user)) // 4), max(1, len(text) // 4)


def _offline_dialogue_critique(user: str) -> str:
    """The dialogue critic only lists "blockers" when the deterministic structure check found
    problems; the canned 4-node tree is well-formed, so the default verdict is pass."""
    if "treat as blockers" in user:
        result: dict[str, Any] = {
            "verdict": "revise",
            "score": 0.5,
            "summary": "对话树结构有问题。",
            "dimensions": [
                {
                    "dimension": "grounding",
                    "severity": "blocker",
                    "issue": "结构检查发现问题。",
                    "fix": "修好断链/未知说话人，让每个选项指向已定义节点。",
                }
            ],
        }
    else:
        result = {
            "verdict": "pass",
            "score": 0.9,
            "summary": "对话连贯、选项是真抉择、说话人在档。",
            "dimensions": [{"dimension": "coherence", "severity": "ok", "issue": "", "fix": ""}],
        }
    return json.dumps(result, ensure_ascii=False)


def _speakers_from_user(user: str) -> list[str]:
    match = re.search(r"Speakers:\s*(.+)", user)
    if not match:
        return []
    return [part.strip() for part in match.group(1).split(",") if part.strip()]


def _brief_from_user(user: str) -> str:
    match = re.search(r"Brief:\s*(.+)", user)
    return match.group(1).strip() if match else user.strip()[:40]
