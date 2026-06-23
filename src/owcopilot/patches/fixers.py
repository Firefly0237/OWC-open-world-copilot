"""Deterministic patch candidates for rules with a principled mechanical fix.

These are the zero-cost fallback half of the suggest flow: they never call a model, they only
propose operations whose correctness is implied by the rule itself (drop a dangling optional
reference, deduplicate an identical relation, swap a forbidden term for its canonical form, add
the conventional localization key). Every candidate still goes through the same shadow-audit
validation as LLM candidates before it is shown to a human.
"""

from __future__ import annotations

import re

from ..audit.models import Issue
from ..content.models import ContentBundle
from .models import PatchCandidate, PatchOp, PatchOperation

# target_ref prefix -> top-level ContentBundle collection (all dict-shaped except relations).
BUNDLE_COLLECTIONS = {
    "entity": "entities",
    "quest": "quests",
    "region": "regions",
    "poi": "pois",
    "dialogue": "dialogues",
    "localized_text": "localized_texts",
    "term": "terms",
    "style_guide": "style_guides",
    "quest_event_ref": "quest_event_refs",
}

# Optional reference fields that may simply be dropped when they dangle.
_REMOVABLE_REF_FIELDS = {"giver_npc", "location", "speaker_id", "controlling_faction"}


def bundle_pointer_for_ref(ref: str) -> str | None:
    """Map an audit target_ref to the JSON pointer of that object in the bundle document."""
    kind, _, rest = ref.partition(":")
    if kind == "relation":
        index = rest.rsplit(":", 1)[-1]
        return f"/relations/{index}" if index.isdigit() else None
    collection = BUNDLE_COLLECTIONS.get(kind)
    if collection is None or not rest:
        return None
    return f"/{collection}/{_escape(rest)}"


def deterministic_candidates(issue: Issue, bundle: ContentBundle) -> list[PatchCandidate]:
    fixer = _FIXERS.get(issue.rule_code)
    if fixer is None:
        return []
    return fixer(issue, bundle)


def _fix_unknown_entity_ref(issue: Issue, bundle: ContentBundle) -> list[PatchCandidate]:
    pointer = bundle_pointer_for_ref(issue.target_ref)
    if pointer is None:
        return []
    missing = _quoted_id(issue.message)
    candidates: list[PatchCandidate] = []
    for evidence in issue.evidence:
        field = evidence.path or ""
        if field in _REMOVABLE_REF_FIELDS:
            candidates.append(
                PatchCandidate(
                    ops=[PatchOperation(op=PatchOp.REMOVE, path=f"{pointer}/{field}")],
                    rationale=(
                        f"Remove dangling reference field '{field}' on {issue.target_ref}; "
                        "the referenced object does not exist."
                    ),
                    evidence=[{"rule_code": issue.rule_code, "field": field}],
                )
            )
            continue
        stage_match = re.fullmatch(r"stages\.(\d+)\.required_entities", field)
        quest_id = issue.target_ref.partition(":")[2]
        if stage_match and missing and quest_id in bundle.quests:
            stage_index = int(stage_match.group(1))
            stages = bundle.quests[quest_id].stages
            if stage_index < len(stages) and missing in stages[stage_index].required_entities:
                value = [ref for ref in stages[stage_index].required_entities if ref != missing]
                candidates.append(
                    PatchCandidate(
                        ops=[
                            PatchOperation(
                                op=PatchOp.REPLACE,
                                path=f"{pointer}/stages/{stage_index}/required_entities",
                                value=value,
                            )
                        ],
                        rationale=(
                            f"Remove dangling stage required entity '{missing}' from "
                            f"{issue.target_ref}; the entity does not exist."
                        ),
                        evidence=[
                            {
                                "rule_code": issue.rule_code,
                                "field": field,
                                "missing_ref": missing,
                            }
                        ],
                    )
                )
            continue
        stage_loc_match = re.fullmatch(r"stages\.(\d+)\.location", field)
        if stage_loc_match and missing and quest_id in bundle.quests:
            stage_index = int(stage_loc_match.group(1))
            stages = bundle.quests[quest_id].stages
            if stage_index < len(stages) and stages[stage_index].location == missing:
                candidates.append(
                    PatchCandidate(
                        ops=[
                            PatchOperation(
                                op=PatchOp.REMOVE,
                                path=f"{pointer}/stages/{stage_index}/location",
                            )
                        ],
                        rationale=(
                            f"Remove dangling stage location '{missing}' from "
                            f"{issue.target_ref}; the entity does not exist."
                        ),
                        evidence=[
                            {
                                "rule_code": issue.rule_code,
                                "field": field,
                                "missing_ref": missing,
                            }
                        ],
                    )
                )
    return candidates


