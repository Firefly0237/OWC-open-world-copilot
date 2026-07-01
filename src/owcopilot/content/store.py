"""File-backed content store.

The files are the source of truth. SQLite indexes and audit tables come later and must be
rebuildable from this directory.
"""

from __future__ import annotations

import json
from collections.abc import Callable
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

# Scale-P0 G2-C C3a: the baseline version is this store's root tree; derived versions live under
# ``root/versions/<version>/``. ``v1`` matches the default scope, so ``load_scoped(version="v1")``
# with no ``versions/`` dir is byte-identical to ``load()`` (INV-1).
_BASELINE_VERSION = "v1"


class ContentStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        # mtime/size fast path for reload(): a per-file parse cache keyed on the file's identity
        # plus its (mtime_ns, size) stat. An unchanged file is served from cache without re-reading
        # or re-deserializing it -- the dominant cost when a long-running project re-opens a corpus
        # of which only a handful of files changed. Files are the source of truth, so a changed file
        # always misses (its stat differs) and is re-parsed; this is purely a performance shortcut,
        # never a correctness one. Keyed by absolute path string -> (mtime_ns, size, parsed value).
        self._parse_cache: dict[str, tuple[int, int, Any]] = {}
        # Test/diagnostic counters: how many cached files were reused vs. read from disk on the last
        # set of loads. Lets a test assert "an unchanged directory touched zero disk reads".
        self._cache_hits = 0
        self._cache_misses = 0

    def load(self) -> ContentBundle:
        """Load the baseline content tree (the default world, version ``v1``)."""
        return self._load_bundle_from(self.root)

    def _load_bundle_from(self, root: Path) -> ContentBundle:
        """Load a full ``ContentBundle`` from an arbitrary content-root directory.

        Scale-P0 G2-C C3a: factored out of ``load()`` so version overlays can load the baseline
        tree and each derived version's override directory through the same code path. Called with
        ``self.root`` for the baseline; with ``self.root / "versions" / <version>`` for an override
        layer (see ``load_scoped``). Behaviour for ``self.root`` is identical to pre-C3 ``load``."""
        bundle = ContentBundle()
        for entity in self._load_json_dir(root / "world" / "entities", Entity):
            bundle.entities[entity.id] = entity
        bundle.relations = self._load_relations(root)
        bundle.quest_event_refs = self._load_quest_event_refs(root)
        for region in self._load_json_dir(root / "regions", RegionBrief):
            bundle.regions[region.id] = region
        for quest in self._load_json_dir(root / "quests", Quest):
            bundle.quests[quest.id] = quest
        for poi in self._load_json_dir(root / "pois", POI):
            bundle.pois[poi.id] = poi
        for dialogue in self._load_json_dir(root / "dialogues", DialogueRef):
            bundle.dialogues[dialogue.id] = dialogue
        for tree in self._load_json_dir(root / "dialogues" / "trees", DialogueTree):
            bundle.dialogue_trees[tree.id] = tree
        for text in self._load_json_dir(root / "localization" / "texts", LocalizedText):
            bundle.localized_texts[text.id] = text
        for term in self._load_terms(root):
            bundle.terms[term.id] = term
        for style in self._load_style_guides(root):
            bundle.style_guides[style.id] = style
        return bundle

    # -- Scale-P0 G2-C C3a: version overlay (copy-on-write inheritance) --------------------------

    def load_scoped(self, *, world_id: str = "default", version: str = "v1") -> ContentBundle:
        """Effective ``ContentBundle`` for a ``(world_id, version)`` scope via copy-on-write.

        The baseline ``v1`` is this store's root tree. A derived version lives in
        ``root/versions/<version>/`` (only its added/changed object files) plus an optional
        ``tombstones.json`` (``"kind:id"`` entries it deletes from the base). Reading walks the base
        chain [baseline … target] and overlays each layer: the nearest version's definition of an id
        wins; a tombstone removes it. ``version == "v1"`` with no ``versions/`` dir returns exactly
        ``load()`` (INV-1 byte-identical). ``world_id`` is threaded for the (world, version) scope,
        but multi-world content-root routing is C6 -- here the baseline is always this store's root.
        """
        chain = self._resolve_version_chain(version)
        bundle = self._load_bundle_from(self._version_root(chain[0]))
        for derived in chain[1:]:
            overlay = self._load_bundle_from(self._version_root(derived))
            self._apply_overlay(bundle, overlay, self._load_tombstones(derived))
        return bundle

    def _version_root(self, version: str) -> Path:
        """Content root for a version: the store root for baseline ``v1``, else its override dir."""
        if version == _BASELINE_VERSION:
            return self.root
        return self.root / "versions" / version

    def _resolve_version_chain(self, version: str) -> list[str]:
        """Base chain from baseline to ``version`` (inclusive), read from the file-backed
        ``versions/<v>/version.json`` (``base_version``). The baseline ``v1`` terminates the walk;
        a version with no metadata derives directly from the baseline. Defensive against cycles."""
        if version == _BASELINE_VERSION:
            return [_BASELINE_VERSION]
        chain: list[str] = []
        seen: set[str] = set()
        current: str | None = version
        while current is not None and current != _BASELINE_VERSION and current not in seen:
            seen.add(current)
            chain.append(current)
            current = self._version_base(current)
        chain.append(_BASELINE_VERSION)
        chain.reverse()  # baseline first, target last
        return chain

    def _version_base(self, version: str) -> str | None:
        meta = self.root / "versions" / version / "version.json"
        if not meta.exists():
            return None  # no metadata -> derives directly from the baseline
        data = json.loads(meta.read_text(encoding="utf-8"))
        base = data.get("base_version")
        return str(base) if base else None

    def _load_tombstones(self, version: str) -> set[str]:
        """The ``"kind:id"`` entries a derived version deletes from its base (``[]`` if none)."""
        path = self.root / "versions" / version / "tombstones.json"
        if not path.exists():
            return set()
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(x) for x in data} if isinstance(data, list) else set()

    def _apply_overlay(
        self, base: ContentBundle, overlay: ContentBundle, tombstones: set[str]
    ) -> None:
        """Overlay ``overlay`` onto ``base`` in place: override id-keyed objects, union relations,
        then remove tombstoned ``"kind:id"`` entries. (Relation removal is a later refinement.)"""
        base.entities.update(overlay.entities)
        base.quests.update(overlay.quests)
        base.regions.update(overlay.regions)
        base.pois.update(overlay.pois)
        base.dialogues.update(overlay.dialogues)
        base.dialogue_trees.update(overlay.dialogue_trees)
        base.localized_texts.update(overlay.localized_texts)
        base.terms.update(overlay.terms)
        base.style_guides.update(overlay.style_guides)
        base.quest_event_refs.update(overlay.quest_event_refs)
        base.relations.extend(overlay.relations)
        if not tombstones:
            return
        collections: dict[str, dict[str, Any]] = {
            "entity": base.entities,
            "quest": base.quests,
            "region": base.regions,
            "poi": base.pois,
            "dialogue": base.dialogues,
            "dialogue_tree": base.dialogue_trees,
            "localized_text": base.localized_texts,
            "term": base.terms,
            "style_guide": base.style_guides,
            "quest_event_ref": base.quest_event_refs,
        }
        for entry in tombstones:
            kind, _, oid = entry.partition(":")
            coll = collections.get(kind)
            if coll is not None:
                coll.pop(oid, None)

    def create_version(self, version: str, *, base_version: str = _BASELINE_VERSION) -> None:
        """Branch a derived ``version`` from ``base_version`` (copy-on-write: no content copied).

        Writes only ``versions/<version>/version.json`` recording the base; the override dir starts
        empty, so ``load_scoped(version=<version>)`` immediately equals its base until content is
        saved into it. Refuses the baseline name, an existing version, an unknown base, or a base
        cycle -- keeping ``_resolve_version_chain`` acyclic by construction."""
        if version == _BASELINE_VERSION:
            raise ValueError(f"{_BASELINE_VERSION!r} is the baseline version and cannot be created")
        _validate_id_chars(version, context="create_version", forbidden=_FORBIDDEN_ID_CHARS)
        vdir = self.root / "versions" / version
        if (vdir / "version.json").exists():
            raise ValueError(f"version {version!r} already exists")
        if (
            base_version != _BASELINE_VERSION
            and not (self.root / "versions" / base_version / "version.json").exists()
        ):
            raise ValueError(f"base version {base_version!r} does not exist")
        if version in self._resolve_version_chain(base_version):
            raise ValueError(f"creating {version!r} on base {base_version!r} would form a cycle")
        vdir.mkdir(parents=True, exist_ok=True)
        (vdir / "version.json").write_text(
            _json({"version": version, "base_version": base_version}), encoding="utf-8"
        )
        self._parse_cache.clear()

    def save_scoped(self, bundle: ContentBundle, *, version: str = _BASELINE_VERSION) -> None:
        """Persist ``bundle`` as ``version``, writing only its diff from the base (copy-on-write).

        Baseline ``v1`` writes the whole bundle to the root tree (== ``save``). A derived version
        writes to ``versions/<version>/`` only objects that differ from / are new vs its resolved
        base, plus a ``tombstones.json`` for base objects the bundle drops -- so an unchanged object
        is never duplicated into the override, and ``load_scoped(version=version)`` round-trips the
        saved bundle. (Removing a *relation* present in the base is not yet supported; relations
        union -- a later refinement, matching the overlay.)"""
        if version == _BASELINE_VERSION:
            self.save(bundle)
            return
        if not (self.root / "versions" / version / "version.json").exists():
            raise ValueError(f"version {version!r} does not exist; call create_version first")
        base = self.load_scoped(version=self._version_base(version) or _BASELINE_VERSION)
        diff, tombstones = self._diff_bundle(bundle, base)
        self._parse_cache.clear()
        self._write_bundle_to(self.root / "versions" / version, diff)
        (self.root / "versions" / version / "tombstones.json").write_text(
            _json(sorted(tombstones)), encoding="utf-8"
        )

    def _diff_bundle(
        self, bundle: ContentBundle, base: ContentBundle
    ) -> tuple[ContentBundle, list[str]]:
        """Copy-on-write diff of ``bundle`` vs ``base``: a bundle of only the overridden/new
        objects, and the ``"kind:id"`` tombstones for base objects ``bundle`` drops."""
        diff = ContentBundle()
        tombstones: list[str] = []
        dict_kinds = (
            ("entity", "entities"),
            ("region", "regions"),
            ("quest", "quests"),
            ("poi", "pois"),
            ("dialogue", "dialogues"),
            ("dialogue_tree", "dialogue_trees"),
            ("localized_text", "localized_texts"),
            ("term", "terms"),
            ("style_guide", "style_guides"),
            ("quest_event_ref", "quest_event_refs"),
        )
        for kind, attr in dict_kinds:
            current: dict[str, Any] = getattr(bundle, attr)
            base_coll: dict[str, Any] = getattr(base, attr)
            override = {oid: obj for oid, obj in current.items() if base_coll.get(oid) != obj}
            getattr(diff, attr).update(override)
            tombstones += [f"{kind}:{oid}" for oid in base_coll if oid not in current]
        diff.relations = [r for r in bundle.relations if r not in base.relations]
        return diff, tombstones

    def save(self, bundle: ContentBundle) -> None:
        # A save rewrites files in place; mtime_ns + size usually shifts, but an edit that preserves
        # both on a same-second filesystem could otherwise let the parse cache serve the pre-save
        # value. save() is rare relative to load(), so drop the whole cache and let the next load
        # re-stat -- correctness over a micro-optimisation on the write path.
        self._parse_cache.clear()
        self._write_bundle_to(self.root, bundle)

    def _write_bundle_to(self, root: Path, bundle: ContentBundle) -> None:
        """Write a full ``ContentBundle`` to an arbitrary content root (baseline or a version dir).

        Scale-P0 G2-C C3b: factored out of ``save`` so ``save_scoped`` can write a version's
        override tree through the same writers. For ``self.root`` this is the pre-C3 ``save``."""
        self._write_json_dir(root / "world" / "entities", bundle.entities)
        self._write_relations(root, bundle.relations)
        self._write_quest_event_refs(root, bundle.quest_event_refs)
        self._write_json_dir(root / "regions", bundle.regions)
        self._write_json_dir(root / "quests", bundle.quests)
        self._write_json_dir(root / "pois", bundle.pois)
        self._write_json_dir(root / "dialogues", bundle.dialogues)
        self._write_json_dir(root / "dialogues" / "trees", bundle.dialogue_trees)
        self._write_json_dir(root / "localization" / "texts", bundle.localized_texts)
        self._write_terms(root, bundle.terms)
        self._write_style_guides(root, bundle.style_guides)

    def exists(self) -> bool:
        return self.root.exists()

    def _read_parsed(self, file_path: Path, parse: Callable[[str], Any]) -> Any:
        """Read + parse ``file_path``, reusing the cache when its (mtime, size) is unchanged.

        ``parse`` turns the file's text into the value to cache (a model, a list, …). The cache key
        folds in both the modification time (nanosecond precision) and the byte size, so an in-place
        edit that happens to preserve one still misses on the other. A changed or vanished file
        re-reads from disk -- files remain the source of truth and the fast path never serves
        stale data. The cache holds the value, not the text, so an unchanged file skips re-parsing.
        """
        stat = file_path.stat()
        key = str(file_path.resolve())
        cached = self._parse_cache.get(key)
        if cached is not None and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            self._cache_hits += 1
            return cached[2]
        self._cache_misses += 1
        value = parse(file_path.read_text(encoding="utf-8"))
        self._parse_cache[key] = (stat.st_mtime_ns, stat.st_size, value)
        return value

    def _load_json_dir(self, path: Path, model: type[ModelT]) -> list[ModelT]:
        if not path.exists():
            return []
        loaded: list[ModelT] = []
        for file_path in sorted(path.glob("*.json")):
            loaded.append(self._read_parsed(file_path, model.model_validate_json))
        return loaded

    def _load_relations(self, root: Path) -> list[Relation]:
        path = root / "world" / "relations.jsonl"
        if not path.exists():
            return []

        def parse(text: str) -> list[Relation]:
            return [
                Relation.model_validate_json(raw) for raw in text.splitlines() if raw.strip()
            ]

        return self._read_parsed(path, parse)

    def _load_quest_event_refs(self, root: Path) -> dict[str, QuestEventReference]:
        path = root / "quests" / "event_refs.jsonl"
        if not path.exists():
            return {}

        def parse(text: str) -> dict[str, QuestEventReference]:
            refs: dict[str, QuestEventReference] = {}
            for raw in text.splitlines():
                if raw.strip():
                    ref = QuestEventReference.model_validate_json(raw)
                    refs[ref.id] = ref
            return refs

        return self._read_parsed(path, parse)

    def _load_style_guides(self, root: Path) -> list[StyleGuide]:
        """Full-fidelity JSON is canonical; an old body-only ``style_guide.md`` still loads (so
        worlds saved before this format upgrade keep working, just without their rules)."""
        path = root / "world" / "style_guides.json"
        if path.exists():

            def parse_guides(text: str) -> list[StyleGuide]:
                data = json.loads(text)
                if isinstance(data, dict):
                    return [StyleGuide.model_validate(raw) for raw in data.values()]
                return []

            return self._read_parsed(path, parse_guides)
        legacy = root / "world" / "style_guide.md"
        if legacy.exists():
            return self._read_parsed(legacy, lambda text: [StyleGuide(body=text)])
        return []

    def _load_terms(self, root: Path) -> list[Term]:
        path = root / "world" / "terms.json"
        if not path.exists():
            return []

        def parse_terms(text: str) -> list[Term]:
            data = json.loads(text)
            if isinstance(data, list):
                return [Term.model_validate(item) for item in data]
            if isinstance(data, dict):
                return [
                    Term.model_validate(item)
                    for item in data.values()
                    if isinstance(item, dict)
                ]
            return []

        return self._read_parsed(path, parse_terms)

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

    def _write_relations(self, root: Path, relations: list[Relation]) -> None:
        path = root / "world" / "relations.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [_json_line(relation) for relation in relations]
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def _write_quest_event_refs(self, root: Path, refs: dict[str, QuestEventReference]) -> None:
        path = root / "quests" / "event_refs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [_json_line(ref) for ref in refs.values()]
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def _write_terms(self, root: Path, terms: dict[str, Term]) -> None:
        path = root / "world" / "terms.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = [term.model_dump(mode="json", exclude_none=True) for term in terms.values()]
        path.write_text(_json(payload), encoding="utf-8")

    def _write_style_guides(self, root: Path, style_guides: dict[str, StyleGuide]) -> None:
        """Persist EVERY style guide with all its fields (id, body, rules, …) — the old code only
        wrote the single ``"style_guide"`` key's ``body``, silently dropping rules and any other
        guide. The legacy ``.md`` is removed once the full-fidelity JSON exists."""
        path = root / "world" / "style_guides.json"
        legacy = root / "world" / "style_guide.md"
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
