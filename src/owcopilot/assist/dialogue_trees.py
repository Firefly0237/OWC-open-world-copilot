"""Branching dialogue tree generation: voice-card constrained, integrity-checked, reviewable."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel, Field

from ..content.models import (
    ContentBundle,
    DialogueChoice,
    DialogueNode,
    DialogueTree,
    Origin,
    ReviewStatus,
)
from ..llm.gateway import LLMGateway
from .lint import AssistLintIssue, lint_text
from .review_queue import ReviewItem, ReviewQueue
from .voice import build_voice_card

_SYSTEM_PROMPT = (
    "You write branching game dialogue. Return ONE JSON object only (no markdown): "
    '{"title": str, "root": str, "nodes": [{"id": str, "speaker": str, "text": str, '
    '"next": str|null, "choices": [{"text": str, "next": str|null}]}]}. '
    "speaker must be one of the provided speaker ids. Use choices for player decisions "
    "(2-3 options) and next for linear flow; a node with neither ends the branch. "
    "Every next/choice target must be a node id defined in nodes. "
    "Keep each line within the given character budget and within the speakers' voice cards."
)


class DialogueTreeResult(BaseModel):
    tree: DialogueTree
    lint_issues: list[AssistLintIssue] = Field(default_factory=list)
    structure_problems: list[str] = Field(default_factory=list)
    review_item: ReviewItem | None = None


class DialogueTreeService:
    def __init__(
        self,
        *,
        gateway: LLMGateway,
        bundle: ContentBundle,
        review_queue: ReviewQueue | None = None,
    ) -> None:
        self.gateway = gateway
        self.bundle = bundle
        self.review_queue = review_queue

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
        cards = [
            build_voice_card(self.bundle.entities[pid], self.bundle) for pid in participant_ids
        ]
        cards_json = json.dumps(
            [card.model_dump(mode="json") for card in cards], ensure_ascii=False
        )
        raw = self.gateway.complete(
            task="dialogue_tree",
            system=(
                f"{_SYSTEM_PROMPT}\nCharacter budget per line: {max_chars}. "
                f"Max nodes: {max_nodes}.\nVoice cards: {cards_json}"
            ),
            user=f"Brief: {brief}\nSpeakers: {', '.join(participant_ids)}",
        )
        tree = parse_dialogue_tree(
            raw,
            brief=brief,
            participant_ids=participant_ids,
            quest_id=quest_id,
            existing_ids=set(self.bundle.dialogue_trees),
            max_nodes=max_nodes,
        )
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
        problems = tree_structure_problems(tree, known_entities=set(self.bundle.entities))
        result = DialogueTreeResult(tree=tree, lint_issues=lint_issues, structure_problems=problems)
        if self.review_queue is not None:
            result.review_item = self.review_queue.add_dialogue_tree(
                tree.model_dump(mode="json", exclude_none=True)
            )
        return result


def parse_dialogue_tree(
    raw: str,
    *,
    brief: str,
    participant_ids: list[str],
    quest_id: str | None,
    existing_ids: set[str],
    max_nodes: int,
) -> DialogueTree:
    text = raw.strip()
    if text.startswith("```"):
        text = text[text.find("{") : text.rfind("}") + 1]
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("dialogue tree provider returned non-object JSON")
    tree_id = _unique_id("dlg", str(payload.get("title") or brief[:24]), set(existing_ids))
    speakers = set(participant_ids)
    nodes: dict[str, DialogueNode] = {}
    for raw_node in list(payload.get("nodes") or [])[:max_nodes]:
        if not isinstance(raw_node, dict):
            continue
        node_id = _slug(str(raw_node.get("id") or f"node_{len(nodes) + 1}")) or (
            f"node_{len(nodes) + 1}"
        )
        speaker = str(raw_node.get("speaker") or "").strip() or None
        if speaker is not None and speaker not in speakers:
            matched = next((pid for pid in participant_ids if speaker in pid), None)
            speaker = matched  # unknown speakers surface via the structure check
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
    stem = _slug(raw)
    base = stem if stem.startswith(f"{prefix}_") else f"{prefix}_{stem or 'tree'}"
    candidate = base
    index = 2
    while candidate in used:
        candidate = f"{base}_{index}"
        index += 1
    return candidate


def _optional_slug(value: Any) -> str | None:
    if value is None:
        return None
    text = _slug(str(value))
    return text or None


def _slug(value: str) -> str:
    text = value.strip().lower()
    text = re.sub(r"[^a-z0-9㐀-鿿]+", "_", text)
    return text.strip("_")


class OfflineDialogueTreeProvider:
    """Deterministic 4-node tree (greeting -> choice -> two endings) for $0 pipeline tests."""

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
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


def _speakers_from_user(user: str) -> list[str]:
    match = re.search(r"Speakers:\s*(.+)", user)
    if not match:
        return []
    return [part.strip() for part in match.group(1).split(",") if part.strip()]


def _brief_from_user(user: str) -> str:
    match = re.search(r"Brief:\s*(.+)", user)
    return match.group(1).strip() if match else user.strip()[:40]