def _fix_missing_prerequisite(issue: Issue, bundle: ContentBundle) -> list[PatchCandidate]:
    pointer = bundle_pointer_for_ref(issue.target_ref)
    quest_id = issue.target_ref.partition(":")[2]
    if pointer is None or quest_id not in bundle.quests:
        return []
    missing = _field_suffix(issue, "prerequisites") or _quoted_id(issue.message)
    if not missing or missing not in bundle.quests[quest_id].prerequisites:
        return []
    return [
        PatchCandidate(
            ops=[
                PatchOperation(
                    op=PatchOp.REPLACE,
                    path=f"{pointer}/prerequisites",
                    value=[ref for ref in bundle.quests[quest_id].prerequisites if ref != missing],
                )
            ],
            rationale=(
                f"Remove missing prerequisite '{missing}' from {issue.target_ref}; "
                "the prerequisite quest does not exist."
            ),
            evidence=[{"rule_code": issue.rule_code, "missing_ref": missing}],
        )
    ]


def _fix_missing_dialogue_ref(issue: Issue, bundle: ContentBundle) -> list[PatchCandidate]:
    pointer = bundle_pointer_for_ref(issue.target_ref)
    quest_id = issue.target_ref.partition(":")[2]
    if pointer is None or quest_id not in bundle.quests:
        return []
    missing = _field_suffix(issue, "dialogue_refs") or _quoted_id(issue.message)
    if not missing or missing not in bundle.quests[quest_id].dialogue_refs:
        return []
    return [
        PatchCandidate(
            ops=[
                PatchOperation(
                    op=PatchOp.REPLACE,
                    path=f"{pointer}/dialogue_refs",
                    value=[ref for ref in bundle.quests[quest_id].dialogue_refs if ref != missing],
                )
            ],
            rationale=(
                f"Remove missing dialogue reference '{missing}' from {issue.target_ref}; "
                "the dialogue entry does not exist."
            ),
            evidence=[{"rule_code": issue.rule_code, "missing_ref": missing}],
        )
    ]


def _fix_missing_relation_endpoint(issue: Issue, bundle: ContentBundle) -> list[PatchCandidate]:
    triples = [evidence.relation for evidence in issue.evidence if evidence.relation is not None]
    if not triples:
        return []
    ops: list[PatchOperation] = []
    for source, kind, target in triples:
        indices = [
            index
            for index, relation in enumerate(bundle.relations)
            if (relation.source, relation.kind, relation.target) == (source, kind, target)
        ]
        ops.extend(
            PatchOperation(op=PatchOp.REMOVE, path=f"/relations/{index}")
            for index in sorted(indices, reverse=True)
        )
    if not ops:
        return []
    return [
        PatchCandidate(
            ops=ops,
            rationale=(
                "Remove relation(s) with missing endpoint(s); graph edges cannot point to "
                "objects outside the canon."
            ),
            evidence=[
                {
                    "rule_code": issue.rule_code,
                    "relations": [list(triple) for triple in triples],
                }
            ],
        )
    ]


def _fix_missing_localization_key(issue: Issue, bundle: ContentBundle) -> list[PatchCandidate]:
    pointer = bundle_pointer_for_ref(issue.target_ref)
    quest_id = issue.target_ref.partition(":")[2]
    if pointer is None or quest_id not in bundle.quests:
        return []
    return [
        PatchCandidate(
            ops=[
                PatchOperation(
                    op=PatchOp.REPLACE,
                    path=f"{pointer}/localization_keys",
                    value=[f"quest.{quest_id}.objective"],
                )
            ],
            rationale=(
                f"Add the conventional localization key 'quest.{quest_id}.objective' "
                "so the quest enters the localization pipeline."
            ),
            evidence=[{"rule_code": issue.rule_code}],
        )
    ]


