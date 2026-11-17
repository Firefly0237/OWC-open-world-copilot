"""Contracts for the narrative studio, independent of HTTP and model providers."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal, Self
from uuid import uuid4

import networkx as nx
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator

from ..logic.temporal import TemporalSpec
from .anchors import FeedbackAnchor

Operation = Literal[
    "plan_events",
    "draft_quest",
    "draft_dialogue",
    "draft_logic",
    "bind_dialogue",
    "audit",
    "compare",
    "package",
]
NodeStatus = Literal[
    "running",
    "retrying",
    "waiting_user",
    "succeeded",
    "blocked_dependency",
    "skipped_retry_exhausted",
    "skipped_nonretryable",
    "cancelled",
]


def new_id() -> str:
    return uuid4().hex


def now() -> str:
    return datetime.now(UTC).isoformat()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeclaredTemporalSpec(TemporalSpec):
    """Explicit workflow rules reject fields the temporal checker does not consume."""

    model_config = ConfigDict(extra="forbid")


class ComparisonInput(Contract):
    initial_state: dict[str, StrictBool | StrictInt | StrictStr] = Field(default_factory=dict)
    formal_specs: list[DeclaredTemporalSpec] | None = None


class TaskInput(Contract):
    brief: str = Field(min_length=1, max_length=16000)
    protected: list[str] = Field(default_factory=list, max_length=30)
    reference_ids: list[str] = Field(default_factory=list, max_length=30)
    participation: Literal["delegate", "direction", "edit"] = "delegate"
    world_id: str = "default"
    version: str | None = None
    model: str = ""
    workflow_id: str | None = None
    use_references: bool = False
    comparison: ComparisonInput | None = None


class TaskCreateInput(TaskInput):
    """Presentation metadata is not part of the immutable generation request."""

    display_title: str = Field(default="", max_length=160)
    display_synopsis: str = Field(default="", max_length=1000)
    origin_kind: Literal["author", "development", "unspecified"] = "unspecified"


class WorkflowNode(Contract):
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    operation: Operation
    depends_on: list[str] = Field(default_factory=list)
    optional_dependencies: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    guidance: str = Field(default="", max_length=8000)


class WorkflowInputSlot(Contract):
    name: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$")
    source: Literal["brief", "protected", "entities", "quests", "reference_ids"]
    description: str = Field(default="", max_length=2000)


class WorkflowSpec(Contract):
    schema_version: int = 1
    name: str = Field(min_length=1, max_length=160)
    description: str = ""
    compile_notes: list[str] = Field(default_factory=list)
    applicability: list[str] = Field(default_factory=list, max_length=20)
    input_slots: list[WorkflowInputSlot] = Field(default_factory=list, max_length=20)
    method_source_ids: list[str] = Field(default_factory=list, max_length=8)
    nodes: list[WorkflowNode] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        slots = [slot.name for slot in self.input_slots]
        if len(set(slots)) != len(slots):
            raise ValueError("方法输入槽位名称重复")
        for node in self.nodes:
            used = set(re.findall(r"\{\{([a-zA-Z][a-zA-Z0-9_]*)\}\}", node.guidance))
            if used - set(slots):
                raise ValueError(
                    f"步骤{node.id}引用未声明的方法输入槽位：{sorted(used - set(slots))}"
                )
        ids = [node.id for node in self.nodes]
        if len(set(ids)) != len(ids):
            raise ValueError("工作图节点ID重复")
        operations = [node.operation for node in self.nodes]
        # A prototype task produces one narrative slice. Renaming a node cannot create a
        # second budget for the same logical output in this task revision.
        if len(set(operations)) != len(operations):
            raise ValueError("同一任务每种操作只允许一个逻辑节点")
        graph = nx.DiGraph()
        graph.add_nodes_from(ids)
        for node in self.nodes:
            parents = node.depends_on + node.optional_dependencies
            if len(set(parents)) != len(parents):
                raise ValueError(f"节点{node.id}依赖重复")
            for parent in parents:
                if parent not in ids:
                    raise ValueError(f"节点{node.id}引用不存在的依赖{parent}")
                graph.add_edge(parent, node.id)
        if not nx.is_directed_acyclic_graph(graph):
            raise ValueError("工作图必须为有向无环图")
        if "draft_quest" not in operations:
            raise ValueError("叙事原型工作图至少需要任务内容产物")
        return self


class Interpretation(Contract):
    goal: str
    assumptions: list[str] = Field(default_factory=list)
    protected: list[str] = Field(default_factory=list)


class CharacterGoal(Contract):
    character_id: str
    goal: str = Field(min_length=1, max_length=2000)
    stakes: str = Field(min_length=1, max_length=2000)


class NarrativeEvent(Contract):
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    summary: str = Field(min_length=1, max_length=2000)
    participant_ids: list[str] = Field(default_factory=list, max_length=20)
    causes: list[str] = Field(default_factory=list, max_length=20)
    source_refs: list[str] = Field(default_factory=list, max_length=20)


class NarrativeDecision(Contract):
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    event_id: str
    options: list[str] = Field(min_length=2, max_length=6)
    consequences: list[str] = Field(min_length=2, max_length=6)

    @model_validator(mode="after")
    def pair_options(self) -> Self:
        if len(self.options) != len(self.consequences):
            raise ValueError("每个剧情选项必须有对应的预期后果")
        return self


class EventPlan(Contract):
    premise: str
    beats: list[str] = Field(min_length=1, max_length=12)
    choices: list[str] = Field(default_factory=list)
    participant_ids: list[str] = Field(default_factory=list)
    contract_version: int = Field(default=0, ge=0, le=1)
    conflict: str = ""
    character_goals: list[CharacterGoal] = Field(default_factory=list, max_length=20)
    protected_facts: list[str] = Field(default_factory=list, max_length=30)
    events: list[NarrativeEvent] = Field(default_factory=list, max_length=20)
    decisions: list[NarrativeDecision] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_events(self) -> Self:
        ids = [event.id for event in self.events]
        if len(set(ids)) != len(ids):
            raise ValueError("叙事事件ID重复")
        graph = nx.DiGraph()
        graph.add_nodes_from(ids)
        for event in self.events:
            if set(event.causes) - set(ids):
                raise ValueError(f"事件{event.id}引用不存在的前因")
            graph.add_edges_from((parent, event.id) for parent in event.causes)
        if not nx.is_directed_acyclic_graph(graph):
            raise ValueError("叙事事件因果关系包含循环")
        if any(decision.event_id not in ids for decision in self.decisions):
            raise ValueError("剧情决策引用不存在的事件")
        return self


class TaskRecord(Contract):
    id: str = Field(default_factory=new_id)
    project: str
    request: TaskInput
    revision: int = 1
    base_version: str
    base_hash: str
    base_bundle: dict[str, Any]
    feedback: list[dict[str, Any]] = Field(default_factory=list)
    preserved_budgets: dict[str, str] = Field(default_factory=dict)
    created_at: str = Field(default_factory=now)
    display_title: str = Field(default="", max_length=160)
    display_synopsis: str = Field(default="", max_length=1000)
    origin_kind: Literal["author", "development", "unspecified"] = "unspecified"


class Artifact(Contract):
    id: str = Field(default_factory=new_id)
    task_id: str
    revision: int
    run_id: str
    node_id: str
    kind: str
    payload: dict[str, Any]
    input_hash: str
    content_hash: str
    created_at: str = Field(default_factory=now)


class NodeOutcome(Contract):
    node_id: str
    operation: str
    status: NodeStatus
    attempts: int = 0
    budget_id: str
    artifact_id: str | None = None
    error: str = ""


class RunRecord(Contract):
    id: str = Field(default_factory=new_id)
    task_id: str
    revision: int
    status: str = "queued"
    job_id: str | None = None
    learning_job_id: str | None = None
    cancel_requested: bool = False
    workflow: WorkflowSpec | None = None
    workflow_source: str = "new"
    budget_overrides: dict[str, str] = Field(default_factory=dict)
    budget_authorization_note: str = ""
    waiting_question: str = ""
    direction_confirmed: bool = False
    error: str = ""
    created_at: str = Field(default_factory=now)


class FeedbackInput(Contract):
    text: str = Field(min_length=1, max_length=8000)
    expected_revision: int = Field(ge=1)
    scope: Literal["content", "project", "workflow"] = "content"
    target_operation: str | None = None
    anchor: FeedbackAnchor | None = None


class PreviewInput(Contract):
    initial_state: dict[str, bool | int | str] = Field(default_factory=dict)
    artifact_id: str | None = None
    quest_id: str | None = None
    dialogue_tree_id: str | None = None
    compose_dialogue: bool = True


class PreviewStepInput(Contract):
    action_id: str = Field(min_length=1, max_length=128)
    choice_id: str | None = None
    expected_seq: int = Field(ge=0)


class SaveWorkflowInput(Contract):
    name: str = Field(min_length=1, max_length=160)
    run_id: str
    scope: Literal["project", "personal"] = "project"


class LearnMethodInput(Contract):
    expected_revision: int = Field(ge=1)
    name: str = Field(default="", max_length=160)


class RevisionInput(Contract):
    expected_hash: str
    payload: dict[str, Any]


class RestoreArtifactInput(Contract):
    expected_revision: int = Field(ge=1)
    source_hash: str


class WorkflowPatchInput(Contract):
    acceptance_status: Literal["pending", "accepted", "rejected"] | None = None
    disabled: bool | None = None
    expected_hash: str | None = None
    name: str | None = Field(default=None, min_length=1, max_length=160)
    spec: WorkflowSpec | None = None
