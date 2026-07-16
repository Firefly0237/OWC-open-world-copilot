# UE DataTable 导出器：ContentBundle -> 可导入引擎的 row-array JSON（recognize 的写侧对偶）。
"""Unreal Engine **DataTable** exporter — the write-side counterpart of ``recognize_engine_data``.

The importable DataTable JSON is a *top-level array of row objects*: each row carries a ``"Name"``
key (the RowName) plus one key per struct property, and the DataTable's Row Struct must derive
``FTableRowBase``. References serialize as ``FDataTableRowHandle`` objects (``{"RowName": "X"}``)
or, for arrays, JSON arrays of those; a bare ``FString`` field whose value is another row id is a
foreign key too. This is exactly the shape ``recognize/engine_data.py`` reads back (``_ID_KEYS``
ue = ``("Name", "RowName")``), so ``export -> re-recognize`` is a closed round-trip.

Localization: an ``FText`` field serializes as a plain JSON string. Importing a bare string yields a
culture-invariant/inline ``FText`` that the loc pipeline does **not** reliably gather, so real
localization references a **StringTable** by key. We therefore reuse the SAME ``text_key`` that
``exporters/xliff.py`` already emits — no strings are duplicated inside the DataTable — and ship a
companion ``<table>_strings.csv`` (the Epic StringTable CSV shape: two mandatory columns
``Key,SourceString``).

Field names are **supplied, not guessed**: ``row_struct_map`` maps our canonical field names to the
consuming project's struct EXPORT names, and a contract *preflight* validates the result (reserved
``Name`` collision, spaces, non-identifier characters, dangling references). Export names are the
JSON keys; for a ``bool`` field the C++ member re-adds the leading ``b`` (``bIsActive`` <-> export
``IsActive``), matching ``get_data_table_column_export_names``.

The whole path is deterministic and LLM-free (offline, $0).

!!! note "Needs a real UE machine to confirm"
    In ``text_mode="stringtable"`` we write the ``text_key`` as the field value and ship the
    StringTable CSV, but the *exact* JSON literal that binds an ``FText`` field to a StringTable
    (bare key vs ``NSLOCTEXT`` vs a struct form) is engine-version dependent and cannot be verified
    offline. Preflight surfaces this as an honest ``stringtable_literal_unverified`` warning (same
    "live run pending" discipline as the P3 notes). ``text_mode="inline"`` writes the source string
    directly (imports as an inline ``FText``) and carries no unverified literal.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..content.models import ContentBundle, Effect
from .models import ExportedFile

HandleStyle = Literal["rowhandle", "string"]
TextMode = Literal["inline", "stringtable"]

# The row key ("Name") and the classification key ("Type") are structural: recognize_engine_data
# reads them by these exact spellings, so they are never remapped by row_struct_map.
_RESERVED_KEYS = frozenset({"Name", "Type"})
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class UEType(str, Enum):
    """The UE property types the exporter emits — used for the generated row-struct header."""

    FTEXT = "FText"
    FSTRING = "FString"
    INT32 = "int32"
    FLOAT = "float"
    BOOL = "bool"
    ROW_HANDLE = "FDataTableRowHandle"
    ROW_HANDLE_ARRAY = "TArray<FDataTableRowHandle>"
    STRING_ARRAY = "TArray<FString>"


class _Role(str, Enum):
    CONSTANT = "constant"  # a fixed classification value (e.g. Type="quest")
    SCALAR = "scalar"  # a plain FText/FString/int/float attribute
    SCALAR_LIST = "scalar_list"  # a list of literal strings -> TArray<FString> (NOT references)
    REFERENCE = "reference"  # a single id -> RowHandle or bare string
    REFERENCE_LIST = "reference_list"  # a list of ids -> array of RowHandles/strings
    LOCALIZED = "localized"  # an FText with a text_key -> StringTable/inline


@dataclass(frozen=True)
class _FieldSpec:
    name: str  # canonical field name (the JSON key before row_struct_map)
    ue_type: UEType
    role: _Role
    extract: Callable[[Any], Any]


@dataclass(frozen=True)
class _TableSpec:
    key: str  # e.g. "quests"
    struct_name: str  # e.g. "QuestRow" -> emitted as FQuestRow
    filename: str  # e.g. "Quests.json"
    select: Callable[[ContentBundle], Iterable[Any]]
    id_of: Callable[[Any], str]
    fields: tuple[_FieldSpec, ...]
    relation_groups: bool = False  # entities: append one TArray<RowHandle> column per relation kind


# --- table specs (data-driven, so new tables are declared here only) ---------------------------

_QUEST_FIELDS: tuple[_FieldSpec, ...] = (
    _FieldSpec("Type", UEType.FSTRING, _Role.CONSTANT, lambda _q: "quest"),
    _FieldSpec("DisplayName", UEType.FTEXT, _Role.SCALAR, lambda q: q.title),
    _FieldSpec("Objective", UEType.FTEXT, _Role.SCALAR, lambda q: q.objective),
    _FieldSpec("GiverNPC", UEType.ROW_HANDLE, _Role.REFERENCE, lambda q: q.giver_npc),
    _FieldSpec("Location", UEType.ROW_HANDLE, _Role.REFERENCE, lambda q: q.location),
    _FieldSpec(
        "Prerequisites", UEType.ROW_HANDLE_ARRAY, _Role.REFERENCE_LIST, lambda q: q.prerequisites
    ),
    _FieldSpec(
        "DialogueRefs", UEType.ROW_HANDLE_ARRAY, _Role.REFERENCE_LIST, lambda q: q.dialogue_refs
    ),
    _FieldSpec("TimelineOrder", UEType.INT32, _Role.SCALAR, lambda q: q.timeline_order),
)

_ENTITY_FIELDS: tuple[_FieldSpec, ...] = (
    _FieldSpec("Type", UEType.FSTRING, _Role.CONSTANT, lambda e: e.type.value),
    _FieldSpec("DisplayName", UEType.FTEXT, _Role.SCALAR, lambda e: e.name),
    _FieldSpec("Description", UEType.FTEXT, _Role.SCALAR, lambda e: e.description),
    _FieldSpec("Status", UEType.FSTRING, _Role.SCALAR, lambda e: e.status),
)

_DIALOGUE_FIELDS: tuple[_FieldSpec, ...] = (
    _FieldSpec("Type", UEType.FSTRING, _Role.CONSTANT, lambda _d: "dialogue"),
    _FieldSpec("Speaker", UEType.ROW_HANDLE, _Role.REFERENCE, lambda d: d.speaker_id),
    _FieldSpec("Quest", UEType.ROW_HANDLE, _Role.REFERENCE, lambda d: d.quest_id),
    _FieldSpec("Text", UEType.FTEXT, _Role.LOCALIZED, lambda d: (d.text_key, d.text, d.locale)),
)

# --- full-fidelity logic/stage/reward/dialogue-tree sub-tables ---------------------------------
#
# UE DataTable rows are flat, so a quest's nested logic (stages, variables, branches, rewards) and a
# dialogue tree's node/choice graph are decomposed into *child sub-tables* whose rows carry a
# ``FDataTableRowHandle`` foreign key back to the parent (the miHoYo QuestExcelConfigData /
# nextDialogs pointer-graph pattern). Intra-graph pointers (branch from/to stage, node/choice
# next_node, tree root_node) are RowHandles into the sibling sub-table, using the composite RowName
# below — this is the branch-reachability graph the deterministic audit walks.
#
# Two field families stay *string columns* rather than sub-tables, both sanctioned:
#   * boolean-expression sources (precondition/condition) are arbitrary-depth expressions;
#   * effect lists are encoded as a compact JSON array of ``{var, op, value}`` — the SAME field
#     vocabulary the C# ``EffectDto`` / ``content_bundle.json`` use (``Effect.model_dump``), so a
#     UE-side parser reconstructs the exact effect a stage/branch/choice applies. An ``Effect`` has
#     no id, so it cannot be a keyed sub-table row without fabricating surrogate ids for every one.

_COMPOSITE_SEP = "::"  # parent<sep>child RowName; child is recovered by stripping the known prefix.


def _composite(parent: str, child: str) -> str:
    return f"{parent}{_COMPOSITE_SEP}{child}"


def _encode_effects(effects: list[Effect]) -> str:
    """Effects -> compact JSON array of ``{var, op, value}`` (the C#/simulator vocabulary); '' when
    empty so the column is omitted (SCALAR rule)."""
    if not effects:
        return ""
    return json.dumps(
        [e.model_dump(mode="json") for e in effects],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _encode_json(value: object) -> str:
    """A dict/struct -> compact deterministic JSON; '' for empty/None so the column is omitted."""
    if not value:
        return ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _encode_logic_value(value: bool | int | str) -> str:
    """A LogicVar.default / typed literal -> JSON scalar so the bool|int|str union round-trips
    (``false`` / ``0`` / ``"foo"``) instead of collapsing to an ambiguous ``str()``."""
    return json.dumps(value, ensure_ascii=False)


@dataclass(frozen=True)
class _StageRow:
    """A quest stage joined with its StageLogic (both keyed by stage_id). Driven by the UNION of
    ``quest.stages`` and ``quest.logic.stage_logic`` so a logic-only stage is never dropped."""

    quest_id: str
    ordinal: int
    stage_id: str
    summary: str
    location: str | None
    required_entities: list[str]
    precondition: str
    effects_on_complete: list[Effect]


@dataclass(frozen=True)
class _VarRow:
    quest_id: str
    var_id: str
    name: str
    type: str
    default: bool | int | str
    enum_values: list[str]


@dataclass(frozen=True)
class _BranchRow:
    quest_id: str
    branch_id: str
    from_stage: str
    to_stage: str
    condition: str
    outcome: str
    effects: list[Effect]


@dataclass(frozen=True)
class _RewardRow:
    quest_id: str
    ordinal: int
    kind: str
    value: str
    amount: int | None


@dataclass(frozen=True)
class _LogicHeaderRow:
    quest_id: str
    precondition: str
    unlocks: list[str]


@dataclass(frozen=True)
class _NodeRow:
    tree_id: str
    node_id: str
    speaker_id: str | None
    text: str
    next_node: str | None


@dataclass(frozen=True)
class _ChoiceRow:
    tree_id: str
    node_id: str
    ordinal: int
    text: str
    next_node: str | None
    condition: str
    effects: list[Effect]


def _iter_stage_rows(bundle: ContentBundle) -> Iterable[_StageRow]:
    for quest in bundle.quests.values():
        logic = quest.logic
        by_id = {sl.stage_id: sl for sl in (logic.stage_logic if logic else [])}
        ordinal = 0
        seen: set[str] = set()
        for stage in quest.stages:
            sl = by_id.get(stage.id)
            yield _StageRow(
                quest_id=quest.id,
                ordinal=ordinal,
                stage_id=stage.id,
                summary=stage.summary,
                location=stage.location,
                required_entities=list(stage.required_entities),
                precondition=sl.precondition if sl else "",
                effects_on_complete=list(sl.effects_on_complete) if sl else [],
            )
            seen.add(stage.id)
            ordinal += 1
        for sl in logic.stage_logic if logic else []:
            if sl.stage_id in seen:
                continue
            # StageLogic with no matching QuestStage (audit flags it dangling) — still exported so
            # the logic layer is fully represented.
            yield _StageRow(
                quest_id=quest.id,
                ordinal=ordinal,
                stage_id=sl.stage_id,
                summary="",
                location=None,
                required_entities=[],
                precondition=sl.precondition,
                effects_on_complete=list(sl.effects_on_complete),
            )
            seen.add(sl.stage_id)
            ordinal += 1


def _iter_var_rows(bundle: ContentBundle) -> Iterable[_VarRow]:
    for quest in bundle.quests.values():
        if quest.logic is None:
            continue
        for var in quest.logic.variables:
            yield _VarRow(
                quest_id=quest.id,
                var_id=var.id,
                name=var.name,
                type=var.type.value,
                default=var.default,
                enum_values=list(var.enum_values),
            )


def _iter_branch_rows(bundle: ContentBundle) -> Iterable[_BranchRow]:
    for quest in bundle.quests.values():
        if quest.logic is None:
            continue
        for branch in quest.logic.branches:
            yield _BranchRow(
                quest_id=quest.id,
                branch_id=branch.id,
                from_stage=branch.from_stage,
                to_stage=branch.to_stage,
                condition=branch.condition,
                outcome=branch.outcome,
                effects=list(branch.effects),
            )


def _iter_reward_rows(bundle: ContentBundle) -> Iterable[_RewardRow]:
    for quest in bundle.quests.values():
        for ordinal, reward in enumerate(quest.rewards):
            yield _RewardRow(
                quest_id=quest.id,
                ordinal=ordinal,
                kind=reward.kind,
                value=reward.value,
                amount=reward.amount,
            )


def _iter_logic_header_rows(bundle: ContentBundle) -> Iterable[_LogicHeaderRow]:
    for quest in bundle.quests.values():
        logic = quest.logic
        if logic is None:
            continue
        # Quest-level logic scalars (precondition gate + unlocks). Variables/stages/branches ride
        # their own sub-tables; emit a header row only when there is a quest-level scalar to carry.
        if not logic.precondition and not logic.unlocks:
            continue
        yield _LogicHeaderRow(
            quest_id=quest.id, precondition=logic.precondition, unlocks=list(logic.unlocks)
        )


def _iter_tree_node_rows(bundle: ContentBundle) -> Iterable[_NodeRow]:
    for tree in bundle.dialogue_trees.values():
        for node in tree.nodes.values():
            yield _NodeRow(
                tree_id=tree.id,
                node_id=node.id,
                speaker_id=node.speaker_id,
                text=node.text,
                next_node=node.next_node,
            )


def _iter_tree_choice_rows(bundle: ContentBundle) -> Iterable[_ChoiceRow]:
    for tree in bundle.dialogue_trees.values():
        for node in tree.nodes.values():
            for ordinal, choice in enumerate(node.choices):
                yield _ChoiceRow(
                    tree_id=tree.id,
                    node_id=node.id,
                    ordinal=ordinal,
                    text=choice.text,
                    next_node=choice.next_node,
                    condition=choice.condition,
                    effects=list(choice.effects),
                )


def _node_ref(tree_id: str, node_id: str | None) -> str | None:
    return _composite(tree_id, node_id) if node_id else None


def _stage_ref(quest_id: str, stage_id: str) -> str | None:
    return _composite(quest_id, stage_id) if stage_id else None


_QUEST_STAGE_FIELDS: tuple[_FieldSpec, ...] = (
    _FieldSpec("Type", UEType.FSTRING, _Role.CONSTANT, lambda _s: "quest_stage"),
    _FieldSpec("Quest", UEType.ROW_HANDLE, _Role.REFERENCE, lambda s: s.quest_id),
    _FieldSpec("StageId", UEType.FSTRING, _Role.SCALAR, lambda s: s.stage_id),
    _FieldSpec("Ordinal", UEType.INT32, _Role.SCALAR, lambda s: s.ordinal),
    _FieldSpec("Summary", UEType.FTEXT, _Role.SCALAR, lambda s: s.summary),
    _FieldSpec("Location", UEType.ROW_HANDLE, _Role.REFERENCE, lambda s: s.location),
    _FieldSpec(
        "RequiredEntities",
        UEType.ROW_HANDLE_ARRAY,
        _Role.REFERENCE_LIST,
        lambda s: s.required_entities,
    ),
    _FieldSpec("Precondition", UEType.FSTRING, _Role.SCALAR, lambda s: s.precondition),
    _FieldSpec(
        "EffectsOnComplete",
        UEType.FSTRING,
        _Role.SCALAR,
        lambda s: _encode_effects(s.effects_on_complete),
    ),
)

_QUEST_LOGIC_VAR_FIELDS: tuple[_FieldSpec, ...] = (
    _FieldSpec("Type", UEType.FSTRING, _Role.CONSTANT, lambda _v: "quest_logic_var"),
    _FieldSpec("Quest", UEType.ROW_HANDLE, _Role.REFERENCE, lambda v: v.quest_id),
    _FieldSpec("VarId", UEType.FSTRING, _Role.SCALAR, lambda v: v.var_id),
    _FieldSpec("VarName", UEType.FSTRING, _Role.SCALAR, lambda v: v.name),
    _FieldSpec("VarType", UEType.FSTRING, _Role.SCALAR, lambda v: v.type),
    _FieldSpec(
        "DefaultValue", UEType.FSTRING, _Role.SCALAR, lambda v: _encode_logic_value(v.default)
    ),
    _FieldSpec("EnumValues", UEType.STRING_ARRAY, _Role.SCALAR_LIST, lambda v: v.enum_values),
)

_QUEST_LOGIC_BRANCH_FIELDS: tuple[_FieldSpec, ...] = (
    _FieldSpec("Type", UEType.FSTRING, _Role.CONSTANT, lambda _b: "quest_logic_branch"),
    _FieldSpec("Quest", UEType.ROW_HANDLE, _Role.REFERENCE, lambda b: b.quest_id),
    _FieldSpec("BranchId", UEType.FSTRING, _Role.SCALAR, lambda b: b.branch_id),
    _FieldSpec(
        "FromStage",
        UEType.ROW_HANDLE,
        _Role.REFERENCE,
        lambda b: _stage_ref(b.quest_id, b.from_stage),
    ),
    _FieldSpec(
        "ToStage", UEType.ROW_HANDLE, _Role.REFERENCE, lambda b: _stage_ref(b.quest_id, b.to_stage)
    ),
    _FieldSpec("Condition", UEType.FSTRING, _Role.SCALAR, lambda b: b.condition),
    _FieldSpec("Outcome", UEType.FSTRING, _Role.SCALAR, lambda b: b.outcome),
    _FieldSpec("Effects", UEType.FSTRING, _Role.SCALAR, lambda b: _encode_effects(b.effects)),
)

_QUEST_REWARD_FIELDS: tuple[_FieldSpec, ...] = (
    _FieldSpec("Type", UEType.FSTRING, _Role.CONSTANT, lambda _r: "quest_reward"),
    _FieldSpec("Quest", UEType.ROW_HANDLE, _Role.REFERENCE, lambda r: r.quest_id),
    _FieldSpec("Ordinal", UEType.INT32, _Role.SCALAR, lambda r: r.ordinal),
    _FieldSpec("RewardKind", UEType.FSTRING, _Role.SCALAR, lambda r: r.kind),
    _FieldSpec("RewardValue", UEType.FSTRING, _Role.SCALAR, lambda r: r.value),
    _FieldSpec("Amount", UEType.INT32, _Role.SCALAR, lambda r: r.amount),
)

_QUEST_LOGIC_FIELDS: tuple[_FieldSpec, ...] = (
    _FieldSpec("Type", UEType.FSTRING, _Role.CONSTANT, lambda _l: "quest_logic"),
    _FieldSpec("Quest", UEType.ROW_HANDLE, _Role.REFERENCE, lambda logic: logic.quest_id),
    _FieldSpec("Precondition", UEType.FSTRING, _Role.SCALAR, lambda logic: logic.precondition),
    _FieldSpec(
        "Unlocks", UEType.ROW_HANDLE_ARRAY, _Role.REFERENCE_LIST, lambda logic: logic.unlocks
    ),
)

_DIALOGUE_TREE_FIELDS: tuple[_FieldSpec, ...] = (
    _FieldSpec("Type", UEType.FSTRING, _Role.CONSTANT, lambda _t: "dialogue_tree"),
    _FieldSpec("Title", UEType.FTEXT, _Role.SCALAR, lambda t: t.title),
    _FieldSpec("Quest", UEType.ROW_HANDLE, _Role.REFERENCE, lambda t: t.quest_id),
    _FieldSpec(
        "Participants", UEType.ROW_HANDLE_ARRAY, _Role.REFERENCE_LIST, lambda t: t.participants
    ),
    _FieldSpec(
        "RootNode", UEType.ROW_HANDLE, _Role.REFERENCE, lambda t: _node_ref(t.id, t.root_node)
    ),
    _FieldSpec("Tags", UEType.STRING_ARRAY, _Role.SCALAR_LIST, lambda t: t.tags),
    _FieldSpec("Metadata", UEType.FSTRING, _Role.SCALAR, lambda t: _encode_json(t.metadata)),
    _FieldSpec("Origin", UEType.FSTRING, _Role.SCALAR, lambda t: t.origin.value),
    _FieldSpec("ReviewStatus", UEType.FSTRING, _Role.SCALAR, lambda t: t.review_status.value),
    _FieldSpec(
        "SourceRef",
        UEType.FSTRING,
        _Role.SCALAR,
        lambda t: _encode_json(t.source_ref.model_dump(mode="json") if t.source_ref else None),
    ),
)

_DIALOGUE_TREE_NODE_FIELDS: tuple[_FieldSpec, ...] = (
    _FieldSpec("Type", UEType.FSTRING, _Role.CONSTANT, lambda _n: "dialogue_node"),
    _FieldSpec("Tree", UEType.ROW_HANDLE, _Role.REFERENCE, lambda n: n.tree_id),
    _FieldSpec("NodeId", UEType.FSTRING, _Role.SCALAR, lambda n: n.node_id),
    _FieldSpec("Speaker", UEType.ROW_HANDLE, _Role.REFERENCE, lambda n: n.speaker_id),
    _FieldSpec("Text", UEType.FTEXT, _Role.SCALAR, lambda n: n.text),
    _FieldSpec(
        "NextNode", UEType.ROW_HANDLE, _Role.REFERENCE, lambda n: _node_ref(n.tree_id, n.next_node)
    ),
)

_DIALOGUE_TREE_CHOICE_FIELDS: tuple[_FieldSpec, ...] = (
    _FieldSpec("Type", UEType.FSTRING, _Role.CONSTANT, lambda _c: "dialogue_choice"),
    _FieldSpec(
        "Node", UEType.ROW_HANDLE, _Role.REFERENCE, lambda c: _composite(c.tree_id, c.node_id)
    ),
    _FieldSpec("Ordinal", UEType.INT32, _Role.SCALAR, lambda c: c.ordinal),
    _FieldSpec("Text", UEType.FTEXT, _Role.SCALAR, lambda c: c.text),
    _FieldSpec(
        "NextNode", UEType.ROW_HANDLE, _Role.REFERENCE, lambda c: _node_ref(c.tree_id, c.next_node)
    ),
    _FieldSpec("Condition", UEType.FSTRING, _Role.SCALAR, lambda c: c.condition),
    _FieldSpec("Effects", UEType.FSTRING, _Role.SCALAR, lambda c: _encode_effects(c.effects)),
)


_TABLE_SPECS: tuple[_TableSpec, ...] = (
    _TableSpec(
        key="quests",
        struct_name="QuestRow",
        filename="Quests.json",
        select=lambda b: b.quests.values(),
        id_of=lambda q: q.id,
        fields=_QUEST_FIELDS,
    ),
    _TableSpec(
        key="entities",
        struct_name="EntityRow",
        filename="Entities.json",
        select=lambda b: b.entities.values(),
        id_of=lambda e: e.id,
        fields=_ENTITY_FIELDS,
        relation_groups=True,
    ),
    _TableSpec(
        key="dialogues",
        struct_name="DialogueRow",
        filename="Dialogues.json",
        select=lambda b: b.dialogues.values(),
        id_of=lambda d: d.id,
        fields=_DIALOGUE_FIELDS,
    ),
    _TableSpec(
        key="quest_stages",
        struct_name="QuestStageRow",
        filename="QuestStages.json",
        select=_iter_stage_rows,
        id_of=lambda s: _composite(s.quest_id, s.stage_id),
        fields=_QUEST_STAGE_FIELDS,
    ),
    _TableSpec(
        key="quest_logic_vars",
        struct_name="QuestLogicVarRow",
        filename="QuestLogicVars.json",
        select=_iter_var_rows,
        id_of=lambda v: _composite(v.quest_id, f"var{_COMPOSITE_SEP}{v.var_id}"),
        fields=_QUEST_LOGIC_VAR_FIELDS,
    ),
    _TableSpec(
        key="quest_logic_branches",
        struct_name="QuestLogicBranchRow",
        filename="QuestLogicBranches.json",
        select=_iter_branch_rows,
        id_of=lambda b: _composite(b.quest_id, f"branch{_COMPOSITE_SEP}{b.branch_id}"),
        fields=_QUEST_LOGIC_BRANCH_FIELDS,
    ),
    _TableSpec(
        key="quest_rewards",
        struct_name="QuestRewardRow",
        filename="QuestRewards.json",
        select=_iter_reward_rows,
        id_of=lambda r: _composite(r.quest_id, f"reward{_COMPOSITE_SEP}{r.ordinal}"),
        fields=_QUEST_REWARD_FIELDS,
    ),
    _TableSpec(
        key="quest_logic",
        struct_name="QuestLogicRow",
        filename="QuestLogic.json",
        select=_iter_logic_header_rows,
        id_of=lambda logic: _composite(logic.quest_id, "logic"),
        fields=_QUEST_LOGIC_FIELDS,
    ),
    _TableSpec(
        key="dialogue_trees",
        struct_name="DialogueTreeRow",
        filename="DialogueTrees.json",
        select=lambda b: b.dialogue_trees.values(),
        id_of=lambda t: t.id,
        fields=_DIALOGUE_TREE_FIELDS,
    ),
    _TableSpec(
        key="dialogue_tree_nodes",
        struct_name="DialogueTreeNodeRow",
        filename="DialogueTreeNodes.json",
        select=_iter_tree_node_rows,
        id_of=lambda n: _composite(n.tree_id, n.node_id),
        fields=_DIALOGUE_TREE_NODE_FIELDS,
    ),
    _TableSpec(
        key="dialogue_tree_choices",
        struct_name="DialogueTreeChoiceRow",
        filename="DialogueTreeChoices.json",
        select=_iter_tree_choice_rows,
        id_of=lambda c: _composite(
            _composite(c.tree_id, c.node_id), f"choice{_COMPOSITE_SEP}{c.ordinal}"
        ),
        fields=_DIALOGUE_TREE_CHOICE_FIELDS,
    ),
)

_HEADER_FILENAME = "OWCopilotRows.h"
_PREFLIGHT_FILENAME = "unreal_preflight.json"


# --- preflight (the contract) ------------------------------------------------------------------


class PreflightSeverity(str, Enum):
    ERROR = "error"  # would break the UE import -> blocks unless force=True
    WARNING = "warning"  # honest heads-up (dangling ref, unverified literal) -> never blocks


class PreflightIssue(BaseModel):
    severity: PreflightSeverity
    code: str
    table: str = ""
    field: str = ""
    message: str


class PreflightReport(BaseModel):
    issues: list[PreflightIssue] = Field(default_factory=list)

    @property
    def errors(self) -> list[PreflightIssue]:
        return [i for i in self.issues if i.severity is PreflightSeverity.ERROR]

    @property
    def warnings(self) -> list[PreflightIssue]:
        return [i for i in self.issues if i.severity is PreflightSeverity.WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors


class UnrealExportResult(BaseModel):
    output_dir: str
    files: list[ExportedFile] = Field(default_factory=list)
    preflight: PreflightReport
    tables: list[str] = Field(default_factory=list)  # DataTable json filenames written
    string_tables: list[str] = Field(default_factory=list)  # <table>_strings.csv filenames written


class UnrealExportError(Exception):
    """Raised when preflight finds blocking (ERROR) contract violations and ``force`` is not set.

    Carries a guided message (what is wrong + how to fix) plus the full report, so callers surface a
    friendly next-step instead of a raw traceback."""

    def __init__(self, report: PreflightReport) -> None:
        self.report = report
        lines = [f"  - [{i.table}.{i.field or '?'}] {i.message}" for i in report.errors]
        super().__init__(
            "UE DataTable 契约预检未通过，已中止导出（未写任何文件）：\n"
            + "\n".join(lines)
            + "\n修复：用 row_struct_map 把这些字段改成合法的项目导出名"
            "（不得等于保留键 Name/Type、不含空格、只用字母数字下划线且不以数字开头），"
            "或确认关系 kind 命名合法后重试；确需强行导出可传 force=True。"
        )


# --- serialization helpers ---------------------------------------------------------------------


def _export_name(table_key: str, field_name: str, row_struct_map: dict[str, dict[str, str]]) -> str:
    """Map a canonical field name to the project's struct EXPORT name (identity if unmapped).

    Reserved structural keys (``Name``/``Type``) are never remapped — recognize reads them by
    spelling."""
    if field_name in _RESERVED_KEYS:
        return field_name
    return row_struct_map.get(table_key, {}).get(field_name, field_name)


def _cpp_member_name(export_name: str, ue_type: UEType) -> str:
    """The C++ member name for an export name. A bool export name drops the UE 'b' prefix, so the
    generated member re-adds it (export ``IsActive`` -> member ``bIsActive``)."""
    if ue_type is UEType.BOOL:
        return "b" + export_name
    return export_name


def _serialize_single(target: str, handle_style: HandleStyle) -> Any:
    return {"RowName": target} if handle_style == "rowhandle" else target


def _serialize_list(targets: list[str], handle_style: HandleStyle) -> Any:
    if handle_style == "rowhandle":
        return [{"RowName": t} for t in targets]
    return list(targets)


def _dump_json(value: object) -> str:
    # Same deterministic shape as the generic bundle (sorted keys, indent 2, trailing newline).
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _write_string_table_csv(rows: list[tuple[str, str]]) -> str:
    """Epic StringTable CSV: two mandatory columns Key,SourceString (quotes CSV-escaped)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["Key", "SourceString"])
    for key, source in sorted(set(rows)):
        writer.writerow([key, source])
    return buffer.getvalue()