def _fix_term_inconsistent(issue: Issue, bundle: ContentBundle) -> list[PatchCandidate]:
    pointer = bundle_pointer_for_ref(issue.target_ref)
    if pointer is None:
        return []
    candidates: list[PatchCandidate] = []
    for evidence in issue.evidence:
        forbidden = str(evidence.data.get("forbidden") or "")
        canonical = str(evidence.data.get("canonical") or "")
        field = evidence.path or ""
        if not forbidden or not canonical or not field:
            continue
        current = _field_text(bundle, issue.target_ref, field)
        if current is None or forbidden.lower() not in current.lower():
            continue
        replaced = _replace_case_insensitive(current, forbidden, canonical)
        candidates.append(
            PatchCandidate(
                ops=[PatchOperation(op=PatchOp.REPLACE, path=f"{pointer}/{field}", value=replaced)],
                rationale=(
                    f"Replace forbidden term '{forbidden}' with canonical term "
                    f"'{canonical}' in {issue.target_ref}.{field}."
                ),
                evidence=[{"rule_code": issue.rule_code, "forbidden": forbidden}],
            )
        )
    return candidates


def _fix_duplicate_relation(issue: Issue, bundle: ContentBundle) -> list[PatchCandidate]:
    triple = None
    for evidence in issue.evidence:
        if evidence.relation is not None:
            triple = evidence.relation
            break
    if triple is None:
        return []
    source, kind, target = triple
    indices = [
        index
        for index, relation in enumerate(bundle.relations)
        if (relation.source, relation.kind, relation.target) == (source, kind, target)
    ]
    if len(indices) < 2:
        return []
    ops = [
        PatchOperation(op=PatchOp.REMOVE, path=f"/relations/{index}")
        for index in sorted(indices[1:], reverse=True)  # high to low keeps pointers stable
    ]
    return [
        PatchCandidate(
            ops=ops,
            rationale=(
                f"Remove {len(ops)} duplicate copies of relation "
                f"'{source} {kind} {target}', keeping the first occurrence."
            ),
            evidence=[{"rule_code": issue.rule_code, "relation": list(triple)}],
        )
    ]


def _field_text(bundle: ContentBundle, target_ref: str, field: str) -> str | None:
    kind, _, object_id = target_ref.partition(":")
    document: dict | None = None
    if kind == "quest":
        quest = bundle.quests.get(object_id)
        document = quest.model_dump(mode="json") if quest else None
    elif kind == "dialogue":
        dialogue = bundle.dialogues.get(object_id)
        document = dialogue.model_dump(mode="json") if dialogue else None
    elif kind == "localized_text":
        text = bundle.localized_texts.get(object_id)
        document = text.model_dump(mode="json") if text else None
    if document is None:
        return None
    value = document.get(field)
    return value if isinstance(value, str) else None


def _replace_case_insensitive(text: str, old: str, new: str) -> str:
    result: list[str] = []
    cursor = 0
    lowered = text.lower()
    needle = old.lower()
    while True:
        found = lowered.find(needle, cursor)
        if found < 0:
            result.append(text[cursor:])
            return "".join(result)
        result.append(text[cursor:found])
        result.append(new)
        cursor = found + len(needle)


def _escape(part: str) -> str:
    return part.replace("~", "~0").replace("/", "~1")


def _quoted_id(text: str) -> str | None:
    matches = re.findall(r"'([^']+)'", text)
    return matches[-1] if matches else None


def _field_suffix(issue: Issue, prefix: str) -> str | None:
    marker = prefix + "."
    for evidence in issue.evidence:
        path = evidence.path or ""
        if path.startswith(marker):
            return path[len(marker) :]
    return None


_FIXERS = {
    "UNKNOWN_ENTITY_REF": _fix_unknown_entity_ref,
    "PREREQ_MISSING": _fix_missing_prerequisite,
    "MISSING_DIALOGUE_REF": _fix_missing_dialogue_ref,
    "MISSING_RELATION_ENDPOINT": _fix_missing_relation_endpoint,
    "MISSING_LOCALIZATION_KEY": _fix_missing_localization_key,
    "TERM_INCONSISTENT": _fix_term_inconsistent,
    "DUPLICATE_RELATION": _fix_duplicate_relation,
}
