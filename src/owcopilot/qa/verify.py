"""Deterministic QA post-checks."""

from __future__ import annotations

from ..content.models import ContentBundle
from ..retrieval.models import ContextPack
from .models import QAAnswer, QAVerification


def verify_qa_answer(
    answer: QAAnswer, *, pack: ContextPack, bundle: ContentBundle
) -> QAVerification:
    errors: list[str] = []
    if not answer.refused and not answer.citations:
        errors.append("non-refusal answer must cite at least one retrieved lore ref")
    allowed_refs = set(pack.refs)
    citation_text_by_ref = {
        hit.ref: " ".join(part for part in (hit.title, hit.body) if part).strip()
        for hit in pack.hits
    }
    for citation in answer.citations:
        canonical_ref = _canonical_ref(citation.ref, allowed_refs)
        if canonical_ref:
            citation.ref = canonical_ref
            if not citation.text:
                citation.text = citation_text_by_ref.get(canonical_ref, "")[:500]
        else:
            errors.append(f"citation {citation.ref!r} was not in the context pack")

    known_refs = _known_refs(bundle)
    known_ids = {ref.split(":", 1)[1] for ref in known_refs if ":" in ref}
    known_names = _known_names(bundle)
    unresolved: list[str] = list(answer.unresolved_mentions)
    answer_text = answer.answer.lower()
    for mention in answer.mentioned_entities:
        normalized = mention.lower()
        if (
            mention in known_refs
            or mention in known_ids
            or _canonical_ref(mention, known_refs)
            or normalized in known_names
        ):
            continue
        if normalized not in answer_text:
            continue
        unresolved.append(mention)
        errors.append(f"mentioned entity {mention!r} could not be resolved")

    return QAVerification(valid=not errors, errors=errors, unresolved_mentions=unresolved)


def _canonical_ref(ref: str, allowed_refs: set[str]) -> str | None:
    if ref in allowed_refs:
        return ref
    object_id = ref.split(":", 1)[-1]
    matches = [allowed for allowed in allowed_refs if allowed.endswith(f":{object_id}")]
    if len(matches) == 1:
        return matches[0]
    return None


def _known_refs(bundle: ContentBundle) -> set[str]:
    refs = {f"entity:{object_id}" for object_id in bundle.entities}
    refs.update(f"quest:{object_id}" for object_id in bundle.quests)
    refs.update(f"region:{object_id}" for object_id in bundle.regions)
    refs.update(f"poi:{object_id}" for object_id in bundle.pois)
    refs.update(f"dialogue:{object_id}" for object_id in bundle.dialogues)
    refs.update(f"localized_text:{object_id}" for object_id in bundle.localized_texts)
    refs.update(f"term:{object_id}" for object_id in bundle.terms)
    refs.update(f"quest_event_ref:{object_id}" for object_id in bundle.quest_event_refs)
    return refs


def _known_names(bundle: ContentBundle) -> set[str]:
    names = {entity.name.lower() for entity in bundle.entities.values()}
    names.update(poi.name.lower() for poi in bundle.pois.values())
    names.update(region.name.lower() for region in bundle.regions.values())
    names.update(quest.title.lower() for quest in bundle.quests.values())
    names.update(term.canonical.lower() for term in bundle.terms.values())
    return names