# --- the row builder ---------------------------------------------------------------------------


class _Ref:
    """A recorded reference (for dangling checks): from a table field to a target row id."""

    __slots__ = ("table", "field", "target")

    def __init__(self, table: str, field: str, target: str) -> None:
        self.table = table
        self.field = field
        self.target = target


def _build_rows(
    spec: _TableSpec,
    bundle: ContentBundle,
    *,
    row_struct_map: dict[str, dict[str, str]],
    handle_style: HandleStyle,
    text_mode: TextMode,
) -> tuple[list[dict[str, Any]], list[tuple[str, str]], list[_Ref], set[str]]:
    """Build one table's row-array. Returns (rows, string_table_rows, refs, relation_kinds)."""
    rows: list[dict[str, Any]] = []
    string_rows: list[tuple[str, str]] = []
    refs: list[_Ref] = []
    relation_kinds: set[str] = set()

    # Group entity relations by (source, kind) once, so relation-group columns are cheap per row.
    grouped: dict[str, dict[str, list[str]]] = {}
    if spec.relation_groups:
        for rel in bundle.relations:
            grouped.setdefault(rel.source, {}).setdefault(rel.kind, []).append(rel.target)

    for obj in spec.select(bundle):
        rid = spec.id_of(obj)
        row: dict[str, Any] = {"Name": rid}
        for field in spec.fields:
            value = field.extract(obj)
            emitted, sub_refs, sub_strings = _field_value(
                spec, field, value, handle_style=handle_style, text_mode=text_mode
            )
            if emitted is _OMIT:
                continue
            row[_export_name(spec.key, field.name, row_struct_map)] = emitted
            refs.extend(sub_refs)
            string_rows.extend(sub_strings)

        if spec.relation_groups:
            for kind in sorted(grouped.get(rid, {})):
                targets = [t for t in grouped[rid][kind] if t]
                if not targets:
                    continue
                relation_kinds.add(kind)
                row[_export_name(spec.key, kind, row_struct_map)] = _serialize_list(
                    targets, handle_style
                )
                refs.extend(_Ref(spec.key, kind, t) for t in targets)

        rows.append(row)

    rows.sort(key=lambda r: r["Name"])  # deterministic ordering
    return rows, string_rows, refs, relation_kinds


