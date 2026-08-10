from __future__ import annotations

import copy
import json
import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts.bootstrap_team_genesis import _parser as _bootstrap_cli_parser

from owcopilot.content.models import (
    POI,
    ContentBundle,
    DialogueRef,
    DialogueTree,
    Entity,
    EntityType,
    LocalizedText,
    Quest,
    QuestEventReference,
    RegionBrief,
    Relation,
    SourceRef,
    StyleGuide,
    StyleRubric,
    Term,
)
from owcopilot.content.store import ContentStore
from owcopilot.contracts import (
    ContentObjectVersion,
    ContentRevision,
    OwnershipScope,
    ReleasePointer,
    RevisionMembership,
)
from owcopilot.contracts.revisions import revision_manifest_hash
from owcopilot.teamdb.bootstrap import (
    BootstrapCompletion,
    BootstrapImportRun,
    BootstrapPhase,
    BootstrapProtocolError,
    BootstrapRepository,
    BootstrapTransaction,
    LegacyRegistryEntry,
    bootstrap_from_legacy_archive,
    bootstrap_genesis,
)
from owcopilot.teamdb.canonical_mapping import (
    GenesisPlan,
    LegacyCanonSnapshot,
    map_genesis,
    relation_legacy_tombstone_key,
    relation_set_logical_id,
    resolve_legacy_canon,
)
from owcopilot.teamdb.legacy_archive import (
    CutoverBlockedError,
    LegacyArchiveError,
    LegacyArchivePackage,
    create_legacy_archive,
    restore_legacy_archive,
    verify_legacy_archive,
)

_NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
_SCOPE = OwnershipScope(
    tenant_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    container_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
)
_ACTOR = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def test_bootstrap_cli_requires_explicit_legacy_sqlite_inventory_decision() -> None:
    parser = _bootstrap_cli_parser()
    base = ["--content-root", "content", "--archive", "legacy.zip", "--preflight-only"]
    with pytest.raises(SystemExit):
        parser.parse_args(base)
    assert parser.parse_args([*base, "--no-legacy-sqlite"]).no_legacy_sqlite
    assert parser.parse_args([*base, "--sqlite", "ledger=ledger.sqlite3"]).sqlite == [
        "ledger=ledger.sqlite3"
    ]
    with pytest.raises(SystemExit):
        parser.parse_args([*base, "--sqlite", "ledger=ledger.sqlite3", "--no-legacy-sqlite"])


_OTHER_ACTOR = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"


def _all_collection_bundle() -> ContentBundle:
    relation = Relation(
        source="npc_a",
        target="region_a",
        kind="guards:at",
        metadata={"rank": 2},
        source_ref=SourceRef(path="imports/world.xlsx", sheet="Relations", row=7),
    )
    return ContentBundle(
        entities={"npc_a": Entity(id="npc_a", name="阿岚", type=EntityType.NPC)},
        relations=[relation, relation.model_copy(deep=True)],
        quest_event_refs={
            "qer_a": QuestEventReference(id="qer_a", quest_id="quest_a", event_id="event_a")
        },
        quests={"quest_a": Quest(id="quest_a", title="守港")},
        regions={"region_a": RegionBrief(id="region_a", name="北港")},
        pois={"poi_a": POI(id="poi_a", name="灯塔")},
        dialogues={"dialogue_a": DialogueRef(id="dialogue_a", text_key="dlg.a")},
        dialogue_trees={"tree_a": DialogueTree(id="tree_a")},
        localized_texts={
            "text_a": LocalizedText(id="text_a", text_key="dlg.a", locale="zh-CN", text="站住。")
        },
        terms={"term_a": Term(id="term_a", canonical="潮誓")},
        style_guides={"guide_a": StyleGuide(id="guide_a", body="冷峻。")},
        style_rubrics={"rubric_a": StyleRubric(id="rubric_a", label="冷峻")},
    )


