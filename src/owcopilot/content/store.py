"""File-backed content store.

The files are the source of truth. SQLite indexes and audit tables come later and must be
rebuildable from this directory.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from ..trust.security import resolve_under_root
from .models import (
    POI,
    ContentBundle,
    DialogueRef,
    DialogueTree,
    Entity,
    LocalizedText,
    Quest,
    QuestEventReference,
    RegionBrief,
    Relation,
    StyleGuide,
    Term,
)
from .normalize import _FORBIDDEN_ID_CHARS, _validate_id_chars

ModelT = TypeVar("ModelT", bound=BaseModel)


class ContentStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def load(self) -> ContentBundle:
        bundle = ContentBundle()
        for entity in self._load_json_dir(self.root / "world" / "entities", Entity):
            bundle.entities[entity.id] = entity
        bundle.relations = self._load_relations()
        bundle.quest_event_refs = self._load_quest_event_refs()
        for region in self._load_json_dir(self.root / "regions", RegionBrief):
            bundle.regions[region.id] = region
        for quest in self._load_json_dir(self.root / "quests", Quest):
            bundle.quests[quest.id] = quest
        for poi in self._load_json_dir(self.root / "pois", POI):
            bundle.pois[poi.id] = poi
        for dialogue in self._load_json_dir(self.root / "dialogues", DialogueRef):
            bundle.dialogues[dialogue.id] = dialogue
        for tree in self._load_json_dir(self.root / "dialogues" / "trees", DialogueTree):
            bundle.dialogue_trees[tree.id] = tree
        for text in self._load_json_dir(self.root / "localization" / "texts", LocalizedText):
            bundle.localized_texts[text.id] = text
        for term in self._load_terms():
            bundle.terms[term.id] = term
        for style in self._load_style_guides():
            bundle.style_guides[style.id] = style
        return bundle

    def save(self, bundle: ContentBundle) -> None:
        self._write_json_dir(self.root / "world" / "entities", bundle.entities)
        self._write_relations(bundle.relations)
        self._write_quest_event_refs(bundle.quest_event_refs)
        self._write_json_dir(self.root / "regions", bundle.regions)
        self._write_json_dir(self.root / "quests", bundle.quests)
        self._write_json_dir(self.root / "pois", bundle.pois)
        self._write_json_dir(self.root / "dialogues", bundle.dialogues)
        self._write_json_dir(self.root / "dialogues" / "trees", bundle.dialogue_trees)
        self._write_json_dir(self.root / "localization" / "texts", bundle.localized_texts)
        self._write_terms(bundle.terms)
        self._write_style_guides(bundle.style_guides)

    def exists(self) -> bool:
        return self.root.exists()

    def _load_json_dir(self, path: Path, model: type[ModelT]) -> list[ModelT]:
        if not path.exists():
            return []
        loaded: list[ModelT] = []
        for file_path in sorted(path.glob("*.json")):
            loaded.append(model.model_validate_json(file_path.read_text(encoding="utf-8")))
        return loaded

    def _load_relations(self) -> list[Relation]:
        path = self.root / "world" / "relations.jsonl"
        if not path.exists():
            return []
        relations: list[Relation] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            if raw.strip():
                relations.append(Relation.model_validate_json(raw))
        return relations

    def _load_quest_event_refs(self) -> dict[str, QuestEventReference]:
        path = self.root / "quests" / "event_refs.jsonl"
        if not path.exists():
            return {}
        refs: dict[str, QuestEventReference] = {}
        for raw in path.read_text(encoding="utf-8").splitlines():
            if raw.strip():
                ref = QuestEventReference.model_validate_json(raw)
                refs[ref.id] = ref
        return refs

    def _load_style_guides(self) -> list[StyleGuide]:
        """Full-fidelity JSON is canonical; an old body-only ``style_guide.md`` still loads (so
        worlds saved before this format upgrade keep working, just without their rules)."""
        path = self.root / "world" / "style_guides.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return [StyleGuide.model_validate(raw) for raw in data.values()]
            return []
        legacy = self.root / "world" / "style_guide.md"
        if legacy.exists():
            return [StyleGuide(body=legacy.read_text(encoding="utf-8"))]
        return []

    def _load_terms(self) -> list[Term]:
        path = self.root / "world" / "terms.json"
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [Term.model_validate(item) for item in data]
        if isinstance(data, dict):
            return [Term.model_validate(item) for item in data.values() if isinstance(item, dict)]
        return []

    def _write_json_dir(self, path: Path, objects: dict[str, ModelT]) -> None:
        # Write-boundary id invariant (the last line of defense). Every object_id here becomes
        # a `{object_id}.json` filename, so a traversal / separator / control-char id could
        # escape the content directory. The normalize ingest path already enforces this via
        # `_resolve_id`, but other ingest paths (recognize → human review → store.save) bypass
        # normalize entirely. Validating at the write boundary with the SAME shared invariant
        # (`_validate_id_chars`, imported from normalize — not a copy) means *every* path that
        # produces a `{id}.json` file shares one guarantee, instead of each remembering to call
        # normalize. jsonl / aggregate writers (relations, quest_event_refs, terms, style_guides)
        # do NOT pass through here, so the quest_event_ref synthetic colon id stays legal.
        for object_id in objects:
            _validate_id_chars(
                object_id,
                context=f"content store write to {path}",
                forbidden=_FORBIDDEN_ID_CHARS,
            )
        path.mkdir(parents=True, exist_ok=True)
        expected = {f"{object_id}.json" for object_id in objects}
        for existing in path.glob("*.json"):
            if existing.name not in expected:
                existing.unlink()
        for object_id, model in sorted(objects.items()):
            target = path / f"{object_id}.json"
            # Second layer: assert the FINAL path stays inside the content root using the same
            # canon container helper the rest of the codebase uses (resolve_under_root), instead
            # of a second hand-rolled resolve. PathSecurityError is a ValueError subclass, so the
            # CLI's guided-error boundary still formats it. `_validate_id_chars` above already
            # rejected traversal/separator ids; this is defense-in-depth over the real filename.
            resolve_under_root(self.root, target)
            self._write_json(target, model)

    def _write_relations(self, relations: list[Relation]) -> None:
        path = self.root / "world" / "relations.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [_json_line(relation) for relation in relations]
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def _write_quest_event_refs(self, refs: dict[str, QuestEventReference]) -> None:
        path = self.root / "quests" / "event_refs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [_json_line(ref) for ref in refs.values()]
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def _write_terms(self, terms: dict[str, Term]) -> None:
        path = self.root / "world" / "terms.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = [term.model_dump(mode="json", exclude_none=True) for term in terms.values()]
        path.write_text(_json(payload), encoding="utf-8")

    def _write_style_guides(self, style_guides: dict[str, StyleGuide]) -> None:
        """Persist EVERY style guide with all its fields (id, body, rules, …) — the old code only
        wrote the single ``"style_guide"`` key's ``body``, silently dropping rules and any other
        guide. The legacy ``.md`` is removed once the full-fidelity JSON exists."""
        path = self.root / "world" / "style_guides.json"
        legacy = self.root / "world" / "style_guide.md"
        if not style_guides:
            path.unlink(missing_ok=True)
            legacy.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            style_id: guide.model_dump(mode="json", exclude_none=True)
            for style_id, guide in sorted(style_guides.items())
        }
        path.write_text(_json(payload), encoding="utf-8")
        legacy.unlink(missing_ok=True)

    def _write_json(self, path: Path, model: BaseModel) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json(model.model_dump(mode="json", exclude_none=True)), encoding="utf-8")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _json_line(model: BaseModel) -> str:
    payload = model.model_dump(mode="json", exclude_none=True)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