class _Omit:
    pass


_OMIT = _Omit()  # sentinel: field value absent/empty -> key omitted from the row


def _field_value(
    spec: _TableSpec,
    field: _FieldSpec,
    value: Any,
    *,
    handle_style: HandleStyle,
    text_mode: TextMode,
) -> tuple[Any, list[_Ref], list[tuple[str, str]]]:
    """Resolve one field to (json_value | _OMIT, refs, string_table_rows)."""
    if field.role is _Role.CONSTANT:
        return value, [], []

    if field.role is _Role.SCALAR:
        if value is None or (isinstance(value, str) and value == ""):
            return _OMIT, [], []
        return value, [], []

    if field.role is _Role.SCALAR_LIST:
        # Literal strings (enum_values, tags) — a plain JSON array, never handle-ified into
        # references, so they do not become spurious relations on re-recognize.
        items = [str(v) for v in (value or []) if v != ""]
        if not items:
            return _OMIT, [], []
        return items, [], []

    if field.role is _Role.REFERENCE:
        if not value:
            return _OMIT, [], []
        target = str(value)
        return _serialize_single(target, handle_style), [_Ref(spec.key, field.name, target)], []

    if field.role is _Role.REFERENCE_LIST:
        targets = [str(v) for v in (value or []) if v]
        if not targets:
            return _OMIT, [], []
        refs = [_Ref(spec.key, field.name, t) for t in targets]
        return _serialize_list(targets, handle_style), refs, []

    # _Role.LOCALIZED — an FText carrying a text_key.
    text_key, text, locale = value
    if text_mode == "inline":
        if not text:
            return _OMIT, [], []
        return text, [], []
    # stringtable: the field value is the StringTable key; the source string travels in the
    # companion CSV, gated exactly like xliff._units (text present + locale non-empty).
    if not text_key:
        return _OMIT, [], []
    strings = [(text_key, text)] if (text is not None and locale) else []
    return text_key, [], strings