def test_canonical_mapping_covers_eleven_collections_relations_and_tombstones() -> None:
    bundle = _all_collection_bundle()
    deleted_relation_key = "relation:old_source:old:kind:old_target"
    snapshot = LegacyCanonSnapshot(
        bundle=bundle,
        resolved_version="work__3",
        active_tombstones=("entity:deleted_npc", deleted_relation_key),
    )

    plan = map_genesis(snapshot, created_at=_NOW, created_by="bootstrap:test")

    live_ids = {item.logical_id for item in plan.object_versions}
    assert {
        "entity:npc_a",
        "quest:quest_a",
        "region:region_a",
        "poi:poi_a",
        "dialogue:dialogue_a",
        "dialogue_tree:tree_a",
        "localized_text:text_a",
        "term:term_a",
        "style_guide:guide_a",
        "style_rubric:rubric_a",
        "quest_event_ref:qer_a",
    }.issubset(live_ids)
    relation_key = relation_legacy_tombstone_key(bundle.relations[0])
    relation_id = relation_set_logical_id(relation_key)
    assert relation_id in live_ids
    relation_version = next(item for item in plan.object_versions if item.logical_id == relation_id)
    payload = json.loads(relation_version.payload_json)
    assert payload["legacy_tombstone_key"] == relation_key
    assert len(payload["relations"]) == 2  # exact duplicates are retained
    assert payload["relations"][0]["source_ref"] == {
        "path": "imports/world.xlsx",
        "row": 7,
        "sheet": "Relations",
    }
    memberships = {item.logical_id: item for item in plan.memberships}
    assert memberships["entity:deleted_npc"].tombstone
    assert memberships[relation_set_logical_id(deleted_relation_key)].tombstone
    assert plan.revision.parent_revision is None
    assert plan.revision.manifest_hash == revision_manifest_hash(plan.memberships)
    assert plan.release_pointer == ReleasePointer(
        authoring_head=plan.revision.revision_id,
        serving_head=None,
    )

    reordered = bundle.model_copy(deep=True)
    reordered.relations.reverse()
    repeated = map_genesis(
        LegacyCanonSnapshot(reordered, "work__3", snapshot.active_tombstones),
        created_at=_NOW,
        created_by="bootstrap:test",
    )
    assert repeated.object_versions == plan.object_versions
    assert repeated.memberships == plan.memberships
    assert repeated.revision.revision_id == plan.revision.revision_id


def test_resolve_legacy_canon_keeps_only_current_effective_tombstones(tmp_path: Path) -> None:
    store = ContentStore(tmp_path / "synthetic-content")
    baseline = ContentBundle(
        entities={
            "keep": Entity(id="keep", name="保留", type=EntityType.NPC),
            "remove": Entity(id="remove", name="删除", type=EntityType.NPC),
        },
        relations=[Relation(source="keep", target="remove", kind="knows")],
    )
    store.save(baseline)
    store.create_version("v2", base_version="v1")
    resolved = baseline.model_copy(deep=True)
    del resolved.entities["remove"]
    resolved.relations.clear()
    store.save_scoped(resolved, version="v2")

    snapshot = resolve_legacy_canon(store, version="v2")

    assert snapshot.bundle == resolved
    assert snapshot.active_tombstones == (
        "entity:remove",
        "relation:keep:knows:remove",
    )


@pytest.mark.parametrize(
    "head_json",
    ["[]", "{}", '{"working":1}', '{"working":"missing-version"}'],
)
def test_resolve_legacy_canon_rejects_malformed_head_and_missing_working_directory(
    tmp_path: Path,
    head_json: str,
) -> None:
    root = tmp_path / "content"
    (root / "versions").mkdir(parents=True)
    (root / "versions" / "_head.json").write_text(head_json, encoding="utf-8")
    with pytest.raises(ValueError, match="head|does not exist"):
        resolve_legacy_canon(ContentStore(root))


