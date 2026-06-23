"""Safe canon-wide rename/refactor: change an object's display name, or change its id and update
*every* reference to it (relations, quest giver/location/prereqs/stage-entities/logic, POI
region/faction, dialogue speakers). The plan is computed first (dry-run), applied atomically on a
copy, and the action layer wraps it with a snapshot (undo) + a post-rename audit (must stay clean).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .models import ContentBundle

_COLLECTIONS = (
    "entities",
    "quests",
    "regions",
    "pois",
    "terms",
    "dialogues",
    "dialogue_trees",
    "localized_texts",
)

_REF_KIND_TO_COLLECTION = {
    "entity": "entities",
    "quest": "quests",
    "region": "regions",
    "poi": "pois",
    "term": "terms",
    "dialogue": "dialogues",
    "dialogue_tree": "dialogue_trees",
    "localized_text": "localized_texts",
}
_COLLECTION_TO_REF_KIND = {collection: kind for kind, collection in _REF_KIND_TO_COLLECTION.items()}


class RefEdit(BaseModel):
    owner_ref: str  # where the reference lives, e.g. "quest:q1", "relation:0"
    field: str  # which field, e.g. "giver_npc", "source", "logic.unlocks"


class RenamePlan(BaseModel):
    target: str  # "<kind>:<old_id>"
    old_id: str
    new_id: str | None = None
    new_name: str | None = None
    edits: list[RefEdit] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)


def _object_kind(bundle: ContentBundle, obj_id: str) -> str | None:
    for collection in _COLLECTIONS:
        if obj_id in getattr(bundle, collection):
            return _COLLECTION_TO_REF_KIND.get(collection, collection)
    return None


def _kind_to_collection(bundle: ContentBundle, obj_id: str) -> str | None:
    for kind in _COLLECTIONS:
        if obj_id in getattr(bundle, kind):
            return kind
    return None


def _normalize_ref(bundle: ContentBundle, ref: str) -> str:
    """Accept both the internal bare id (``npc_mara``) and public refs (``entity:npc_mara``)."""
    if _kind_to_collection(bundle, ref) is not None:
        return ref
    kind, sep, object_id = ref.partition(":")
    if not sep:
        return ref
    collection = _REF_KIND_TO_COLLECTION.get(kind)
    if collection is not None and object_id in getattr(bundle, collection):
        return object_id
    return ref


def find_references(bundle: ContentBundle, old_id: str) -> list[RefEdit]:
    """Every site that references ``old_id`` by id (excludes the object's own key)."""
    edits: list[RefEdit] = []
    for index, relation in enumerate(bundle.relations):
        if relation.source == old_id:
            edits.append(RefEdit(owner_ref=f"relation:{index}", field="source"))
        if relation.target == old_id:
            edits.append(RefEdit(owner_ref=f"relation:{index}", field="target"))
    for quest in bundle.quests.values():
        owner = f"quest:{quest.id}"
        if quest.giver_npc == old_id:
            edits.append(RefEdit(owner_ref=owner, field="giver_npc"))
        if quest.location == old_id:
            edits.append(RefEdit(owner_ref=owner, field="location"))
        if old_id in quest.prerequisites:
            edits.append(RefEdit(owner_ref=owner, field="prerequisites"))
        if old_id in quest.dialogue_refs:
            edits.append(RefEdit(owner_ref=owner, field="dialogue_refs"))
        for sidx, stage in enumerate(quest.stages):
            if old_id in stage.required_entities:
                edits.append(RefEdit(owner_ref=owner, field=f"stages.{sidx}.required_entities"))
        if quest.logic is not None:
            if old_id in quest.logic.unlocks:
                edits.append(RefEdit(owner_ref=owner, field="logic.unlocks"))
            if _logic_mentions_quest(quest.logic, old_id):
                edits.append(RefEdit(owner_ref=owner, field="logic.expressions"))
    for poi in bundle.pois.values():
        if poi.region_id == old_id:
            edits.append(RefEdit(owner_ref=f"poi:{poi.id}", field="region_id"))
        if poi.controlling_faction == old_id:
            edits.append(RefEdit(owner_ref=f"poi:{poi.id}", field="controlling_faction"))
    for dialogue in bundle.dialogues.values():
        if dialogue.speaker_id == old_id:
            edits.append(RefEdit(owner_ref=f"dialogue:{dialogue.id}", field="speaker_id"))
    for tree in bundle.dialogue_trees.values():
        for node_id, node in tree.nodes.items():
            if node.speaker_id == old_id:
                edits.append(
                    RefEdit(owner_ref=f"dialogue_tree:{tree.id}", field=f"nodes.{node_id}.speaker")
                )
    return edits


def _logic_mentions_quest(logic: object, quest_id: str) -> bool:
    token = f"quest:{quest_id}.done"
    sources = [getattr(logic, "precondition", "")]
    sources += [s.precondition for s in getattr(logic, "stage_logic", [])]
    sources += [b.condition for b in getattr(logic, "branches", [])]
    return any(token in (src or "") for src in sources)


def plan_rename(
    bundle: ContentBundle,
    *,
    ref: str,
    new_name: str | None = None,
    new_id: str | None = None,
) -> RenamePlan:
    """Dry-run: compute the edits a rename would make and any conflicts. Mutates nothing."""
    ref = _normalize_ref(bundle, ref)
    collection = _kind_to_collection(bundle, ref)
    if collection is None:
        raise ValueError(f"对象不存在：{ref}")
    kind = _object_kind(bundle, ref) or collection
    plan = RenamePlan(target=f"{kind}:{ref}", old_id=ref, new_name=new_name, new_id=new_id)
    if new_id and new_id != ref:
        if new_id in getattr(bundle, collection):
            plan.conflicts.append(f"id「{new_id}」已存在于 {collection}")
        plan.edits = find_references(bundle, ref)
    return plan


def apply_rename(bundle: ContentBundle, plan: RenamePlan) -> ContentBundle:
    """Apply a plan on a fresh copy and return the new bundle (pure; no IO). Raises on conflict."""
    if plan.conflicts:
        raise ValueError("；".join(plan.conflicts))
    out = ContentBundle.model_validate(bundle.model_dump(mode="python"))
    collection = _kind_to_collection(out, plan.old_id)
    if collection is None:
        raise ValueError(f"对象不存在：{plan.old_id}")
    target = getattr(out, collection)[plan.old_id]
    if plan.new_name is not None:
        if hasattr(target, "name"):
            target.name = plan.new_name
        elif hasattr(target, "title"):
            target.title = plan.new_name
    if plan.new_id and plan.new_id != plan.old_id:
        _apply_id_change(out, plan.old_id, plan.new_id, collection)
    return out


def _swap(values: list[str], old: str, new: str) -> list[str]:
    return [new if v == old else v for v in values]


def _apply_id_change(out: ContentBundle, old: str, new: str, collection: str) -> None:
    for relation in out.relations:
        if relation.source == old:
            relation.source = new
        if relation.target == old:
            relation.target = new
    for quest in out.quests.values():
        if quest.giver_npc == old:
            quest.giver_npc = new
        if quest.location == old:
            quest.location = new
        quest.prerequisites = _swap(quest.prerequisites, old, new)
        quest.dialogue_refs = _swap(quest.dialogue_refs, old, new)
        for stage in quest.stages:
            stage.required_entities = _swap(stage.required_entities, old, new)
        if quest.logic is not None:
            quest.logic.unlocks = _swap(quest.logic.unlocks, old, new)
            _rewrite_logic_quest_ref(quest.logic, old, new)
    for poi in out.pois.values():
        if poi.region_id == old:
            poi.region_id = new
        if poi.controlling_faction == old:
            poi.controlling_faction = new
    for dialogue in out.dialogues.values():
        if dialogue.speaker_id == old:
            dialogue.speaker_id = new
    for tree in out.dialogue_trees.values():
        for node in tree.nodes.values():
            if node.speaker_id == old:
                node.speaker_id = new
    moved = getattr(out, collection).pop(old)
    moved.id = new
    getattr(out, collection)[new] = moved


def _rewrite_logic_quest_ref(logic: object, old: str, new: str) -> None:
    token, replacement = f"quest:{old}.done", f"quest:{new}.done"
    if getattr(logic, "precondition", ""):
        logic.precondition = logic.precondition.replace(token, replacement)  # type: ignore[attr-defined]
    for stage in getattr(logic, "stage_logic", []):
        stage.precondition = stage.precondition.replace(token, replacement)
    for branch in getattr(logic, "branches", []):
        branch.condition = branch.condition.replace(token, replacement)