# --- preflight validation ----------------------------------------------------------------------


def _run_preflight(
    bundle: ContentBundle,
    *,
    row_struct_map: dict[str, dict[str, str]],
    text_mode: TextMode,
    universe: set[str],
    refs: list[_Ref],
    relation_kinds_by_table: dict[str, set[str]],
    any_string_table_rows: bool,
) -> PreflightReport:
    issues: list[PreflightIssue] = []

    def validate_name(table: str, canonical: str, ue_type: UEType) -> None:
        export = _export_name(table, canonical, row_struct_map)
        if export == "Name":
            issues.append(
                PreflightIssue(
                    severity=PreflightSeverity.ERROR,
                    code="name_collision",
                    table=table,
                    field=export,
                    message="导出名与保留行键 'Name' 冲突（Name 是 DataTable 行名，不能作为字段）",
                )
            )
            return
        if " " in export:
            issues.append(
                PreflightIssue(
                    severity=PreflightSeverity.ERROR,
                    code="space_in_name",
                    table=table,
                    field=export,
                    message="导出名含空格；UE 打包后会破坏解析",
                )
            )
            return
        # The JSON key for a bool drops the leading 'b'; that stripped export name must still be a
        # valid identifier.
        if not _IDENTIFIER.match(export):
            issues.append(
                PreflightIssue(
                    severity=PreflightSeverity.ERROR,
                    code="invalid_identifier",
                    table=table,
                    field=export,
                    message="导出名不是合法标识符（只允许字母数字下划线，且不以数字开头）",
                )
            )

    for spec in _TABLE_SPECS:
        for field in spec.fields:
            validate_name(spec.key, field.name, field.ue_type)
        for kind in sorted(relation_kinds_by_table.get(spec.key, set())):
            validate_name(spec.key, kind, UEType.ROW_HANDLE_ARRAY)

    # Dangling references: a RowHandle whose target is outside the exported universe still round-
    # trips (recognize reads it unconditionally), but it's an honest review flag.
    for ref in refs:
        if ref.target not in universe:
            issues.append(
                PreflightIssue(
                    severity=PreflightSeverity.WARNING,
                    code="dangling_reference",
                    table=ref.table,
                    field=ref.field,
                    message=f"引用未知对象「{ref.target}」（不在导出集合内），保留待人审复核",
                )
            )

    # A bundle relation whose source is not in any exported table cannot be represented at all.
    for rel in bundle.relations:
        if rel.source not in universe:
            issues.append(
                PreflightIssue(
                    severity=PreflightSeverity.WARNING,
                    code="orphan_relation",
                    field=rel.kind,
                    message=f"关系源「{rel.source}」不属任何导出集合，该关系无法写入 DataTable",
                )
            )

    if text_mode == "stringtable" and any_string_table_rows:
        issues.append(
            PreflightIssue(
                severity=PreflightSeverity.WARNING,
                code="stringtable_literal_unverified",
                message="stringtable 模式下字段写入 StringTable key 并另发 <table>_strings.csv；"
                "但『裸 key 绑定 FText->StringTable』的精确 JSON 字面量随引擎版本而异，"
                "离线无法证实，需在真机 UE 上核对（inline 模式无此不确定性）",
            )
        )

    return PreflightReport(issues=issues)


