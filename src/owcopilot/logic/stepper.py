"""Serializable, explicit-choice narrative previews over the production logic semantics.

Starting or reading a preview never applies completion effects. An accepted action commits one
dialogue node or quest stage; an opted-in compiled player route also settles its already-selected
quest path without asking the player to select it again. The persistence boundary owns
compare-and-swap and artifact scope; these pure functions support JSON save/restore.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..content.models import DialogueTree, Effect, Quest
from .expr import parse_expr, refs_in
from .player_presentation import player_presentation_for
from .semantics import Value, WorldState, evaluate
from .simulate import _seed_state, _truthy

PreviewPhase = Literal["dialogue", "quest"]
PreviewStatus = Literal[
    "ready",
    "waiting_choice",
    "completed",
    "blocked",
    "deadlock",
    "cycle",
    "unavailable",
    "dangling",
    "unsupported",
    "truncated",
]


class QuestStageFrame(BaseModel):
    """A detached stage-completion projection, shared with the batch simulator."""

    precondition_ok: bool
    values: dict[str, Value]
    effects: list[str] = Field(default_factory=list)


def apply_effects(state: WorldState, effects: list[Effect]) -> list[str]:
    """Apply the domain Effect contract and return the existing human-readable trace."""
    descriptions: list[str] = []
    for effect in effects:
        state.apply(effect.var, effect.op, effect.value)
        descriptions.append(f"{effect.var} {effect.op} {effect.value}")
    return descriptions


def prepare_quest_stage(quest: Quest, stage_id: str, values: dict[str, Value]) -> QuestStageFrame:
    """Project completion effects once onto a copy; never mutate the caller's state."""
    state = WorldState(values)
    stage_logic = (
        next((row for row in quest.logic.stage_logic if row.stage_id == stage_id), None)
        if quest.logic
        else None
    )
    allowed = _truthy(state, stage_logic.precondition) if stage_logic else True
    effects = (
        apply_effects(state, stage_logic.effects_on_complete) if allowed and stage_logic else []
    )
    return QuestStageFrame(precondition_ok=allowed, values=state.as_mapping(), effects=effects)


class PreviewStep(BaseModel):
    phase: PreviewPhase
    object_id: str
    cursor: str
    choice_id: str | None = None
    effects: list[str] = Field(default_factory=list)


class PreviewStateChange(BaseModel):
    id: str
    name: str
    before: Value | None = None
    after: Value | None = None