@pytest.mark.parametrize(
    ("metadata", "match"),
    [
        ("[]", "JSON object"),
        ('{"base_version":[]}', "base_version"),
        ('{"base_version":"missing-base"}', "does not exist"),
    ],
)
def test_resolve_legacy_canon_strictly_validates_the_complete_base_chain(
    tmp_path: Path,
    metadata: str,
    match: str,
) -> None:
    root = tmp_path / "content"
    version = root / "versions" / "v2"
    version.mkdir(parents=True)
    (version / "version.json").write_text(metadata, encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        resolve_legacy_canon(ContentStore(root), version="v2")


def _make_sqlite(path: Path, *, proposed: bool = False, unknown: bool = False) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE patches (id TEXT PRIMARY KEY, status TEXT NOT NULL);
            CREATE TABLE telemetry (id INTEGER PRIMARY KEY, task_type TEXT NOT NULL);
            """
        )
        connection.execute(
            "INSERT INTO patches (id, status) VALUES (?, ?)",
            ("patch-1", "proposed" if proposed else "applied"),
        )
        connection.execute("INSERT INTO telemetry (id, task_type) VALUES (1, 'draft')")
        if unknown:
            connection.execute("CREATE TABLE surprise_state (id TEXT PRIMARY KEY)")
            connection.execute("INSERT INTO surprise_state (id) VALUES ('mystery')")
        connection.commit()
    finally:
        connection.close()


def _archive_inputs(tmp_path: Path, *, proposed: bool = False, unknown: bool = False):
    content = tmp_path / "synthetic-content"
    (content / "world" / "entities").mkdir(parents=True)
    (content / "empty-dir").mkdir()
    (content / "world" / "entities" / "npc.json").write_text(
        '{"id":"npc","name":"守卫"}\n', encoding="utf-8", newline=""
    )
    database = tmp_path / "legacy.sqlite3"
    _make_sqlite(database, proposed=proposed, unknown=unknown)
    return content, database


def test_archive_is_deterministic_restorable_and_records_terminal_dispositions(
    tmp_path: Path,
) -> None:
    content, database = _archive_inputs(tmp_path)
    first = create_legacy_archive(
        content_root=content,
        sqlite_databases={"runtime": database},
        archive_path=tmp_path / "legacy-a.zip",
    )
    second = create_legacy_archive(
        content_root=content,
        sqlite_databases={"runtime": database},
        archive_path=tmp_path / "legacy-b.zip",
    )

    assert first.archive_sha256 == second.archive_sha256
    assert first.archive_path.read_bytes() == second.archive_path.read_bytes()
    assert first.manifest.cutover_safe
    patch = next(row for row in first.manifest.sqlite_records if row.table == "patches")
    assert patch.record_class == "archive-only"
    assert patch.source_status == "applied"
    assert patch.disposition == "preserve_read_only"
    assert (
        verify_legacy_archive(first.archive_path, expected_archive_sha256=first.archive_sha256)
        == first.manifest
    )

    restored = tmp_path / "restored"
    restored_manifest = restore_legacy_archive(
        first.archive_path,
        restored,
        expected_archive_sha256=first.archive_sha256,
    )
    assert restored_manifest == first.manifest
    assert (restored / "content" / "empty-dir").is_dir()
    assert (restored / "content" / "world" / "entities" / "npc.json").read_bytes() == (
        content / "world" / "entities" / "npc.json"
    ).read_bytes()
    assert (restored / "sqlite" / "runtime.sqlite3").read_bytes() == database.read_bytes()

    tampered = tmp_path / "tampered.zip"
    damaged = bytearray(first.archive_path.read_bytes())
    damaged[len(damaged) // 2] ^= 0x01
    tampered.write_bytes(damaged)
    with pytest.raises(LegacyArchiveError):
        verify_legacy_archive(tampered)


def test_archive_is_write_once_and_never_overwrites_prior_custody(tmp_path: Path) -> None:
    content, database = _archive_inputs(tmp_path)
    target = tmp_path / "legacy.zip"
    first = create_legacy_archive(
        content_root=content,
        sqlite_databases={"runtime": database},
        archive_path=target,
    )
    original = target.read_bytes()
    (content / "world" / "entities" / "npc.json").write_text(
        '{"id":"npc","name":"已变化"}\n', encoding="utf-8", newline=""
    )

    with pytest.raises(LegacyArchiveError, match="refusing overwrite"):
        create_legacy_archive(
            content_root=content,
            sqlite_databases={"runtime": database},
            archive_path=target,
        )
    assert target.read_bytes() == original
    assert verify_legacy_archive(target) == first.manifest


def test_archive_atomic_publish_does_not_clobber_concurrent_creator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import owcopilot.teamdb.legacy_archive as archive_module

    temporary = tmp_path / "complete.tmp"
    target = tmp_path / "legacy.zip"
    temporary.write_bytes(b"ours")
    real_link = archive_module.os.link

    def competing_link(source: object, destination: object, **kwargs: object) -> None:
        Path(destination).write_bytes(b"competitor")
        real_link(source, destination, **kwargs)

    monkeypatch.setattr(archive_module.os, "link", competing_link)
    with pytest.raises(LegacyArchiveError, match="different bytes"):
        archive_module._publish_file_noreplace(temporary, target)
    assert target.read_bytes() == b"competitor"


def test_restore_atomic_publish_does_not_replace_existing_directory(tmp_path: Path) -> None:
    import owcopilot.teamdb.legacy_archive as archive_module

    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "ours.txt").write_text("ours", encoding="utf-8")
    target = tmp_path / "restore"
    target.mkdir()
    (target / "theirs.txt").write_text("theirs", encoding="utf-8")
    with pytest.raises(LegacyArchiveError, match="appeared concurrently"):
        archive_module._rename_directory_noreplace(staged, target)
    assert (target / "theirs.txt").read_text(encoding="utf-8") == "theirs"
    assert not (target / "ours.txt").exists()


@pytest.mark.parametrize(
    ("proposed", "unknown", "expected_class"),
    [(True, False, "blocker"), (False, True, "unknown")],
)
def test_archive_preflight_blocks_active_and_unknown_records(
    tmp_path: Path,
    proposed: bool,
    unknown: bool,
    expected_class: str,
) -> None:
    content, database = _archive_inputs(tmp_path, proposed=proposed, unknown=unknown)
    package = create_legacy_archive(
        content_root=content,
        sqlite_databases={"runtime": database},
        archive_path=tmp_path / "unsafe.zip",
    )
    assert expected_class in {row.record_class for row in package.manifest.blockers}
    with pytest.raises(CutoverBlockedError):
        package.require_cutover_safe()


def test_reference_source_rows_block_until_explicitly_migrated_or_adjudicated(
    tmp_path: Path,
) -> None:
    content, database = _archive_inputs(tmp_path)
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE reference_sources (id TEXT PRIMARY KEY, title TEXT)")
        connection.execute("INSERT INTO reference_sources VALUES ('source-1', '冻结素材')")
        connection.commit()
    finally:
        connection.close()
    package = create_legacy_archive(
        content_root=content,
        sqlite_databases={"runtime": database},
        archive_path=tmp_path / "source-blocked.zip",
    )
    row = next(
        item for item in package.manifest.sqlite_records if item.table == "reference_sources"
    )
    assert row.record_class == "blocker"
    assert row.disposition == "must_migrate_or_resolve"
    with pytest.raises(CutoverBlockedError):
        package.require_cutover_safe()


def test_unknown_virtual_tables_and_same_prefix_normal_tables_are_not_skipped(
    tmp_path: Path,
) -> None:
    content, database = _archive_inputs(tmp_path)
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE VIRTUAL TABLE mystery_fts USING fts5(body)")
        connection.execute("INSERT INTO mystery_fts(body) VALUES ('must classify')")
        connection.execute("CREATE VIRTUAL TABLE content_fts USING fts5(body)")
        connection.execute("INSERT INTO content_fts(body) VALUES ('rebuildable')")
        connection.execute("CREATE TABLE content_fts_notes (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO content_fts_notes VALUES ('not a shadow')")
        connection.commit()
    finally:
        connection.close()
    package = create_legacy_archive(
        content_root=content,
        sqlite_databases={"runtime": database},
        archive_path=tmp_path / "virtual.zip",
    )
    blocker_tables = {item.table for item in package.manifest.blockers}
    assert "mystery_fts" in blocker_tables
    assert "content_fts_notes" in blocker_tables
    assert "content_fts" not in blocker_tables


def test_owner_gate_query_requires_active_owner_and_rejects_inactive_result() -> None:
    import owcopilot.teamdb.bootstrap as bootstrap_module

    class Result:
        def scalar_one_or_none(self) -> None:
            return None

    class Connection:
        statement: object | None = None

        def execute(self, statement: object) -> Result:
            self.statement = statement
            return Result()

    connection = Connection()
    with pytest.raises(BootstrapProtocolError, match="current tenant owner"):
        bootstrap_module._require_owner(connection, _SCOPE, _ACTOR)
    compiled = str(connection.statement.compile(compile_kwargs={"literal_binds": True}))
    assert "memberships.active IS true" in compiled
    assert "memberships.role = 'owner'" in compiled


def test_archive_rejects_sqlite_wal_and_detects_content_scan_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content, database = _archive_inputs(tmp_path)
    wal = Path(f"{database}-wal")
    wal.write_bytes(b"not-quiescent")
    with pytest.raises(LegacyArchiveError, match="quiescent"):
        create_legacy_archive(
            content_root=content,
            sqlite_databases={"runtime": database},
            archive_path=tmp_path / "wal.zip",
        )
    wal.unlink()

    import owcopilot.teamdb.legacy_archive as archive_module

    real_copy = archive_module._copy_stable_file
    changed = False

    def mutate_after_copy(source: Path, destination: Path) -> tuple[str, int]:
        nonlocal changed
        result = real_copy(source, destination)
        if not changed and source.suffix == ".json":
            changed = True
            source.write_bytes(source.read_bytes() + b"changed")
        return result

    monkeypatch.setattr(archive_module, "_copy_stable_file", mutate_after_copy)
    with pytest.raises(LegacyArchiveError, match="changed"):
        create_legacy_archive(
            content_root=content,
            sqlite_databases={"runtime": database},
            archive_path=tmp_path / "changed.zip",
        )


def test_archive_rejects_symlink_or_reparse_point(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content, database = _archive_inputs(tmp_path)
    link = content / "linked.json"
    try:
        os.symlink(content / "world" / "entities" / "npc.json", link)
    except OSError:
        import owcopilot.teamdb.legacy_archive as archive_module

        target = content / "world" / "entities" / "npc.json"
        real_lstat = archive_module.os.lstat

        def fake_lstat(path: object):
            observed = real_lstat(path)
            if Path(path) == target:
                return SimpleNamespace(
                    st_mode=observed.st_mode,
                    st_file_attributes=0x400,
                )
            return observed

        monkeypatch.setattr(archive_module.os, "lstat", fake_lstat)
    with pytest.raises(LegacyArchiveError, match="symlink|reparse"):
        create_legacy_archive(
            content_root=content,
            sqlite_databases={"runtime": database},
            archive_path=tmp_path / "link.zip",
        )


def test_archive_rejects_source_aliases_before_path_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import owcopilot.teamdb.legacy_archive as archive_module

    content, database = _archive_inputs(tmp_path)
    content_alias = tmp_path / "content-alias"
    database_alias = tmp_path / "database-alias.sqlite3"
    real_resolve = Path.resolve
    real_lstat = archive_module.os.lstat

    def fake_resolve(path: Path, *args: object, **kwargs: object) -> Path:
        if path == content_alias:
            return content
        if path == database_alias:
            return database
        return real_resolve(path, *args, **kwargs)

    def fake_lstat(path: object):
        candidate = Path(path)
        if candidate == content_alias:
            observed = real_lstat(content)
            return SimpleNamespace(
                st_mode=observed.st_mode,
                st_file_attributes=0x400,
            )
        if candidate == database_alias:
            observed = real_lstat(database)
            return SimpleNamespace(
                st_mode=observed.st_mode,
                st_file_attributes=0x400,
            )
        return real_lstat(path)

    monkeypatch.setattr(Path, "resolve", fake_resolve)
    monkeypatch.setattr(archive_module.os, "lstat", fake_lstat)
    with pytest.raises(LegacyArchiveError, match="symlink|reparse"):
        create_legacy_archive(
            content_root=content_alias,
            sqlite_databases={"runtime": database},
            archive_path=tmp_path / "root-alias.zip",
        )
    with pytest.raises(LegacyArchiveError, match="symlink|reparse"):
        create_legacy_archive(
            content_root=content,
            sqlite_databases={"runtime": database_alias},
            archive_path=tmp_path / "database-alias.zip",
        )


@dataclass
class _MemoryState:
    runs: dict[str, BootstrapImportRun] = field(default_factory=dict)
    registry: dict[str, LegacyRegistryEntry] = field(default_factory=dict)
    objects: dict[str, ContentObjectVersion] = field(default_factory=dict)
    revisions: dict[str, ContentRevision] = field(default_factory=dict)
    memberships: dict[str, tuple[RevisionMembership, ...]] = field(default_factory=dict)
    pointer: ReleasePointer | None = None
    completions: dict[str, BootstrapCompletion] = field(default_factory=dict)
    events: dict[str, tuple[BootstrapCompletion, str]] = field(default_factory=dict)


class _MemoryRepository(BootstrapRepository):
    def __init__(self, *, authorized_actors: Iterable[str] = (_ACTOR,)) -> None:
        self.state = _MemoryState()
        self.authorized_actors = frozenset(authorized_actors)

    def authorize_import(self, scope: OwnershipScope, *, actor_subject: str) -> None:
        assert scope == _SCOPE
        assert actor_subject in self.authorized_actors

    def begin_import_run(
        self,
        scope: OwnershipScope,
        run: BootstrapImportRun,
    ) -> BootstrapImportRun:
        del scope
        for existing in self.state.runs.values():
            if existing.idempotency_key == run.idempotency_key:
                return existing
            if existing.status in {"BUILDING", "COMPLETED"}:
                raise BootstrapProtocolError("competing source")
        self.state.runs[run.import_run_id] = run
        return run

    @contextmanager
    def import_transaction(self, scope: OwnershipScope) -> Iterator[BootstrapTransaction]:
        del scope
        working = copy.deepcopy(self.state)
        transaction = _MemoryTransaction(working)
        yield transaction
        self.state = working


class _MemoryTransaction(BootstrapTransaction):
    def __init__(self, state: _MemoryState) -> None:
        self.state = state

    def lock_import_run(self, import_run_id: str) -> BootstrapImportRun:
        return self.state.runs[import_run_id]

    def assert_genesis_target_empty(self) -> None:
        if self.state.pointer is not None or self.state.revisions:
            raise BootstrapProtocolError("not empty")

    def register_legacy_archive(self, entries: Iterable[LegacyRegistryEntry]) -> None:
        for entry in entries:
            if entry.relative_path in self.state.registry:
                raise BootstrapProtocolError("duplicate registry path")
            self.state.registry[entry.relative_path] = entry

    def put_object_versions(self, versions: Iterable[ContentObjectVersion]) -> None:
        for item in versions:
            prior = self.state.objects.setdefault(item.object_version_id, item)
            if prior != item:
                raise BootstrapProtocolError("identity collision")

    def put_revision(self, revision: ContentRevision, *, import_run_id: str) -> None:
        assert import_run_id in self.state.runs
        self.state.revisions[revision.revision_id] = revision

    def put_memberships(
        self,
        revision_id: str,
        memberships: Iterable[RevisionMembership],
    ) -> None:
        self.state.memberships[revision_id] = tuple(memberships)

    def initialize_release_pointer(self, pointer: ReleasePointer) -> None:
        if self.state.pointer is not None:
            raise BootstrapProtocolError("pointer exists")
        self.state.pointer = pointer

    def record_genesis_imported(
        self,
        completion: BootstrapCompletion,
        *,
        actor_id: str,
    ) -> None:
        self.state.events[completion.import_run_id] = (completion, actor_id)

    def complete_import_run(self, completion: BootstrapCompletion) -> None:
        run = self.state.runs[completion.import_run_id]
        assert run.status == "BUILDING"
        assert completion.genesis_revision_id in self.state.revisions
        assert completion.import_run_id in self.state.events
        self.state.runs[completion.import_run_id] = replace(
            run,
            status="COMPLETED",
            finished_at=completion.completed_at,
            genesis_revision_id=completion.genesis_revision_id,
        )
        self.state.completions[completion.import_run_id] = completion


class _InjectedKill(RuntimeError):
    pass


@pytest.fixture
def bootstrap_material(tmp_path: Path) -> tuple[GenesisPlan, LegacyArchivePackage]:
    content, database = _archive_inputs(tmp_path)
    package = create_legacy_archive(
        content_root=content,
        sqlite_databases={"runtime": database},
        archive_path=tmp_path / "legacy.zip",
    )
    plan = map_genesis(
        LegacyCanonSnapshot(
            bundle=ContentBundle(
                entities={"npc": Entity(id="npc", name="守卫", type=EntityType.NPC)}
            ),
            resolved_version="v1",
            active_tombstones=("quest:deleted",),
        ),
        created_at=_NOW,
        created_by=_ACTOR,
        source_manifest_hash=package.manifest.source_manifest_hash,
    )
    return plan, package


@pytest.mark.parametrize("kill_phase", list(BootstrapPhase))
def test_bootstrap_kill_boundaries_never_expose_half_genesis_and_retry_is_idempotent(
    bootstrap_material: tuple[GenesisPlan, LegacyArchivePackage],
    kill_phase: BootstrapPhase,
) -> None:
    plan, package = bootstrap_material
    repository = _MemoryRepository()

    def kill(phase: BootstrapPhase) -> None:
        if phase is kill_phase:
            raise _InjectedKill(phase.value)

    with pytest.raises(_InjectedKill, match=kill_phase.value):
        bootstrap_genesis(
            repository=repository,
            scope=_SCOPE,
            genesis=plan,
            archive=package,
            kill_hook=kill,
            now=lambda: _NOW,
        )

    run = next(iter(repository.state.runs.values()))
    if kill_phase is BootstrapPhase.TRANSACTION_COMMITTED:
        assert run.status == "COMPLETED"
        assert repository.state.pointer == plan.release_pointer
        assert len(repository.state.events) == 1
    else:
        assert run.status == "BUILDING"
        assert repository.state.pointer is None
        assert not repository.state.revisions
        assert not repository.state.memberships
        assert not repository.state.registry
        assert not repository.state.events

    recovered = bootstrap_genesis(
        repository=repository,
        scope=_SCOPE,
        genesis=plan,
        archive=package,
        now=lambda: _NOW,
    )
    assert recovered.completion.genesis_revision_id == plan.revision.revision_id
    assert repository.state.pointer == plan.release_pointer
    assert repository.state.pointer.serving_head is None
    assert len(repository.state.revisions) == 1
    assert len(repository.state.events) == 1
    event_completion, actor = next(iter(repository.state.events.values()))
    assert event_completion.archive_sha256 == package.archive_sha256
    assert event_completion.source_manifest_hash == package.manifest.source_manifest_hash
    assert actor == _ACTOR

    replay = bootstrap_genesis(
        repository=repository,
        scope=_SCOPE,
        genesis=plan,
        archive=package,
        now=lambda: _NOW,
    )
    assert replay.idempotent_replay
    assert len(repository.state.revisions) == 1
    assert len(repository.state.events) == 1


def test_building_retry_rejects_another_resolved_version_from_the_same_archive(
    tmp_path: Path,
) -> None:
    content = tmp_path / "versioned-content"
    store = ContentStore(content)
    store.save(ContentBundle(entities={"npc": Entity(id="npc", name="初始", type=EntityType.NPC)}))
    store.create_version("v2", base_version="v1")
    store.save_scoped(
        ContentBundle(entities={"npc": Entity(id="npc", name="后继", type=EntityType.NPC)}),
        version="v2",
    )
    package = create_legacy_archive(
        content_root=content,
        sqlite_databases={},
        archive_path=tmp_path / "versioned.zip",
    )
    plans = {
        version: map_genesis(
            resolve_legacy_canon(store, version=version),
            created_at=_NOW,
            created_by=_ACTOR,
            source_manifest_hash=package.manifest.source_manifest_hash,
        )
        for version in ("v1", "v2")
    }
    repository = _MemoryRepository()

    def kill_building(phase: BootstrapPhase) -> None:
        if phase is BootstrapPhase.BUILDING_COMMITTED:
            raise _InjectedKill(phase.value)

    with pytest.raises(_InjectedKill, match="building_committed"):
        bootstrap_genesis(
            repository=repository,
            scope=_SCOPE,
            genesis=plans["v1"],
            archive=package,
            kill_hook=kill_building,
            now=lambda: _NOW,
        )
    with pytest.raises(BootstrapProtocolError, match="competing source"):
        bootstrap_genesis(
            repository=repository,
            scope=_SCOPE,
            genesis=plans["v2"],
            archive=package,
            now=lambda: _NOW,
        )
    assert len(repository.state.runs) == 1
    assert next(iter(repository.state.runs.values())).status == "BUILDING"


@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize("drift", ["resolved_version", "created_at", "actor"])
def test_retry_identity_rejects_non_identifying_genesis_metadata_drift(
    bootstrap_material: tuple[GenesisPlan, LegacyArchivePackage],
    completed: bool,
    drift: str,
) -> None:
    plan, package = bootstrap_material
    repository = _MemoryRepository(authorized_actors=(_ACTOR, _OTHER_ACTOR))
    if completed:
        bootstrap_genesis(
            repository=repository,
            scope=_SCOPE,
            genesis=plan,
            archive=package,
            now=lambda: _NOW,
        )
    else:

        def kill_building(phase: BootstrapPhase) -> None:
            if phase is BootstrapPhase.BUILDING_COMMITTED:
                raise _InjectedKill(phase.value)

        with pytest.raises(_InjectedKill, match="building_committed"):
            bootstrap_genesis(
                repository=repository,
                scope=_SCOPE,
                genesis=plan,
                archive=package,
                kill_hook=kill_building,
                now=lambda: _NOW,
            )

    if drift == "resolved_version":
        changed = replace(plan, resolved_legacy_version="v2")
    elif drift == "created_at":
        changed = replace(
            plan,
            revision=plan.revision.model_copy(update={"created_at": _NOW + timedelta(seconds=1)}),
        )
    else:
        changed = replace(
            plan,
            revision=plan.revision.model_copy(update={"created_by": _OTHER_ACTOR}),
        )
    with pytest.raises(BootstrapProtocolError, match="competing source"):
        bootstrap_genesis(
            repository=repository,
            scope=_SCOPE,
            genesis=changed,
            archive=package,
            now=lambda: _NOW,
        )
    assert len(repository.state.runs) == 1


def test_bootstrap_refuses_unsafe_archive_before_creating_building_run(tmp_path: Path) -> None:
    content, database = _archive_inputs(tmp_path, proposed=True)
    package = create_legacy_archive(
        content_root=content,
        sqlite_databases={"runtime": database},
        archive_path=tmp_path / "unsafe.zip",
    )
    plan = map_genesis(
        LegacyCanonSnapshot(ContentBundle(), "v1", ()),
        created_at=_NOW,
        created_by=_ACTOR,
        source_manifest_hash=package.manifest.source_manifest_hash,
    )
    repository = _MemoryRepository()

    with pytest.raises(CutoverBlockedError):
        bootstrap_genesis(
            repository=repository,
            scope=_SCOPE,
            genesis=plan,
            archive=package,
            now=lambda: _NOW,
        )
    assert not repository.state.runs


def test_formal_bootstrap_derives_genesis_from_verified_restored_archive(tmp_path: Path) -> None:
    content = tmp_path / "valid-content"
    ContentStore(content).save(
        ContentBundle(
            entities={"npc": Entity(id="npc", name="守卫", type=EntityType.NPC)},
            relations=[Relation(source="npc", target="gate", kind="guards")],
        )
    )
    package = create_legacy_archive(
        content_root=content,
        sqlite_databases={},
        archive_path=tmp_path / "valid.zip",
    )
    repository = _MemoryRepository()

    result = bootstrap_from_legacy_archive(
        repository=repository,
        scope=_SCOPE,
        archive=package,
        legacy_version="v1",
        created_at=_NOW,
        created_by=_ACTOR,
        now=lambda: _NOW,
        scratch_parent=tmp_path,
    )

    assert result.completion.source_manifest_hash == package.manifest.source_manifest_hash
    assert {item.logical_id for item in repository.state.objects.values()} == {
        "entity:npc",
        relation_set_logical_id("relation:npc:guards:gate"),
    }


def test_low_level_bootstrap_rejects_archive_plan_substitution_and_bad_manifest(
    tmp_path: Path,
) -> None:
    content_a, database_a = _archive_inputs(tmp_path / "a")
    package_a = create_legacy_archive(
        content_root=content_a,
        sqlite_databases={"runtime": database_a},
        archive_path=tmp_path / "a.zip",
    )
    content_b, database_b = _archive_inputs(tmp_path / "b")
    (content_b / "world" / "entities" / "npc.json").write_text(
        '{"id":"other","name":"另一份"}\n', encoding="utf-8", newline=""
    )
    package_b = create_legacy_archive(
        content_root=content_b,
        sqlite_databases={"runtime": database_b},
        archive_path=tmp_path / "b.zip",
    )
    plan_a = map_genesis(
        LegacyCanonSnapshot(
            ContentBundle(entities={"npc": Entity(id="npc", name="守卫", type=EntityType.NPC)}),
            "v1",
            (),
        ),
        created_at=_NOW,
        created_by=_ACTOR,
        source_manifest_hash=package_a.manifest.source_manifest_hash,
    )
    repository = _MemoryRepository()

    with pytest.raises(BootstrapProtocolError, match="not bound"):
        bootstrap_genesis(
            repository=repository,
            scope=_SCOPE,
            genesis=plan_a,
            archive=package_b,
            now=lambda: _NOW,
        )
    assert not repository.state.runs

    broken = replace(plan_a, memberships=())
    with pytest.raises(BootstrapProtocolError, match="manifest_hash|exactly equal"):
        bootstrap_genesis(
            repository=repository,
            scope=_SCOPE,
            genesis=broken,
            archive=package_a,
            now=lambda: _NOW,
        )
    assert not repository.state.runs