# --- C++ row-struct header ---------------------------------------------------------------------


def _render_cpp_header(
    row_struct_map: dict[str, dict[str, str]],
    relation_kinds_by_table: dict[str, set[str]],
) -> str:
    """Emit a starter USTRUCT header (one struct per table, each deriving FTableRowBase).

    This is the row-struct field spec the consuming project must create, expressed as compilable
    C++. The row key 'Name' is the DataTable row name (not a struct field), so it is not declared.
    Bool fields re-add the 'b' member prefix that the JSON export name drops."""
    lines: list[str] = [
        "// OWCopilot exporters/unreal_datatable.py — row-struct field spec.",
        "// Create these USTRUCTs in your project and point each DataTable's Row Struct at them.",
        "// 'Name' is the DataTable RowName (not a struct field). Don't hand-edit — regenerate.",
        "#pragma once",
        "",
        '#include "CoreMinimal.h"',
        '#include "Engine/DataTable.h"',
        '#include "OWCopilotRows.generated.h"',
        "",
    ]
    for spec in _TABLE_SPECS:
        lines.append("USTRUCT(BlueprintType)")
        lines.append(f"struct F{spec.struct_name} : public FTableRowBase")
        lines.append("{")
        lines.append("    GENERATED_BODY()")
        lines.append("")
        for field in spec.fields:
            export = _export_name(spec.key, field.name, row_struct_map)
            member = _cpp_member_name(export, field.ue_type)
            lines.append("    UPROPERTY(EditAnywhere, BlueprintReadWrite)")
            lines.append(f"    {field.ue_type.value} {member};")
            lines.append("")
        if spec.relation_groups:
            for kind in sorted(relation_kinds_by_table.get(spec.key, set())):
                export = _export_name(spec.key, kind, row_struct_map)
                lines.append("    // relation-kind column (data-driven, one per observed kind)")
                lines.append("    UPROPERTY(EditAnywhere, BlueprintReadWrite)")
                lines.append(f"    {UEType.ROW_HANDLE_ARRAY.value} {export};")
                lines.append("")
        lines.append("};")
        lines.append("")
    return "\n".join(lines) + "\n"