class PreviewSession(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    quest: Quest | None = None
    dialogue_tree: DialogueTree | None = None
    phase: PreviewPhase = "quest"
    cursor: str | None = None
    status: PreviewStatus = "ready"
    values: dict[str, Value] = Field(default_factory=dict)
    action_seq: int = 0
    max_steps: int = Field(default=200, ge=1, le=100_000)
    history: list[PreviewStep] = Field(default_factory=list)
    applied_actions: dict[str, str | None] = Field(default_factory=dict)
    message: str = ""
    outcome: str = ""
    presentation: Literal["logic", "player"] = "logic"
    player_text: str = ""
    last_state_changes: list[PreviewStateChange] = Field(default_factory=list)


def start_preview(
    *,
    quest: Quest | None = None,
    dialogue_tree: DialogueTree | None = None,
    initial_state: dict[str, Value] | None = None,
    max_steps: int = 200,
    presentation: Literal["logic", "player"] = "logic",
) -> PreviewSession:
    """Freeze content and seed state; a bound dialogue runs before the optional quest."""
    from .dialogue import _seed

    if quest is None and dialogue_tree is None:
        raise ValueError("预演需要任务或对白。")
    if quest and dialogue_tree and dialogue_tree.quest_id not in (None, "", quest.id):
        raise ValueError("对白绑定的任务与预演任务不一致。")
    values = (
        _seed_state(quest.logic, initial_state).as_mapping()
        if quest and quest.logic
        else dict(initial_state or {})
    )
    if dialogue_tree:
        values = _seed(dialogue_tree, values).as_mapping()
    session = PreviewSession(
        quest=quest.model_copy(deep=True) if quest else None,
        dialogue_tree=dialogue_tree.model_copy(deep=True) if dialogue_tree else None,
        phase="dialogue" if dialogue_tree else "quest",
        cursor=dialogue_tree.root_node if dialogue_tree else None,
        values=values,
        max_steps=max_steps,
        presentation=presentation,
    )
    if not dialogue_tree:
        _begin_quest(session)
        if _compiled_player(session):
            session.status, session.message = "blocked", "这个场景需要绑定对白后才能试玩。"
    _refresh_status(session)
    return session


def _compiled_player(session: PreviewSession) -> bool:
    """Only marked compiler artifacts opt into the separate presentation channel."""
    return bool(
        session.presentation == "player"
        and session.quest
        and (
            "mechanic_ir_v2" in session.quest.metadata
            or session.dialogue_tree
            and (
                "player_presentation_v1" in session.dialogue_tree.metadata
                or "mechanic_ir_v2" in session.dialogue_tree.metadata
            )
        )
    )


def _strict_condition(values: dict[str, Value], source: str) -> bool:
    if not source.strip():
        return True
    expression = parse_expr(source)
    if refs_in(expression) - values.keys():
        raise ValueError("场景条件引用了尚未提供的状态，无法继续结算。")
    result = evaluate(expression, values)
    if type(result) is not bool:
        raise ValueError("场景条件必须返回是或否。")
    return result


def _settle_player_quest(session: PreviewSession) -> None:
    """Consume only the exact, uniquely legal route already selected in dialogue.

    Stage projection remains read-only; each committed stage goes through the
    existing execution function. The enclosing action retains its single CAS
    sequence/idempotency key, while history records every actual settlement.
    """
    if session.phase != "quest" or not _compiled_player(session):
        return
    assert session.quest is not None
    quest = session.quest
    try:
        contract = player_presentation_for(quest, session.dialogue_tree)
        if contract is None:
            raise ValueError("编译场景缺少试玩呈现绑定。")
        tree = session.dialogue_tree
        assert tree is not None
        assert quest.logic is not None
        if not _strict_condition(session.values, quest.logic.precondition):
            return  # _begin_quest already records the unavailable state.
        while session.status in {"ready", "waiting_choice"}:
            _refresh_status(session)
            if session.status not in {"ready", "waiting_choice"}:
                break
            position = session.cursor
            stage_logic = next(
                (row for row in quest.logic.stage_logic if row.stage_id == position), None
            )
            if stage_logic and not _strict_condition(session.values, stage_logic.precondition):
                raise ValueError("当前阶段的进入条件未满足，结算已暂停。")
            assert position is not None
            frame = prepare_quest_stage(quest, position, session.values)
            outgoing = [row for row in quest.logic.branches if row.from_stage == position]
            legal = [row for row in outgoing if _strict_condition(frame.values, row.condition)]
            if len(legal) != 1:
                raise ValueError(
                    "当前阶段有多条可用分支，无法自动结算。"
                    if legal
                    else "当前阶段没有可用分支，结算已暂停。"
                )
            branch = legal[0]
            if branch.id not in contract.automatic_branch_ids:
                selected = [
                    binding
                    for binding in contract.bindings
                    if binding.stage_id == position
                    and any(
                        step.phase == "dialogue"
                        and step.object_id == tree.id
                        and step.cursor == binding.node_id
                        and step.choice_id == f"{binding.node_id}:{binding.choice_index}"
                        for step in session.history
                    )
                ]
                if len(selected) != 1 or selected[0].branch_id != branch.id:
                    raise ValueError("当前分支没有对应的已选对白，结算已暂停。")
                binding = selected[0]
                if session.values.get(binding.selector_variable) != binding.option_value:
                    raise ValueError("已选对白与当前分支状态不一致，结算已暂停。")
            _advance_quest(session, branch.id)
            if session.status == "completed":
                session.player_text = contract.endings.get(branch.id, "")
            _refresh_status(session)
    except ValueError as error:
        session.status, session.message = "blocked", str(error)


def _begin_quest(session: PreviewSession) -> None:
    quest = session.quest
    if quest is None:
        session.cursor, session.status = None, "completed"
        return
    session.phase = "quest"
    if quest.logic is None:
        session.cursor, session.status = None, "unsupported"
        session.message = "任务缺少逻辑层；可阅读正文，不能据此声称逻辑预演通过。"
        return
    if not _truthy(WorldState(session.values), quest.logic.precondition):
        session.cursor, session.status = None, "unavailable"
        session.message = f"任务前置不满足：{quest.logic.precondition}"
        return
    session.cursor = quest.stages[0].id if quest.stages else None
    session.status = "ready" if session.cursor else "completed"


def _available_choices(session: PreviewSession) -> list[dict[str, Any]]:
    if session.phase == "dialogue":
        tree = session.dialogue_tree
        node = tree.nodes.get(session.cursor or "") if tree else None
        if node is None:
            return []
        state = WorldState(session.values)
        return [
            {"id": f"{node.id}:{index}", "label": choice.text, "target": choice.next_node}
            for index, choice in enumerate(node.choices)
            if _truthy(state, choice.condition)
        ]
    quest = session.quest
    if quest is None or quest.logic is None or session.cursor is None:
        return []
    frame = prepare_quest_stage(quest, session.cursor, session.values)
    if not frame.precondition_ok:
        return []
    state = WorldState(frame.values)
    summaries = {stage.id: stage.summary for stage in quest.stages}
    return [
        {
            "id": branch.id,
            "label": branch.outcome or summaries.get(branch.to_stage) or branch.id,
            "target": branch.to_stage or None,
        }
        for branch in quest.logic.branches
        if branch.from_stage == session.cursor and _truthy(state, branch.condition)
    ]


def _refresh_status(session: PreviewSession) -> None:
    if session.status not in {"ready", "waiting_choice"}:
        return
    if session.cursor is None:
        session.status = "completed"
        return
    if any(
        step.phase == session.phase and step.cursor == session.cursor for step in session.history
    ):
        session.status, session.message = "cycle", f"在节点「{session.cursor}」检测到循环"
        return
    if len(session.history) >= session.max_steps:
        session.status, session.message = "truncated", "预演已达到步数上限，尚未完成。"
        return
    if session.phase == "dialogue":
        if not session.dialogue_tree or session.cursor not in session.dialogue_tree.nodes:
            session.status, session.message = (
                "dangling",
                f"链接指向不存在的节点「{session.cursor}」",
            )
            return
    elif not session.quest or session.cursor not in {stage.id for stage in session.quest.stages}:
        session.status, session.message = "blocked", f"分支指向不存在的阶段「{session.cursor}」"
        return
    session.status = "waiting_choice" if _available_choices(session) else "ready"


def preview_view(session: PreviewSession | dict[str, Any]) -> dict[str, Any]:
    """Return a readable view without modifying the session or applying effects."""
    current = PreviewSession.model_validate(session)
    text, speaker_id = "", None
    if current.phase == "dialogue" and current.dialogue_tree:
        node = current.dialogue_tree.nodes.get(current.cursor or "")
        if node:
            text, speaker_id = node.text, node.speaker_id
    elif current.quest and not _compiled_player(current):
        stage = next((row for row in current.quest.stages if row.id == current.cursor), None)
        if stage:
            text = stage.summary
    if current.phase == "quest" and _compiled_player(current):
        text = current.player_text
    active = current.status in {"ready", "waiting_choice"}
    return {
        "session_id": current.id,
        "phase": current.phase,
        "cursor": current.cursor,
        "status": current.status,
        "action_seq": current.action_seq,
        "text": text,
        "speaker_id": speaker_id,
        "choices": _available_choices(current) if active else [],
        "state": dict(current.values),
        "history": [step.model_dump(mode="json") for step in current.history],
        "message": current.message,
        "outcome": current.player_text if _compiled_player(current) else current.outcome,
        "can_advance": active,
        "presentation": current.presentation,
        "state_names": {
            variable.id: variable.name or variable.id for variable in current.quest.logic.variables
        }
        if current.quest and current.quest.logic
        else {},
        "state_changes": [row.model_dump(mode="json") for row in current.last_state_changes],
    }


def advance_preview(
    session: PreviewSession | dict[str, Any],
    *,
    action_id: str,
    choice_id: str | None = None,
    expected_seq: int | None = None,
) -> PreviewSession:
    """Apply one action to a copy. Duplicate actions are no-ops; stale/new invalid actions fail.

    The persistence adapter must atomically CAS ``action_seq`` when storing this result. For an
    exact historical HTTP replay it can retain each response; this pure layer returns the latest
    cursor on a duplicate so an old replay cannot roll back more recent progress.
    """
    current = PreviewSession.model_validate(session)
    if not action_id.strip():
        raise ValueError("action_id不能为空。")
    if action_id in current.applied_actions:
        if current.applied_actions[action_id] != choice_id:
            raise ValueError("同一action_id不能用于不同选择。")
        return current.model_copy(deep=True)
    if expected_seq is not None and expected_seq != current.action_seq:
        raise ValueError("预演动作版本已过期，请读取当前状态。")
    if current.status not in {"ready", "waiting_choice"}:
        raise ValueError(f"预演已停止：{current.status}。")
    options = _available_choices(current)
    if options and choice_id is None:
        raise ValueError("请先选择当前可用选项。")
    if choice_id is not None and choice_id not in {option["id"] for option in options}:
        raise ValueError("该选择不属于当前可用选项。")

    result = current.model_copy(deep=True)
    if result.phase == "dialogue":
        _advance_dialogue(result, choice_id)
    else:
        _advance_quest(result, choice_id)
    _settle_player_quest(result)
    result.action_seq += 1
    result.applied_actions[action_id] = choice_id
    names = (
        {variable.id: variable.name or variable.id for variable in result.quest.logic.variables}
        if result.quest and result.quest.logic
        else {}
    )
    result.last_state_changes = [
        PreviewStateChange(
            id=key,
            name=names.get(key, key),
            before=current.values.get(key),
            after=result.values.get(key),
        )
        for key in sorted(current.values.keys() | result.values.keys())
        if not key.startswith("_owc_")
        and (
            current.values.get(key) != result.values.get(key)
            or type(current.values.get(key)) is not type(result.values.get(key))
        )
    ]
    _refresh_status(result)
    return result


def _advance_dialogue(session: PreviewSession, choice_id: str | None) -> None:
    tree = session.dialogue_tree
    assert tree is not None and session.cursor is not None
    node = tree.nodes[session.cursor]
    step = PreviewStep(phase="dialogue", object_id=tree.id, cursor=node.id, choice_id=choice_id)
    state = WorldState(session.values)
    if choice_id is not None:
        choice = node.choices[int(choice_id.rsplit(":", 1)[1])]
        step.effects = apply_effects(state, choice.effects)
        next_cursor = choice.next_node
    elif node.choices and node.next_node is None:
        session.status, session.message = "deadlock", f"节点「{node.id}」没有可满足的选项"
        next_cursor = node.id
    else:
        next_cursor = node.next_node
    session.values = state.as_mapping()
    session.history.append(step)
    session.cursor = next_cursor
    if next_cursor is None:
        _begin_quest(session)


def _advance_quest(session: PreviewSession, choice_id: str | None) -> None:
    quest = session.quest
    assert quest is not None and quest.logic is not None and session.cursor is not None
    position = session.cursor
    frame = prepare_quest_stage(quest, position, session.values)
    step = PreviewStep(phase="quest", object_id=quest.id, cursor=position, choice_id=choice_id)
    session.history.append(step)
    if not frame.precondition_ok:
        session.status, session.message = "blocked", f"阶段「{position}」的前置不满足"
        return
    state = WorldState(frame.values)
    step.effects = list(frame.effects)
    outgoing = [branch for branch in quest.logic.branches if branch.from_stage == position]
    if choice_id is not None:
        branch = next(branch for branch in outgoing if branch.id == choice_id)
        step.effects.extend(apply_effects(state, branch.effects))
        session.cursor = branch.to_stage or None
        if session.cursor is None:
            session.outcome = branch.outcome
    elif outgoing:
        session.status, session.message = "deadlock", f"阶段「{position}」没有可走的分支"
    else:
        stages = [stage.id for stage in quest.stages]
        index = stages.index(position)
        session.cursor = stages[index + 1] if index + 1 < len(stages) else None
    session.values = state.as_mapping()
    if session.cursor is None:
        session.status = "completed"
