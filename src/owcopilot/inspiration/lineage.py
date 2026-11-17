# 正典对象与灵感素材之间的引用关系（生成后仍留存的那条链接）。
"""Cross-reference between canon objects and the inspiration chunks they were grounded in.

Generation stamps ``metadata["inspiration_refs"]`` — a comma-joined list of reference-chunk ids —
onto AI-drafted objects when a batch drew grounding from the library (the shared ``common_meta``
in ``worldgen/service.py`` and ``worldgen/expand.py``). That link outlives generation, so two
questions follow once the object is adopted into canon:

  * before a reference source is deleted, which already-adopted canon objects still cite one of
    its chunks (:func:`objects_citing_chunks`) — surfaced as a non-blocking warning, since
    references are not canon and carry no tombstone (N2.3 / B6);
  * for a whole world, which canon objects' ``inspiration_refs`` point at chunks that no longer
    resolve to anything — a dangling citation (:func:`dangling_inspiration_refs`, N2.4 / B7).

Only entities/pois/regions/quests ever carry this metadata key — ``Term`` and ``StyleGuide`` have
no ``metadata`` field at all, and dialogue trees are a separate generation path that does not yet
draw on the inspiration library.
"""

from __future__ import annotations

from ..content.models import ContentBundle


def _cited_objects(bundle: ContentBundle) -> list[tuple[str, list[str]]]:
    """Every canon object with cited chunks, as ``(object_ref, chunk_ids)`` pairs."""
    out: list[tuple[str, list[str]]] = []
    collections: tuple[tuple[str, dict], ...] = (
        ("entity", bundle.entities),
        ("poi", bundle.pois),
        ("region", bundle.regions),
        ("quest", bundle.quests),
    )
    for prefix, items in collections:
        for object_id, obj in items.items():
            refs = [r for r in str(obj.metadata.get("inspiration_refs") or "").split(",") if r]
            if refs:
                out.append((f"{prefix}:{object_id}", refs))
    return out


def objects_citing_chunks(bundle: ContentBundle, chunk_ids: set[str]) -> list[str]:
    """Object refs whose ``inspiration_refs`` intersects ``chunk_ids`` — used to warn before a
    reference source is physically deleted (its chunks are about to stop existing)."""
    return [ref for ref, refs in _cited_objects(bundle) if chunk_ids.intersection(refs)]


def dangling_inspiration_refs(
    bundle: ContentBundle, known_chunk_ids: frozenset[str]
) -> dict[str, list[str]]:
    """Object ref -> the subset of its cited chunk ids that resolve to nothing in
    ``known_chunk_ids`` (the reference library's current chunk set)."""
    out: dict[str, list[str]] = {}
    for ref, refs in _cited_objects(bundle):
        missing = [r for r in refs if r not in known_chunk_ids]
        if missing:
            out[ref] = missing
    return out


__all__ = ["dangling_inspiration_refs", "objects_citing_chunks"]
