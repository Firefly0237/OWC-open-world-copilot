"""Exact author-feedback locations; resolving a target does not grant edit permission.

The action layer must reject stale artifacts before calling this pure resolver. Existing
unaffected artifacts from earlier revisions may remain valid, so staleness is not inferred
from revision inequality here. No fuzzy text matching or fallback operation is permitted.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..content.hash import content_hash
from ..content.models import DialogueTree, Quest
from ..logic.stepper import PreviewSession, preview_view

if TYPE_CHECKING:
    from .models import Artifact, TaskRecord


class FeedbackAnchor(BaseModel):
    """A JSON Pointer into the exact immutable artifact the author was viewing."""

    model_config = ConfigDict(extra="forbid", strict=True)

    artifact_id: str = Field(min_length=1, max_length=128)
    artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    path: str = Field(min_length=1, max_length=2048)
    preview_id: str | None = Field(default=None, min_length=1, max_length=128)
    action_seq: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def paired_preview(self) -> Self:
        if (self.preview_id is None) != (self.action_seq is None):
            raise ValueError("预演锚点必须同时提供preview_id和action_seq")
        return self


def _pointer_tokens(path: str) -> list[str]:
    if not path.startswith("/"):
        raise ValueError("反馈路径必须是绝对JSON Pointer")
    parts = path[1:].split("/")
    if any(re.search(r"~(?![01])", part) for part in parts):
        raise ValueError("反馈路径包含非法JSON Pointer转义")
    return [part.replace("~1", "/").replace("~0", "~") for part in parts]


def _array_index(token: str) -> int:
    if not re.fullmatch(r"0|[1-9][0-9]*", token):
        raise ValueError("反馈路径的数组下标必须是规范非负整数")
    return int(token)


def _route(kind: str, tokens: list[str]) -> tuple[str, str]:
    if kind in {"quest", "quest_with_logic", "bound_dialogue"}:
        if tokens in (["quest", "title"], ["quest", "objective"]):
            return "draft_quest", "quest_text"
        if len(tokens) == 4 and tokens[:2] == ["quest", "stages"] and tokens[3] == "summary":
            _array_index(tokens[2])
            return "draft_quest", "quest_stage_text"
    if kind in {"dialogue_tree", "bound_dialogue"}:
        if len(tokens) == 4 and tokens[:2] == ["tree", "nodes"] and tokens[3] == "text":
            return "draft_dialogue", "dialogue_text"
        if (
            len(tokens) == 6
            and tokens[:2] == ["tree", "nodes"]
            and tokens[3] == "choices"
            and tokens[5] == "text"
        ):
            _array_index(tokens[4])
            return "draft_dialogue", "dialogue_choice_text"
    raise ValueError("反馈锚点只允许任务正文、阶段说明、对白正文或选择文本")


def _read(payload: dict[str, Any], tokens: list[str]) -> str:
    current: Any = payload
    for token in tokens:
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list):
            index = _array_index(token)
            if index >= len(current):
                raise ValueError("反馈锚点数组下标不存在")
            current = current[index]
        else:
            raise ValueError("反馈锚点字段不存在")
    if not isinstance(current, str):
        raise ValueError("反馈锚点必须指向现有字符串正文")
    return current


def _check_preview(
    task: TaskRecord,
    artifact: Artifact,
    anchor: FeedbackAnchor,
    tokens: list[str],
    target_kind: str,
    preview: dict[str, Any] | None,
) -> None:
    if preview is None:
        raise ValueError("找不到反馈锚点所指的预演记录")
    if preview.get("version_mode") == "fixed_historical":
        raise ValueError("旧版隔离试玩不能作为当前稿的反馈定位，请重新选择当前稿内容")
    if (
        preview.get("task_id") != task.id
        or preview.get("project") != task.project
        or preview.get("revision") != task.revision
        or preview.get("artifact_id") != artifact.id
    ):
        raise ValueError("预演不属于当前任务版本与指定产物")
    session = PreviewSession.model_validate(preview.get("session"))
    if session.id != anchor.preview_id or session.action_seq != anchor.action_seq:
        raise ValueError("预演位置已改变，请基于当前节点重新反馈")
    if target_kind.startswith("dialogue"):
        source = DialogueTree.model_validate(artifact.payload.get("tree"))
        if (
            session.phase != "dialogue"
            or session.dialogue_tree is None
            or content_hash(session.dialogue_tree) != content_hash(source)
            or session.cursor != tokens[2]
        ):
            raise ValueError("反馈对白不是当前预演所用原文或当前节点")
        if target_kind == "dialogue_choice_text":
            choice_id = f"{tokens[2]}:{tokens[4]}"
            if choice_id not in {row["id"] for row in preview_view(session)["choices"]}:
                raise ValueError("反馈选择不是当前预演可见选项")
    else:
        source_quest = Quest.model_validate(artifact.payload.get("quest"))
        if (
            target_kind != "quest_stage_text"
            or session.phase != "quest"
            or session.quest is None
            or content_hash(session.quest) != content_hash(source_quest)
            or session.cursor != source_quest.stages[_array_index(tokens[2])].id
        ):
            raise ValueError("反馈任务正文不是当前预演阶段；标题和目标请从正文区反馈")


def resolve_feedback_anchor(
    task: TaskRecord,
    artifact: Artifact,
    anchor: FeedbackAnchor | dict[str, Any],
    preview: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve source identity and routing, without mutating content or approving any change.

    ``preview`` is the existing persisted record with task/project/revision/artifact_id and
    ``session`` fields, not the public view or a text excerpt. The caller owns stale checks.
    """
    anchor = FeedbackAnchor.model_validate(anchor)
    if artifact.task_id != task.id or not 1 <= artifact.revision <= task.revision:
        raise ValueError("反馈产物不属于当前任务或版本")
    if artifact.id != anchor.artifact_id:
        raise ValueError("反馈产物ID与锚点不一致")
    if (
        artifact.content_hash != anchor.artifact_hash
        or content_hash(artifact.payload) != artifact.content_hash
    ):
        raise ValueError("反馈产物hash已改变，请重新读取正文")
    tokens = _pointer_tokens(anchor.path)
    operation, target_kind = _route(artifact.kind, tokens)
    target_text = _read(artifact.payload, tokens)
    if anchor.preview_id is not None:
        _check_preview(task, artifact, anchor, tokens, target_kind, preview)
    elif preview is not None:
        raise ValueError("传入预演记录时必须明确提供预演锚点")
    result = {
        "operation": operation,
        "target_kind": target_kind,
        "target_path": anchor.path,
        "target_text": target_text,
        "artifact_id": artifact.id,
        "artifact_revision": artifact.revision,
        "source_hash": artifact.content_hash,
        "task_id": task.id,
        "revision": task.revision,
    }
    if anchor.preview_id is not None:
        result.update(preview_id=anchor.preview_id, action_seq=anchor.action_seq)
    return result