# --- public entry point ------------------------------------------------------------------------


def export_unreal_datatables(
    bundle: ContentBundle,
    output_dir: str | Path,
    *,
    row_struct_map: dict[str, dict[str, str]] | None = None,
    handle_style: HandleStyle = "rowhandle",
    text_mode: TextMode = "stringtable",
    force: bool = False,
) -> UnrealExportResult:
    """Export a ContentBundle as UE DataTable row-array JSON + StringTable CSVs + a row header.

    ``handle_style="rowhandle"`` (default) emits ``{"RowName": id}`` handles that round-trip through
    ``recognize_engine_data`` regardless of whether the target is in-universe; ``"string"`` emits
    bare ids (only in-universe targets round-trip, so it is not the default).

    ``text_mode="stringtable"`` (default) writes each localizable field's ``text_key`` as the value
    and collects ``(text_key, text)`` into ``<table>_strings.csv`` (reusing the xliff text_key
    source — no duplicated strings); ``"inline"`` writes the source string directly.

    Preflight validates the contract; blocking (ERROR) issues raise ``UnrealExportError`` (nothing
    is written) unless ``force=True``. Warnings never block.
    """
    row_struct_map = row_struct_map or {}
    output = Path(output_dir)

    universe: set[str] = set()
    for spec in _TABLE_SPECS:
        universe.update(spec.id_of(o) for o in spec.select(bundle))

    built: dict[str, list[dict[str, Any]]] = {}
    string_rows_by_table: dict[str, list[tuple[str, str]]] = {}
    all_refs: list[_Ref] = []
    relation_kinds_by_table: dict[str, set[str]] = {}

    for spec in _TABLE_SPECS:
        rows, string_rows, refs, relation_kinds = _build_rows(
            spec,
            bundle,
            row_struct_map=row_struct_map,
            handle_style=handle_style,
            text_mode=text_mode,
        )
        built[spec.key] = rows
        string_rows_by_table[spec.key] = string_rows
        all_refs.extend(refs)
        relation_kinds_by_table[spec.key] = relation_kinds

    any_strings = any(string_rows_by_table.values())
    report = _run_preflight(
        bundle,
        row_struct_map=row_struct_map,
        text_mode=text_mode,
        universe=universe,
        refs=all_refs,
        relation_kinds_by_table=relation_kinds_by_table,
        any_string_table_rows=any_strings,
    )
    if not report.ok and not force:
        raise UnrealExportError(report)

    output.mkdir(parents=True, exist_ok=True)
    files: list[ExportedFile] = []
    tables: list[str] = []
    string_tables: list[str] = []

    for spec in _TABLE_SPECS:
        rows = built[spec.key]
        if not rows:
            continue
        (output / spec.filename).write_text(_dump_json(rows), encoding="utf-8")
        files.append(_file_entry(output, spec.filename, "datatable"))
        tables.append(spec.filename)

        if text_mode == "stringtable" and string_rows_by_table[spec.key]:
            csv_name = f"{Path(spec.filename).stem}_strings.csv"
            (output / csv_name).write_text(
                _write_string_table_csv(string_rows_by_table[spec.key]), encoding="utf-8"
            )
            files.append(_file_entry(output, csv_name, "string_table"))
            string_tables.append(csv_name)

    header = _render_cpp_header(row_struct_map, relation_kinds_by_table)
    (output / _HEADER_FILENAME).write_text(header, encoding="utf-8")
    files.append(_file_entry(output, _HEADER_FILENAME, "row_struct_header"))

    (output / _PREFLIGHT_FILENAME).write_text(
        _dump_json(report.model_dump(mode="json")), encoding="utf-8"
    )
    files.append(_file_entry(output, _PREFLIGHT_FILENAME, "preflight"))

    return UnrealExportResult(
        output_dir=str(output),
        files=files,
        preflight=report,
        tables=tables,
        string_tables=string_tables,
    )


def _file_entry(output: Path, relative: str, kind: str) -> ExportedFile:
    digest = hashlib.sha256((output / relative).read_bytes()).hexdigest()
    return ExportedFile(path=relative, kind=kind, sha256=digest)
