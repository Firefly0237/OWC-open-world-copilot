# SQLite 运行时存储：FTS5 全文 + vec0 向量 + WAL + scope 分片。
# 关键：面向长线叙事的大语料——磁盘量化/增量/分片而非全量进内存；索引与真源分离（真源仍是文件）。
"""SQLite runtime storage.

Content files remain the source of truth. This store holds rebuildable runtime state:
audit runs, issues, patches, graph edges, search index rows and telemetry.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from ..audit.models import AuditRun, Issue
from ..content.hash import content_hash
from ..content.models import ContentBundle, DialogueTree
from ..graph.index import ContentGraph
from ..llm.telemetry import CallRecord

if TYPE_CHECKING:
    from ..retrieval.vector_backend import (
        SqliteVecBackend,
        SqliteVecInt8Backend,
        VectorSearchBackend,
    )

logger = logging.getLogger(__name__)


# The canonical default scope. A single-world project (every project today)
# lives entirely under this scope, so a store opened without an explicit scope behaves exactly as
# before — same rows, same retrieval, same audit. The dimension is threaded everywhere now so the
# later layers (scope filtering, version inheritance) only have to *use* it, not add it.
DEFAULT_WORLD_ID = "default"
DEFAULT_VERSION = "v1"
_TELEMETRY_ERROR_CATEGORIES = frozenset(
    {
        "auth",
        "permission",
        "bad_request",
        "config",
        "output_budget",
        "timeout",
        "rate_limit",
        "conflict",
        "circuit_open",
        "deadline",
        "retry_budget",
        "connection",
        "server_error",
        "provider_error",
    }
)


class SQLiteStore:
    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        world_id: str = DEFAULT_WORLD_ID,
        version: str = DEFAULT_VERSION,
    ) -> None:
        self.path = str(path)
        # The store's *current* scope. Writes stamp rows with it; the vector/blob-cache reads are
        # scoped to it (a retriever only caches within its own scope), and vec0 PARTITION KEY search
        # is constrained to it — all of which keep the single-world default behaviour unchanged.
        # Broad scope-aware filtering of the content/audit read paths happens in retrieval.
        self.world_id = world_id
        self.version = version
        # check_same_thread=False: the LangGraph agent loop executes tools on ThreadPoolExecutor
        # worker threads (not the caller thread), so a store bound into a skill must be usable
        # off-thread. Safe under CPython's sqlite3 (threadsafety=3, module-level serialization);
        # same precedent as platform/store.py and multi_agent/session.py.
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # CJK bigram rewrite as a SQL function, so INSERT...SELECT repopulation paths can apply
        # the same transform as the parameterized write paths (one definition, no drift).
        self.conn.create_function("cjk_fts", 1, cjk_fts_text, deterministic=True)
        # WAL lets the Workbench read while the CLI writes (and vice versa); busy_timeout keeps
        # short lock contention from surfacing as immediate "database is locked" errors.
        # On :memory: databases WAL is a no-op and SQLite just reports "memory".
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.initialize()

    def set_scope(self, world_id: str, version: str) -> None:
        """Point the store at a (world_id, version) scope for subsequent writes/reads.

        Carrier only: ProjectContext sets this to its current scope. With the default
        ("default", "v1") this is a no-op vs the historical behaviour."""
        self.world_id = world_id
        self.version = version

    def close(self) -> None:
        self.conn.close()

    # --- version registry -----------------------------------------------------------------------
    # The table + basic CRUD. The base_version column records a version's parent for the
    # copy-on-write inheritance; this layer stores it but does NOT resolve the base chain -- the
    # content store's overlay resolution does that.

    def register_version(
        self, world_id: str, version: str, *, base_version: str | None = None
    ) -> None:
        """Create or update a (world_id, version) registry row. Idempotent on the composite key.

        ``base_version`` is the parent version this one branches from (``None`` for a root line).
        This layer only persists it; the inheritance/fork semantics live in the content store."""
        self.conn.execute(
            """
            INSERT INTO version_registry (world_id, version, base_version, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(world_id, version) DO UPDATE SET base_version = excluded.base_version
            """,
            (world_id, version, base_version, _now_iso()),
        )
        self.conn.commit()

    def get_version(self, world_id: str, version: str) -> dict[str, Any] | None:
        """Return the registry row for (world_id, version), or ``None`` if unregistered."""
        row = self.conn.execute(
            "SELECT world_id, version, base_version, created_at FROM version_registry "
            "WHERE world_id = ? AND version = ?",
            (world_id, version),
        ).fetchone()
        if row is None:
            return None
        return {
            "world_id": str(row["world_id"]),
            "version": str(row["version"]),
            "base_version": row["base_version"],
            "created_at": str(row["created_at"]),
        }

    def list_versions(self, world_id: str | None = None) -> list[dict[str, Any]]:
        """Registry rows, optionally filtered to one world, ordered by (world_id, version)."""
        if world_id is None:
            rows = self.conn.execute(
                "SELECT world_id, version, base_version, created_at FROM version_registry "
                "ORDER BY world_id, version"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT world_id, version, base_version, created_at FROM version_registry "
                "WHERE world_id = ? ORDER BY version",
                (world_id,),
            ).fetchall()
        return [
            {
                "world_id": str(row["world_id"]),
                "version": str(row["version"]),
                "base_version": row["base_version"],
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def delete_version(self, world_id: str, version: str) -> None:
        """Remove a (world_id, version) registry row (no-op if absent). Registry only — this
        does NOT cascade to the scope's content/vector rows (that lifecycle is later units)."""
        self.conn.execute(
            "DELETE FROM version_registry WHERE world_id = ? AND version = ?",
            (world_id, version),
        )
        self.conn.commit()

    def initialize(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS audit_runs (
                id TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                started_at TEXT NOT NULL,
                rule_set_version TEXT NOT NULL,
                totals_json TEXT NOT NULL,
                baseline_delta_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS issues (
                id TEXT PRIMARY KEY,
                rule_code TEXT NOT NULL,
                severity TEXT NOT NULL,
                category TEXT NOT NULL,
                target_ref TEXT NOT NULL,
                message TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                fingerprint TEXT,
                audit_run_id TEXT,
                status TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_issues_rule_code ON issues(rule_code);
            CREATE INDEX IF NOT EXISTS idx_issues_severity ON issues(severity);
            CREATE INDEX IF NOT EXISTS idx_issues_status ON issues(status);

            CREATE TABLE IF NOT EXISTS patches (
                id TEXT PRIMARY KEY,
                issue_id TEXT,
                status TEXT NOT NULL,
                ops_json TEXT NOT NULL,
                rationale TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                origin TEXT NOT NULL,
                applied_by TEXT,
                applied_at TEXT,
                rollback_ops_json TEXT,
                rolled_back_by TEXT,
                rolled_back_at TEXT
            );

            CREATE TABLE IF NOT EXISTS review_items (
                id TEXT PRIMARY KEY,
                item_type TEXT NOT NULL,
                object_ref TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                issue_refs_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                decided_by TEXT,
                decided_at TEXT,
                critic_verdict TEXT,
                critic_score REAL
            );

            CREATE INDEX IF NOT EXISTS idx_patches_status ON patches(status);
            CREATE INDEX IF NOT EXISTS idx_patches_issue_id ON patches(issue_id);
            CREATE INDEX IF NOT EXISTS idx_review_items_status ON review_items(status);
            CREATE INDEX IF NOT EXISTS idx_review_items_type ON review_items(item_type);

            CREATE TABLE IF NOT EXISTS content_index (
                world_id TEXT NOT NULL DEFAULT 'default',
                version TEXT NOT NULL DEFAULT 'v1',
                ref TEXT NOT NULL,
                object_type TEXT NOT NULL,
                object_id TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                row_hash TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (world_id, version, ref)
            );

            CREATE TABLE IF NOT EXISTS content_vectors (
                ref TEXT NOT NULL,
                model_id TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                dim INTEGER NOT NULL,
                vector BLOB NOT NULL,
                world_id TEXT NOT NULL DEFAULT 'default',
                version TEXT NOT NULL DEFAULT 'v1',
                PRIMARY KEY (world_id, version, ref, model_id)
            );

            CREATE TABLE IF NOT EXISTS reference_vectors (
                ref TEXT NOT NULL,
                model_id TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                dim INTEGER NOT NULL,
                vector BLOB NOT NULL,
                world_id TEXT NOT NULL DEFAULT 'default',
                version TEXT NOT NULL DEFAULT 'v1',
                PRIMARY KEY (world_id, version, ref, model_id)
            );

            CREATE TABLE IF NOT EXISTS community_vectors (
                ref TEXT NOT NULL,
                model_id TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                dim INTEGER NOT NULL,
                vector BLOB NOT NULL,
                world_id TEXT NOT NULL DEFAULT 'default',
                version TEXT NOT NULL DEFAULT 'v1',
                PRIMARY KEY (world_id, version, ref, model_id)
            );

            CREATE TABLE IF NOT EXISTS graph_edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                kind TEXT NOT NULL,
                edge_type TEXT NOT NULL,
                valid_from INTEGER,
                valid_until INTEGER,
                edge_fingerprint TEXT NOT NULL DEFAULT '',
                world_id TEXT NOT NULL DEFAULT 'default',
                version TEXT NOT NULL DEFAULT 'v1'
            );

            CREATE TABLE IF NOT EXISTS telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_type TEXT NOT NULL,
                tier TEXT NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                cached_input_tokens INTEGER NOT NULL,
                cache_hit INTEGER NOT NULL,
                latency_ms REAL NOT NULL,
                cost_usd REAL NOT NULL,
                gateway_invocations INTEGER NOT NULL DEFAULT 0,
                transport_attempt_cap INTEGER NOT NULL DEFAULT 0,
                success INTEGER NOT NULL DEFAULT 1,
                error_category TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS content_fts USING fts5(
                ref UNINDEXED,
                object_type UNINDEXED,
                world_id UNINDEXED,
                version UNINDEXED,
                title,
                body
            );

            CREATE TABLE IF NOT EXISTS reference_sources (
                world_id TEXT NOT NULL DEFAULT 'default',
                version TEXT NOT NULL DEFAULT 'v1',
                id TEXT NOT NULL,
                title TEXT NOT NULL,
                source_type TEXT NOT NULL,
                original_filename TEXT,
                allowed_uses_json TEXT NOT NULL,
                -- 收录人声明的权利归属（owcopilot.inspiration.models.Provenance）。默认值取最
                -- 保守的那一项，与模型默认同向：没声明就按第三方参考对待。
                provenance TEXT NOT NULL DEFAULT 'third_party_reference',
                text_hash TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (world_id, version, id)
            );

            CREATE TABLE IF NOT EXISTS reference_chunks (
                world_id TEXT NOT NULL DEFAULT 'default',
                version TEXT NOT NULL DEFAULT 'v1',
                ref TEXT NOT NULL,
                source_id TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                PRIMARY KEY (world_id, version, ref)
            );

            CREATE INDEX IF NOT EXISTS idx_reference_chunks_source_id
                ON reference_chunks(source_id);

            CREATE VIRTUAL TABLE IF NOT EXISTS reference_fts USING fts5(
                ref UNINDEXED,
                source_id UNINDEXED,
                source_title UNINDEXED,
                world_id UNINDEXED,
                version UNINDEXED,
                title,
                body
            );

            CREATE TABLE IF NOT EXISTS community_reports (
                id TEXT PRIMARY KEY,             -- community id (c0…, c0.1…) or "_global"
                level TEXT NOT NULL,             -- community | global
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                member_refs_json TEXT NOT NULL,  -- provenance: the canon ids this report covers
                fingerprint TEXT NOT NULL,       -- cache key (members + their content hash)
                created_at TEXT NOT NULL,
                parent_id TEXT,                  -- hierarchical: parent id (NULL at level 0)
                hier_level INTEGER NOT NULL DEFAULT 0  -- Leiden hierarchy level (0 = coarsest)
            );

            CREATE TABLE IF NOT EXISTS lessons (
                id TEXT PRIMARY KEY,
                item_type TEXT NOT NULL,
                dimension TEXT NOT NULL DEFAULT 'general',
                lesson_text TEXT NOT NULL,
                false_pass_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_lessons_item_type_dim
                ON lessons(item_type, dimension);
            CREATE INDEX IF NOT EXISTS idx_lessons_last_seen_at ON lessons(last_seen_at);

            CREATE TABLE IF NOT EXISTS agent_messages (
                id           TEXT PRIMARY KEY,
                session_id   TEXT NOT NULL,
                from_agent   TEXT NOT NULL,
                to_agent     TEXT NOT NULL,
                msg_type     TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'pending'
            );

            CREATE INDEX IF NOT EXISTS idx_agent_messages_session
                ON agent_messages(session_id, to_agent, status);

            CREATE INDEX IF NOT EXISTS idx_agent_messages_type
                ON agent_messages(session_id, msg_type);

            CREATE INDEX IF NOT EXISTS idx_agent_messages_created
                ON agent_messages(created_at);

            CREATE TABLE IF NOT EXISTS version_registry (
                world_id TEXT NOT NULL DEFAULT 'default',
                version TEXT NOT NULL DEFAULT 'v1',
                base_version TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (world_id, version)
            );

            CREATE TABLE IF NOT EXISTS agent_runs (
                id              TEXT PRIMARY KEY,
                goal            TEXT NOT NULL,
                stop_reason     TEXT NOT NULL,
                step_count      INTEGER NOT NULL,
                cost_usd        REAL NOT NULL,
                latency_ms      REAL NOT NULL,
                agent_name      TEXT NOT NULL,
                transcript_json TEXT NOT NULL,
                created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_agent_runs_created
                ON agent_runs(created_at);

            CREATE TABLE IF NOT EXISTS agent_run_embeddings (
                run_id     TEXT PRIMARY KEY,
                model_id   TEXT NOT NULL,
                goal       TEXT NOT NULL,
                summary    TEXT NOT NULL,
                vector     BLOB NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                deleted_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_agent_run_embeddings_model
                ON agent_run_embeddings(model_id);

            CREATE TABLE IF NOT EXISTS reflections (
                id         TEXT PRIMARY KEY,
                goal       TEXT NOT NULL,
                reflection TEXT NOT NULL,
                source     TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                vector     BLOB,
                model_id   TEXT,
                deleted_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_reflections_model
                ON reflections(model_id);
            """
        )
        # Reliability telemetry added retry-layer counters and sanitized failure status. Upgrade
        # legacy project DBs in place; defaults preserve the meaning of every historical success.
        self._ensure_column("telemetry", "gateway_invocations", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("telemetry", "transport_attempt_cap", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("telemetry", "success", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column("telemetry", "error_category", "TEXT NOT NULL DEFAULT ''")
        # Older runtime DBs predate the rollback column; content files are the source of truth,
        # but runtime DBs should still upgrade in place rather than force a delete.
        self._ensure_column("patches", "rollback_ops_json", "TEXT")
        self._ensure_column("patches", "rolled_back_by", "TEXT")
        self._ensure_column("patches", "rolled_back_at", "TEXT")
        # 审计问题的六要素里，除任务 id 与原因之外的四项（文件、行号、节点 id、建议动作）。落库是
        # 因为"列出已存问题"这条出口是从这张表读回去的，不落库的话，报告在内存里带着六要素、一
        # 从库里查就退回两要素。旧库原地升级，历史行读回来是空值 = 未定位，与今天的行为一致。
        self._ensure_column("issues", "source_path", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("issues", "source_line", "INTEGER")
        self._ensure_column("issues", "node_ref", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("issues", "suggested_action", "TEXT NOT NULL DEFAULT ''")
        # The critic's final verdict/score, recorded at draft time so reviewer calibration can pair
        # it with the human decision later. Older runtime DBs upgrade in place.
        self._ensure_column("review_items", "critic_verdict", "TEXT")
        self._ensure_column("review_items", "critic_score", "REAL")
        # Primary failing dimension from last critique (dimension-aware lessons).
        self._ensure_column("review_items", "critic_primary_dim", "TEXT")
        # Episodic exemplars (T18): JSON array of rejected-draft object_refs attached to a lesson,
        # so a lesson carries concrete past instances (LangMem few-shot / feedback-memory failure
        # detail),
        # not only an aggregate count. Ensure-only (not in CREATE TABLE) mirrors critic_primary_dim;
        # legacy rows back-fill to NULL, i.e. today's exact behaviour.
        self._ensure_column("lessons", "exemplar_ids", "TEXT")
        # Lesson revocation (soft delete): a wrong/harmful lesson would otherwise be injected into
        # prompts forever. revoked_at tombstones it (get_lessons_for_type filters it out) and
        # revoked_reason records why — never a physical DELETE, matching the tombstone convention
        # used elsewhere. Ensure-only (same as exemplar_ids); legacy rows back-fill to NULL, i.e.
        # not revoked, so existing behaviour is unchanged.
        self._ensure_column("lessons", "revoked_at", "TEXT")
        self._ensure_column("lessons", "revoked_reason", "TEXT")
        # T7 memory lifecycle soft-delete tombstone. Over-capacity episodic memories are marked
        # (deleted_at = <iso>) rather than physically deleted, so recall filters them out but a
        # mistaken eviction stays recoverable (restore_* clears the tombstone). Ensure-only, like
        # the columns above; legacy rows back-fill to NULL = live, so existing behaviour is intact.
        self._ensure_column("agent_run_embeddings", "deleted_at", "TEXT")
        self._ensure_column("reflections", "deleted_at", "TEXT")
        # D1: recency-by-last-access: recency decays
        # from a memory's most recent *access*, not its creation. NULL until the first recall touch
        # (``agent.episodic``'s post-recall write-back); ``_recency()`` falls back to ``created_at``
        # for a never-recalled row, so a legacy/never-recalled row's ranking is unchanged.
        self._ensure_column("agent_run_embeddings", "last_accessed_at", "TEXT")
        self._ensure_column("reflections", "last_accessed_at", "TEXT")
        # D2: memory namespace (LangGraph long-term memory is namespace+key organized). The value is
        # the store's own ``world_id`` — the project/world boundary already threaded through every
        # other scoped table by ``ProjectContext`` — not a checkpointer ``thread_id``; see the
        # "Namespace / scope" note in ``agent/episodic.py`` for why that alternative was rejected.
        # Every legacy row back-fills to the canonical 'default' scope, so a single-world project's
        # existing recall/capacity behaviour is unchanged.
        self._ensure_column("agent_run_embeddings", "scope", "TEXT NOT NULL DEFAULT 'default'")
        self._ensure_column("reflections", "scope", "TEXT NOT NULL DEFAULT 'default'")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_run_embeddings_scope "
            "ON agent_run_embeddings(scope)"
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_reflections_scope ON reflections(scope)")
        # D3: the audit-driven importance signal of a run — how many deterministic-checker verdicts
        # it obtained and how large the findings/repairs those verdicts carried. Lifted out of the
        # transcript at write time (``agent.history.persist_agent_run`` calls
        # ``agent.episodic.run_audit_signal``), exactly like the other scalar columns, so recall
        # never parses a transcript. NULL = no signal recorded (a row written before D3, or one
        # saved by a caller that had no ``AgentResult``); ``agent.episodic._run_importance`` then
        # falls back to the pre-D3 step-count proxy, so a legacy row's ranking is unchanged.
        self._ensure_column("agent_runs", "audit_signal", "INTEGER")
        # Hierarchical GraphRAG (T13) added a parent link + level to community reports. Older
        # runtime DBs predate them; the NULL parent / 0 level defaults back-fill existing
        # single-level rows to their (correct) flat semantics, so a legacy index keeps working.
        self._ensure_column("community_reports", "parent_id", "TEXT")
        self._ensure_column("community_reports", "hier_level", "INTEGER NOT NULL DEFAULT 0")
        # P0 #2a incremental sync: content_index/graph_edges grew a content-derived hash/fingerprint
        # so re-opening a project diffs (upsert changed + prune removed) instead of dropping and
        # re-inserting the whole table. Older runtime DBs upgrade in place; the empty-string default
        # marks legacy rows as "unknown hash", so the first incremental sync re-stamps them.
        self._ensure_column("content_index", "row_hash", "TEXT NOT NULL DEFAULT ''")
        added_edge_fingerprint = self._ensure_column(
            "graph_edges", "edge_fingerprint", "TEXT NOT NULL DEFAULT ''"
        )
        if added_edge_fingerprint:
            # A legacy DB's existing edges all carry the '' default, which would collide under the
            # UNIQUE index below. graph_edges is fully rebuildable from the bundle on the next
            # open/reload, so clear it once here rather than back-filling fingerprints for rows we
            # are about to re-diff anyway.
            self.conn.execute("DELETE FROM graph_edges")
        # 素材来源归属（B2）。旧库原地升级：ALTER 后的历史行一律拿到最保守的默认值，下一次
        # sync 会与磁盘上的素材元数据比对，声明为自有/已授权的那些自动改正过来（
        # ``_reference_source_unchanged`` 把这一列也纳入比对，正是为了让这次改正发生）。
        # 必须排在下面的 PK 重建之前——重建按 PRAGMA 列清单逐列复制，列不存在就带不过去。
        self._ensure_column(
            "reference_sources", "provenance", "TEXT NOT NULL DEFAULT 'third_party_reference'"
        )
        # The (world_id, version) scope columns. Older runtime DBs predate them,
        # so upgrade in place; the 'default'/'v1' defaults back-fill every existing row to the
        # canonical scope, losing no data and keeping single-world behaviour unchanged. The scope
        # indexes are created *after* the columns exist (a legacy DB's CREATE TABLE IF NOT EXISTS
        # was a no-op, so the columns are added here, not by the table DDL above).
        scope_tables = (
            "content_index",
            "content_vectors",
            "reference_vectors",
            "community_vectors",
            "graph_edges",
            "reference_sources",
            "reference_chunks",
        )
        for table in scope_tables:
            self._ensure_column(table, "world_id", "TEXT NOT NULL DEFAULT 'default'")
            self._ensure_column(table, "version", "TEXT NOT NULL DEFAULT 'v1'")
        # The authoritative relational tables key on the object id; with scope, that id is only
        # unique *within* a scope, so the primary key must include (world_id, version). A legacy DB
        # (and a DB from an early cut) still has the pre-scope single-column PK — SQLite
        # cannot ALTER a PK, so rebuild those tables under the composite PK, carrying every existing
        # row across (back-filled to the canonical scope). This is what makes a write to one scope
        # physically unable to collide with / delete another scope's rows. Runs *before* the index
        # creation below because the rebuild drops/recreates the table.
        self._ensure_scoped_pk("content_index", ("world_id", "version", "ref"))
        self._ensure_scoped_pk("reference_sources", ("world_id", "version", "id"))
        self._ensure_scoped_pk("reference_chunks", ("world_id", "version", "ref"))
        # Scope vector blob caches too, so a shared DB cannot let the same ref in another scope
        # collide on upsert and thrash the embedding cache.
        self._ensure_scoped_pk("content_vectors", ("world_id", "version", "ref", "model_id"))
        self._ensure_scoped_pk("reference_vectors", ("world_id", "version", "ref", "model_id"))
        self._ensure_scoped_pk("community_vectors", ("world_id", "version", "ref", "model_id"))
        # graph_edges keeps its AUTOINCREMENT id; its per-edge uniqueness (the incremental-sync
        # ON CONFLICT key) becomes scope-qualified so the same fingerprint can exist once per scope.
        # Drop the legacy single-column unique index and (re)create the scoped one.
        self.conn.execute("DROP INDEX IF EXISTS idx_graph_edges_fingerprint")
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_graph_edges_scope_fingerprint "
            "ON graph_edges(world_id, version, edge_fingerprint)"
        )
        # The non-PK helper indexes are (re)created last: the PK rebuilds above drop+recreate their
        # tables, so any index must be created after, and the reference_chunks(source_id) lookup
        # index needs restoring too. CREATE INDEX IF NOT EXISTS keeps this idempotent.
        for table in scope_tables:
            self.conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_scope "  # noqa: S608 - whitelisted names
                f"ON {table}(world_id, version)"
            )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reference_chunks_source_id "
            "ON reference_chunks(source_id)"
        )
        # content_fts / reference_fts are derived search indexes, but they need a scope column so
        # the same ref in two scopes maps to two FTS rows and search can filter by scope. FTS5
        # virtual tables cannot be ALTERed, so a legacy table lacking the scope columns is dropped,
        # recreated under the new shape, and immediately repopulated from the authoritative tables.
        # Repopulating here (not relying on the next replace_* sync) is required for correctness:
        # the incremental sync skips unchanged content_index rows, so it would otherwise leave the
        # rebuilt FTS empty whenever a row's hash already matched — a silent search outage.
        rebuilt_content_fts = self._rebuild_fts_if_unscoped("content_fts", CONTENT_FTS_COLUMNS_DDL)
        if rebuilt_content_fts:
            self._repopulate_content_fts()
        rebuilt_reference_fts = self._rebuild_fts_if_unscoped(
            "reference_fts", REFERENCE_FTS_COLUMNS_DDL
        )
        if rebuilt_reference_fts:
            self._repopulate_reference_fts()
        # CJK bigram FTS generation (PRAGMA user_version = 1). A DB indexed before the bigram
        # rewrite holds whole-run CJK tokens that the bigram MATCH queries can no longer hit, so
        # rebuild+repopulate once. Tables just rebuilt by the scope migration above are already
        # bigram-indexed (the repopulate helpers apply cjk_fts) and are skipped. A fresh DB simply
        # repopulates empty tables and stamps the version.
        if int(self.conn.execute("PRAGMA user_version").fetchone()[0]) < 1:
            if not rebuilt_content_fts:
                self.conn.execute("DROP TABLE IF EXISTS content_fts")
                self.conn.execute(
                    f"CREATE VIRTUAL TABLE content_fts USING fts5({CONTENT_FTS_COLUMNS_DDL})"
                )
                self._repopulate_content_fts()
            if not rebuilt_reference_fts:
                self.conn.execute("DROP TABLE IF EXISTS reference_fts")
                self.conn.execute(
                    f"CREATE VIRTUAL TABLE reference_fts USING fts5({REFERENCE_FTS_COLUMNS_DDL})"
                )
                self._repopulate_reference_fts()
            self.conn.execute("PRAGMA user_version = 1")
        self.conn.commit()

    def _repopulate_content_fts(self) -> None:
        """Refill content_fts from the authoritative content_index (bigram-rewritten)."""
        self.conn.execute(
            "INSERT INTO content_fts (ref, object_type, world_id, version, title, body) "
            "SELECT ref, object_type, world_id, version, cjk_fts(title), cjk_fts(body) "
            "FROM content_index"
        )

    def _repopulate_reference_fts(self) -> None:
        """Refill reference_fts from the authoritative reference tables (bigram-rewritten)."""
        self.conn.execute(
            """
            INSERT INTO reference_fts (
                ref, source_id, source_title, world_id, version, title, body
            )
            SELECT c.ref, c.source_id, COALESCE(s.title, ''), c.world_id, c.version,
                   cjk_fts(c.title), cjk_fts(c.body)
            FROM reference_chunks AS c
            LEFT JOIN reference_sources AS s
                ON s.id = c.source_id AND s.world_id = c.world_id AND s.version = c.version
            """
        )

    def _ensure_column(self, table: str, column: str, column_type: str) -> bool:
        """Add ``column`` to ``table`` if missing. Returns ``True`` when it was just added."""
        existing = {str(row["name"]) for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
            return True
        return False

    def _ensure_scoped_pk(self, table: str, pk_cols: tuple[str, ...]) -> None:
        """Rebuild ``table`` under the composite ``pk_cols`` primary key if it is not already so.

        SQLite cannot ALTER a primary key, so a legacy table that keys on the object id alone is
        rebuilt: a new table with the scoped composite PK and the *same* columns is created, every
        existing row is copied across (scope columns already back-filled to the canonical scope by
        ``_ensure_column``), the old table is dropped, and the new one renamed into place. The copy
        is column-explicit and order-independent, so it is robust regardless of column ordering.

        Idempotent: a table whose PK already matches ``pk_cols`` is left untouched, so re-opening a
        migrated DB is a no-op."""
        info = list(self.conn.execute(f"PRAGMA table_info({table})"))
        current_pk = [str(row["name"]) for row in sorted(info, key=lambda r: r["pk"]) if row["pk"]]
        if tuple(current_pk) == tuple(pk_cols):
            return  # already scoped
        columns = [str(row["name"]) for row in info]
        # Recreate the column definitions verbatim (type/notnull/default), swapping the table-level
        # PK for the composite one. dflt_value comes straight from PRAGMA so 'default'/'v1' defaults
        # survive the rebuild.
        defs: list[str] = []
        for row in info:
            name = str(row["name"])
            col_type = str(row["type"]) or "TEXT"
            notnull = " NOT NULL" if row["notnull"] else ""
            default = f" DEFAULT {row['dflt_value']}" if row["dflt_value"] is not None else ""
            defs.append(f"{name} {col_type}{notnull}{default}")
        defs.append(f"PRIMARY KEY ({', '.join(pk_cols)})")
        tmp = f"{table}__scoped_rebuild"
        col_list = ", ".join(columns)
        self.conn.execute(f"DROP TABLE IF EXISTS {tmp}")  # noqa: S608 - derived from table arg
        self.conn.execute(f"CREATE TABLE {tmp} ({', '.join(defs)})")  # noqa: S608
        self.conn.execute(
            f"INSERT INTO {tmp} ({col_list}) SELECT {col_list} FROM {table}"  # noqa: S608
        )
        self.conn.execute(f"DROP TABLE {table}")  # noqa: S608
        self.conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")  # noqa: S608

    def _rebuild_fts_if_unscoped(self, table: str, columns_ddl: str) -> bool:
        """Drop+recreate an fts5 ``table`` under ``columns_ddl`` when it lacks the scope columns.

        FTS5 virtual tables cannot be ALTERed to add a column, and the FTS index is a *derived*
        structure (the authoritative rows live in content_index / reference_chunks), so a legacy
        table that has no ``world_id`` column is dropped and recreated under the scoped shape.
        Returns ``True`` when it rebuilt (so the caller repopulates), else ``False``.
        Idempotent: a table that already has the scope columns is left untouched."""
        info = list(self.conn.execute(f"PRAGMA table_info({table})"))
        if not info:
            return False  # absent; the executescript CREATE already made the scoped one
        cols = {str(row["name"]) for row in info}
        if "world_id" in cols and "version" in cols:
            return False  # already scoped
        self.conn.execute(f"DROP TABLE IF EXISTS {table}")  # noqa: S608 - whitelisted name
        self.conn.execute(
            f"CREATE VIRTUAL TABLE {table} USING fts5({columns_ddl})"  # noqa: S608
        )
        return True

    def save_audit_run(self, run: AuditRun) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO audit_runs (
                id, content_hash, started_at, rule_set_version, totals_json, baseline_delta_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run.id,
                run.content_hash,
                run.started_at.isoformat(),
                run.rule_set_version,
                _json(run.totals),
                _json(run.baseline_delta),
            ),
        )
        self.conn.commit()

    def get_audit_run(self, run_id: str) -> AuditRun | None:
        row = self.conn.execute("SELECT * FROM audit_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return AuditRun.model_validate(
            {
                "id": row["id"],
                "content_hash": row["content_hash"],
                "started_at": row["started_at"],
                "rule_set_version": row["rule_set_version"],
                "totals": json.loads(str(row["totals_json"])),
                "baseline_delta": json.loads(str(row["baseline_delta_json"])),
            }
        )

    def save_issue(self, issue: Issue) -> Issue:
        # Prefer the deterministic issue fingerprint as the row id. A fresh audit_run_id changes
        # every run; using it in the id would make the same still-open issue look new forever and
        # would break the audit -> suggest -> apply loop's stable handles.
        issue_id = (
            issue.id
            or issue.fingerprint
            or content_hash(
                issue.model_dump(mode="json", exclude_none=True, exclude={"audit_run_id"})
            )
        )
        saved = issue.model_copy(update={"id": issue_id})
        self.conn.execute(
            """
            INSERT OR REPLACE INTO issues (
                id, rule_code, severity, category, target_ref, message, evidence_json,
                fingerprint, audit_run_id, status,
                source_path, source_line, node_ref, suggested_action
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                saved.id,
                saved.rule_code,
                saved.severity.value,
                saved.category.value,
                saved.target_ref,
                saved.message,
                _json([item.model_dump(mode="json", exclude_none=True) for item in saved.evidence]),
                saved.fingerprint,
                saved.audit_run_id,
                saved.status.value,
                saved.source_path,
                saved.source_line,
                saved.node_ref,
                saved.suggested_action,
            ),
        )
        self.conn.commit()
        return saved

    def mark_resolved_issues(self, active_fingerprints: set[str]) -> None:
        """Mark previously-open issues as fixed when the latest audit no longer reports them."""
        if active_fingerprints:
            placeholders = ",".join("?" for _ in active_fingerprints)
            self.conn.execute(
                f"""
                UPDATE issues
                SET status = 'fixed'
                WHERE status = 'open'
                  AND (fingerprint IS NULL OR fingerprint NOT IN ({placeholders}))
                """,  # noqa: S608 - placeholders are generated, values are bound below
                sorted(active_fingerprints),
            )
        else:
            self.conn.execute("UPDATE issues SET status = 'fixed' WHERE status = 'open'")
        self.conn.commit()

    def _issues_where(
        self, *, severity: str | None, rule_code: str | None, status: str | None
    ) -> tuple[str, list[Any]]:
        """The shared filter clause + bind values for the issues table (one source for
        ``list_issues`` and ``count_issues`` so a paged listing and its total can never diverge)."""
        clauses: list[str] = []
        values: list[Any] = []
        if severity is not None:
            clauses.append("severity = ?")
            values.append(severity)
        if rule_code is not None:
            clauses.append("rule_code = ?")
            values.append(rule_code)
        if status is not None:
            clauses.append("status = ?")
            values.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return where, values

    def list_issues(
        self,
        *,
        severity: str | None = None,
        rule_code: str | None = None,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Issue]:
        """Persisted issues matching the filters, ordered by id. ``limit=None`` (default) returns
        every match — byte-for-byte identical SQL/result to before; a non-None ``limit`` pages with
        ``LIMIT/OFFSET`` for the token-bounded MCP surface."""
        where, values = self._issues_where(severity=severity, rule_code=rule_code, status=status)
        sql = f"SELECT * FROM issues{where} ORDER BY id"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            values = [*values, limit, offset]
        rows = self.conn.execute(sql, values).fetchall()
        return [_issue_from_row(row) for row in rows]

    def count_issues(
        self,
        *,
        severity: str | None = None,
        rule_code: str | None = None,
        status: str | None = None,
    ) -> int:
        """Total issues matching the same filters as :meth:`list_issues` (the un-paged total)."""
        where, values = self._issues_where(severity=severity, rule_code=rule_code, status=status)
        row = self.conn.execute(f"SELECT COUNT(*) FROM issues{where}", values).fetchone()
        return int(row[0])

    def save_patch(self, patch: dict[str, Any]) -> None:
        """Persist a patch proposal/decision. `patch` uses plain-dict keys:
        id, issue_id, status, ops, rationale, evidence, origin, applied_by, applied_at,
        rollback_ops. Models stay out of this layer to keep storage import-light."""
        self.conn.execute(
            """
            INSERT OR REPLACE INTO patches (
                id, issue_id, status, ops_json, rationale, evidence_json, origin,
                applied_by, applied_at, rollback_ops_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                patch["id"],
                patch.get("issue_id"),
                patch["status"],
                _json(patch.get("ops") or []),
                patch.get("rationale") or "",
                _json(patch.get("evidence") or []),
                patch.get("origin") or "ai_patch",
                patch.get("applied_by"),
                patch.get("applied_at"),
                _json(patch["rollback_ops"]) if patch.get("rollback_ops") is not None else None,
            ),
        )
        self.conn.commit()

    def get_patch(self, patch_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM patches WHERE id = ?", (patch_id,)).fetchone()
        return _patch_from_row(row) if row is not None else None

    def list_patches(
        self, *, status: str | None = None, issue_id: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            values.append(status)
        if issue_id is not None:
            clauses.append("issue_id = ?")
            values.append(issue_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.conn.execute(f"SELECT * FROM patches{where} ORDER BY id", values).fetchall()
        return [_patch_from_row(row) for row in rows]

    def update_patch(
        self,
        patch_id: str,
        *,
        status: str,
        applied_by: str | None = None,
        applied_at: str | None = None,
        rollback_ops: list[dict[str, Any]] | None = None,
        rolled_back_by: str | None = None,
        rolled_back_at: str | None = None,
    ) -> dict[str, Any]:
        if self.get_patch(patch_id) is None:
            raise KeyError(patch_id)
        self.conn.execute(
            """
            UPDATE patches SET status = ?,
                applied_by = COALESCE(?, applied_by),
                applied_at = COALESCE(?, applied_at),
                rollback_ops_json = COALESCE(?, rollback_ops_json),
                rolled_back_by = COALESCE(?, rolled_back_by),
                rolled_back_at = COALESCE(?, rolled_back_at)
            WHERE id = ?
            """,
            (
                status,
                applied_by,
                applied_at,
                _json(rollback_ops) if rollback_ops is not None else None,
                rolled_back_by,
                rolled_back_at,
                patch_id,
            ),
        )
        self.conn.commit()
        updated = self.get_patch(patch_id)
        assert updated is not None
        return updated

    def save_review_item(self, item: dict[str, Any]) -> None:
        """Persist a review-queue item: id, item_type, object_ref, payload, issue_refs, status."""
        self.conn.execute(
            """
            INSERT OR REPLACE INTO review_items (
                id, item_type, object_ref, payload_json, issue_refs_json, status,
                decided_by, decided_at, critic_verdict, critic_score, critic_primary_dim
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["id"],
                item["item_type"],
                item["object_ref"],
                _json(item.get("payload") or {}),
                _json(item.get("issue_refs") or []),
                item.get("status") or "pending_review",
                item.get("decided_by"),
                item.get("decided_at"),
                item.get("critic_verdict"),
                item.get("critic_score"),
                item.get("critic_primary_dim"),
            ),
        )
        self.conn.commit()

    def get_review_item(self, item_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM review_items WHERE id = ?", (item_id,)).fetchone()
        return _review_item_from_row(row) if row is not None else None

    def list_review_items(
        self, *, status: str | None = None, item_type: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            values.append(status)
        if item_type is not None:
            clauses.append("item_type = ?")
            values.append(item_type)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM review_items{where} ORDER BY created_at, id", values
        ).fetchall()
        return [_review_item_from_row(row) for row in rows]

    def update_review_item(
        self,
        item_id: str,
        *,
        status: str,
        decided_by: str | None = None,
        decided_at: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.get_review_item(item_id) is None:
            raise KeyError(item_id)
        # payload is updated by feedback-driven revision, which replaces the draft in place and
        # keeps the item pending so the reviewer sees the improved version.
        self.conn.execute(
            """
            UPDATE review_items SET status = ?,
                decided_by = COALESCE(?, decided_by),
                decided_at = COALESCE(?, decided_at),
                payload_json = COALESCE(?, payload_json)
            WHERE id = ?
            """,
            (status, decided_by, decided_at, None if payload is None else _json(payload), item_id),
        )
        self.conn.commit()
        updated = self.get_review_item(item_id)
        assert updated is not None
        return updated

    # --- lesson archive (IN-3) ----------------------------------------------------------------

    def save_lesson(
        self,
        item_type: str,
        lesson_text: str,
        *,
        dimension: str = "general",  # keyword-only; default "general" for compat
        exemplar_ids: list[str] | None = None,  # episodic exemplars (T18); None → NULL, no-op
        observed_false_pass_count: int | None = None,
    ) -> None:
        """Upsert a lesson for ``(item_type, dimension)``.

        Added dimension parameter. Callers that omit it get dimension='general',
        preserving exact backward-compatible behaviour.

        Legacy direct observations increment ``false_pass_count``.  A calibration extractor may
        instead pass its cumulative ``observed_false_pass_count`` snapshot; repeated extraction of
        the same report then replaces the count rather than counting that report twice.

        exemplar_ids (T18): a small list of rejected-draft object_refs to attach as episodic
        exemplars. None/empty writes NULL — byte-identical to a pre-T18 save. On conflict the
        column is REPLACED (mirrors lesson_text = excluded.lesson_text): each extract run
        regenerates from the full current report, so the latest concrete instances win.
        """
        import uuid

        now = datetime.now(UTC).isoformat()
        exemplars_json = json.dumps(exemplar_ids) if exemplar_ids else None
        stored_count = observed_false_pass_count if observed_false_pass_count is not None else 1
        self.conn.execute(
            """
            INSERT INTO lessons (id, item_type, dimension, lesson_text, false_pass_count,
                                 created_at, last_seen_at, exemplar_ids)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_type, dimension) DO UPDATE SET
                false_pass_count = CASE
                    WHEN ? THEN excluded.false_pass_count
                    ELSE lessons.false_pass_count + 1
                END,
                lesson_text = excluded.lesson_text,
                last_seen_at = excluded.last_seen_at,
                exemplar_ids = excluded.exemplar_ids
            """,
            (
                str(uuid.uuid4()),
                item_type,
                dimension,
                lesson_text,
                stored_count,
                now,
                now,
                exemplars_json,
                observed_false_pass_count is not None,
            ),
        )
        self.conn.commit()

    def get_lessons_for_type(
        self,
        item_type: str,
        *,
        dimension: str | None = None,  # None = all dimensions (backward compat)
        max_count: int = 3,
        cutoff_days: int = 90,
    ) -> list[dict[str, Any]]:
        """Return lessons for item_type, most-recent first.

        Added dimension parameter.
        dimension=None (default): return all dimensions — preserves backward-compatible behaviour.
        dimension=<str>: filter to that specific dimension only.

        Lessons newer than cutoff_days are sorted before older ones. Both groups are sorted by
        last_seen_at DESC within their tier. The hard limit is max_count rows returned.
        """
        from datetime import timedelta

        cutoff = (datetime.now(UTC) - timedelta(days=cutoff_days)).isoformat()
        # revoked_at IS NULL: a revoked (soft-deleted) lesson must never be injected again.
        if dimension is not None:
            rows = self.conn.execute(
                """
                SELECT * FROM lessons
                WHERE item_type = ? AND dimension = ? AND revoked_at IS NULL
                ORDER BY
                    CASE WHEN last_seen_at >= ? THEN 0 ELSE 1 END ASC,
                    last_seen_at DESC
                LIMIT ?
                """,
                (item_type, dimension, cutoff, max_count),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT * FROM lessons
                WHERE item_type = ? AND revoked_at IS NULL
                ORDER BY
                    CASE WHEN last_seen_at >= ? THEN 0 ELSE 1 END ASC,
                    last_seen_at DESC
                LIMIT ?
                """,
                (item_type, cutoff, max_count),
            ).fetchall()
        return [dict(row) for row in rows]

    def revoke_lesson(self, lesson_id: str, reason: str = "") -> bool:
        """Soft-delete a lesson so ``get_lessons_for_type`` stops returning it.

        A wrong or harmful lesson would otherwise be injected into generation/critique prompts
        forever. This tombstones it (records revoked_at + revoked_reason) rather than physically
        deleting the row, matching the store's tombstone convention elsewhere.

        Idempotent: revoking an already-revoked lesson keeps the ORIGINAL revoked_at/reason
        (via COALESCE) and still returns True. Returns False when no lesson has this id, so the
        caller can surface a guided error instead of silently succeeding.
        """
        row = self.conn.execute("SELECT id FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
        if row is None:
            return False
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """
            UPDATE lessons
            SET revoked_at = COALESCE(revoked_at, ?),
                revoked_reason = COALESCE(revoked_reason, ?)
            WHERE id = ?
            """,
            (now, reason or None, lesson_id),
        )
        self.conn.commit()
        return True

    def list_lessons(self, *, include_revoked: bool = False) -> list[dict[str, Any]]:
        """Return the lesson archive for governance/audit, newest first."""
        where = "" if include_revoked else " WHERE revoked_at IS NULL"
        rows = self.conn.execute(
            f"SELECT * FROM lessons{where} ORDER BY last_seen_at DESC, id ASC"
        ).fetchall()
        lessons: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            raw_exemplars = item.get("exemplar_ids")
            item["exemplar_ids"] = json.loads(raw_exemplars) if raw_exemplars else []
            lessons.append(item)
        return lessons

    # --- telemetry persistence (Item 8) -------------------------------------------------------

    def record_telemetry(self, records: list[CallRecord]) -> None:
        """Persist a batch of CallRecords into the telemetry table.

        Item 8: closes the gap between TelemetryCollector (in-memory) and the telemetry SQLite
        table (which has existed since the schema was created but was never written to). Callers
        (actions) invoke this at the end of each action; failures are non-fatal by design — the
        action already succeeded, and observability should never break the primary flow.

        Column mapping: CallRecord fields → table columns (all required by the NOT NULL schema).
        """
        if not records:
            return
        self.conn.executemany(
            """
            INSERT INTO telemetry (
                task_type, tier, input_tokens, output_tokens,
                cached_input_tokens, cache_hit, latency_ms, cost_usd,
                gateway_invocations, transport_attempt_cap, success, error_category
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r.task,
                    r.tier,
                    r.input_tokens,
                    r.output_tokens,
                    r.cached_input_tokens,
                    int(r.cache_hit),
                    r.latency_ms,
                    r.cost_usd,
                    r.gateway_invocations,
                    r.transport_attempt_cap,
                    int(r.success),
                    (
                        r.error_category
                        if not r.success and r.error_category in _TELEMETRY_ERROR_CATEGORIES
                        else ("provider_error" if not r.success else "")
                    ),
                )
                for r in records
            ],
        )
        self.conn.commit()

    def query_telemetry(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return the most-recent telemetry rows (newest first), for debugging / admin UI."""
        rows = self.conn.execute(
            """
            SELECT id, task_type, tier, input_tokens, output_tokens,
                   cached_input_tokens, cache_hit, latency_ms, cost_usd,
                   gateway_invocations, transport_attempt_cap, success, error_category, created_at
            FROM telemetry
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "task_type": str(row["task_type"]),
                "tier": str(row["tier"]),
                "input_tokens": int(row["input_tokens"]),
                "output_tokens": int(row["output_tokens"]),
                "cached_input_tokens": int(row["cached_input_tokens"]),
                "cache_hit": bool(row["cache_hit"]),
                "latency_ms": float(row["latency_ms"]),
                "cost_usd": float(row["cost_usd"]),
                "gateway_invocations": int(row["gateway_invocations"]),
                "transport_attempt_cap": int(row["transport_attempt_cap"]),
                "success": bool(row["success"]),
                "error_category": str(row["error_category"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    # --- agent run transcripts (T19: episodic / recall memory WRITE side) ---------------------
    # Persist each reasoning-and-tool loop run's full trajectory so no run is lost and it can be
    # listed/replayed by
    # id. Mirrors the audit_runs precedent (TEXT PK + a JSON transcript column) and the telemetry
    # precedent (newest-first query). The runtime table lives in the project's always-on
    # runtime.sqlite; this is the store experience-from-earlier-decision-cycles primitive
    # (execution-experience storage
    # episodic memory / historical-message retrieval). The retrieval half (reading a run back into
    # an
    # agent's working context) is deliberately not built here.

    def save_agent_run(
        self,
        *,
        run_id: str,
        goal: str,
        stop_reason: str,
        step_count: int,
        cost_usd: float,
        latency_ms: float,
        agent_name: str,
        transcript_json: str,
        created_at: str | None = None,
        audit_signal: int | None = None,
    ) -> None:
        """Persist one agent run. Idempotent on ``run_id`` (INSERT OR REPLACE: a re-save with the
        same id overwrites in place). ``transcript_json`` is the full serialized ``AgentResult``;
        the scalar columns are lifted out for cheap listing without loading the transcript.

        ``audit_signal`` (D3) is one more such lifted scalar: the deterministic-audit signal
        ``agent.episodic.run_audit_signal`` reads off the run's own steps, which the episodic
        recall/capacity layer uses as the run's importance. ``None`` means "not recorded" — the
        importance layer then falls back to its pre-D3 step-count proxy."""
        self.conn.execute(
            """
            INSERT OR REPLACE INTO agent_runs (
                id, goal, stop_reason, step_count, cost_usd, latency_ms,
                agent_name, transcript_json, created_at, audit_signal
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                goal,
                stop_reason,
                step_count,
                cost_usd,
                latency_ms,
                agent_name,
                transcript_json,
                created_at or _now_iso(),
                audit_signal,
            ),
        )
        self.conn.commit()

    def list_agent_runs(self, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """Recent agent runs, newest first (created_at DESC, rowid DESC tiebreak).

        Lightweight: the heavy ``transcript_json`` is intentionally NOT selected — call
        :meth:`get_agent_run` to load one run's full transcript. ``offset`` exists for the REST
        cursor adapter; the browser never receives or controls it directly."""
        if limit < 0 or offset < 0:
            raise ValueError("agent run list limit/offset must be non-negative")
        rows = self.conn.execute(
            """
            SELECT id, goal, stop_reason, step_count, cost_usd, latency_ms, agent_name, created_at
            FROM agent_runs
            ORDER BY created_at DESC, rowid DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "goal": str(row["goal"]),
                "stop_reason": str(row["stop_reason"]),
                "step_count": int(row["step_count"]),
                "cost_usd": float(row["cost_usd"]),
                "latency_ms": float(row["latency_ms"]),
                "agent_name": str(row["agent_name"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def agent_runs_snapshot(self) -> tuple[int, str]:
        """Return ``(total, etag)`` for stable cursor pagination of the run history.

        Agent runs are saved with ``INSERT OR REPLACE``.  SQLite gives every replacement a new
        rowid, so count/max/sum(rowid) changes on every supported insert, replacement or delete.
        Hashing those aggregates is constant-memory and avoids loading an unbounded history just
        to validate a cursor.
        """
        row = self.conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                COALESCE(MAX(rowid), 0) AS max_rowid,
                COALESCE(SUM(rowid), 0) AS rowid_sum
            FROM agent_runs
            """
        ).fetchone()
        total = int(row["total"])
        marker = f"{total}:{int(row['max_rowid'])}:{int(row['rowid_sum'])}"
        etag = hashlib.sha256(marker.encode("utf-8")).hexdigest()[:24]
        return total, etag

    def get_agent_run(self, run_id: str) -> dict[str, Any] | None:
        """One run by id with its full transcript (``transcript`` = parsed JSON), or ``None``."""
        row = self.conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "goal": str(row["goal"]),
            "stop_reason": str(row["stop_reason"]),
            "step_count": int(row["step_count"]),
            "cost_usd": float(row["cost_usd"]),
            "latency_ms": float(row["latency_ms"]),
            "agent_name": str(row["agent_name"]),
            "created_at": str(row["created_at"]),
            "transcript": json.loads(str(row["transcript_json"])),
        }

    # --- episodic recall (historical-message retrieval)
    # The READ half of episodic memory: a per-run embedding of (goal + outcome) so a later run can
    # retrieve semantically-similar past runs and inject them as prompt context
    # (historical-message recall recall
    # storage, . The embedding lives beside the full transcript (agent_runs);
    # recall joins the two so only a *finished* run (stop_reason == 'finished') is ever surfaced.

    def save_agent_run_embedding(
        self,
        *,
        run_id: str,
        model_id: str,
        goal: str,
        summary: str,
        vector: bytes,
        created_at: str | None = None,
    ) -> None:
        """Persist the recall embedding for one run. Idempotent on ``run_id`` (INSERT OR REPLACE).

        ``model_id`` records the embedding space the vector was produced in — recall only ever
        compares vectors sharing the current embedder's id, since two embedders' vectors are not
        comparable. ``vector`` is a float32 blob (see ``agent.episodic._to_blob``). Stamped with
        the store's current ``world_id`` as the D2 memory-namespace ``scope`` (a re-save via
        REPLACE also resets ``last_accessed_at``/``deleted_at`` to NULL, matching the pre-D1/D2
        REPLACE semantics for every other unlisted column)."""
        self.conn.execute(
            """
            INSERT OR REPLACE INTO agent_run_embeddings (
                run_id, model_id, goal, summary, vector, created_at, scope
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, model_id, goal, summary, vector, created_at or _now_iso(), self.world_id),
        )
        self.conn.commit()

    def query_agent_run_embeddings(self, *, model_id: str) -> list[dict[str, Any]]:
        """Recall candidates: every FINISHED, live run embedding in the ``model_id`` space AND the
        store's current ``world_id`` scope (D2 — a project/world never recalls another's memories).

        Joins ``agent_runs`` so only runs whose ``stop_reason == 'finished'`` are returned (a
        run that ran out of step budget carries no trustworthy conclusion to recall) and excludes
        soft-deleted rows (``deleted_at IS NULL``) — an over-capacity eviction stops surfacing them.
        ``audit_signal`` (D3, nullable) is lifted from the run row so the retrieval layer can weigh
        an episode's importance, with ``step_count`` alongside it as the pre-D3 fallback for a run
        that recorded no signal; ``last_accessed_at`` (nullable) feeds D1 recency; the vector blob
        is returned raw and the three-factor ranking happens in ``agent.episodic``."""
        rows = self.conn.execute(
            """
            SELECT e.run_id, e.goal, e.summary, e.vector, e.created_at, e.last_accessed_at,
                   r.step_count, r.audit_signal
            FROM agent_run_embeddings AS e
            JOIN agent_runs AS r ON r.id = e.run_id
            WHERE e.model_id = ? AND e.scope = ?
              AND r.stop_reason = 'finished' AND e.deleted_at IS NULL
            """,
            (model_id, self.world_id),
        ).fetchall()
        return [
            {
                "run_id": str(row["run_id"]),
                "goal": str(row["goal"]),
                "summary": str(row["summary"]),
                "vector": bytes(row["vector"]),
                "created_at": str(row["created_at"]),
                "last_accessed_at": (
                    str(row["last_accessed_at"]) if row["last_accessed_at"] is not None else None
                ),
                "step_count": int(row["step_count"]),
                "audit_signal": (
                    int(row["audit_signal"]) if row["audit_signal"] is not None else None
                ),
            }
            for row in rows
        ]

    # --- feedback-memory cross-trial buffer
    # Verbal reflections distilled from failed trials, persisted so a later run at a similar goal
    # can prepend them to the actor's reflection slot (stored correction buffer).
    # Each row carries a goal embedding so recall is semantic, not exact-match.

    def save_reflection(
        self,
        *,
        reflection_id: str,
        goal: str,
        reflection: str,
        source: str,
        vector: bytes | None = None,
        model_id: str | None = None,
        created_at: str | None = None,
    ) -> None:
        """Persist one distilled reflection. Idempotent on ``reflection_id``.

        ``vector`` is the goal embedding (float32 blob) and ``model_id`` its embedding space; both
        are nullable so a reflection can still be recorded when the embedder is unavailable (it is
        then simply not recallable — recall filters ``vector IS NOT NULL``). Stamped with the
        store's current ``world_id`` as the D2 memory-namespace ``scope``."""
        self.conn.execute(
            """
            INSERT OR REPLACE INTO reflections (
                id, goal, reflection, source, created_at, vector, model_id, scope
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reflection_id,
                goal,
                reflection,
                source,
                created_at or _now_iso(),
                vector,
                model_id,
                self.world_id,
            ),
        )
        self.conn.commit()

    def query_reflections(self, *, model_id: str) -> list[dict[str, Any]]:
        """Recall candidates: every embedded reflection in the ``model_id`` embedding space AND the
        store's current ``world_id`` scope (D2 — a project/world never recalls another's memories).

        Rows without a vector (embedder was unavailable at write time) are excluded — they cannot
        be ranked — as are soft-deleted rows (``deleted_at IS NULL``), so an over-capacity eviction
        stops surfacing them. ``last_accessed_at`` (nullable) feeds D1 recency. Ranking happens in
        the retrieval layer (``agent.episodic``)."""
        rows = self.conn.execute(
            """
            SELECT id, goal, reflection, source, created_at, last_accessed_at, vector
            FROM reflections
            WHERE model_id = ? AND scope = ? AND vector IS NOT NULL AND deleted_at IS NULL
            """,
            (model_id, self.world_id),
        ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "goal": str(row["goal"]),
                "reflection": str(row["reflection"]),
                "source": str(row["source"]),
                "created_at": str(row["created_at"]),
                "last_accessed_at": (
                    str(row["last_accessed_at"]) if row["last_accessed_at"] is not None else None
                ),
                "vector": bytes(row["vector"]),
            }
            for row in rows
        ]

    # --- episodic capacity lifecycle (T7: soft-delete retention) -------------------------------
    # The query-independent retention inputs (when a memory was written + how substantive it was)
    # that ``agent.episodic`` scores to decide which over-capacity memories to soft-delete. Kept as
    # dumb reads here; the recency/importance policy lives in the memory-policy module, not storage.

    def list_run_embedding_retention(self, *, model_id: str) -> list[dict[str, Any]]:
        """Live (non-tombstoned) run embeddings in ``model_id`` + the store's scope, with their
        retention inputs (D2 — capacity is enforced per project/world, not globally across a shared
        ``runtime.sqlite``).

        Returns every un-deleted embedding — not only finished runs — because capacity bounds the
        *stored volume* of the space. ``audit_signal`` (D3, nullable) feeds the importance factor
        with ``step_count`` (LEFT-joined, COALESCE 0 if the run row is somehow absent) as its
        pre-D3 fallback; ``last_accessed_at``/``created_at`` feed D1 recency (see
        ``agent.episodic._recency_basis``)."""
        rows = self.conn.execute(
            """
            SELECT e.run_id, e.created_at, e.last_accessed_at,
                   COALESCE(r.step_count, 0) AS step_count, r.audit_signal
            FROM agent_run_embeddings AS e
            LEFT JOIN agent_runs AS r ON r.id = e.run_id
            WHERE e.model_id = ? AND e.scope = ? AND e.deleted_at IS NULL
            """,
            (model_id, self.world_id),
        ).fetchall()
        return [
            {
                "run_id": str(row["run_id"]),
                "created_at": str(row["created_at"]),
                "last_accessed_at": (
                    str(row["last_accessed_at"]) if row["last_accessed_at"] is not None else None
                ),
                "step_count": int(row["step_count"]),
                "audit_signal": (
                    int(row["audit_signal"]) if row["audit_signal"] is not None else None
                ),
            }
            for row in rows
        ]

    def list_reflection_retention(self, *, model_id: str) -> list[dict[str, Any]]:
        """Live (non-tombstoned) reflection ids + retention inputs in ``model_id`` + the store's
        scope (D2 — same per-project/world capacity boundary as run embeddings)."""
        rows = self.conn.execute(
            """
            SELECT id, created_at, last_accessed_at
            FROM reflections
            WHERE model_id = ? AND scope = ? AND vector IS NOT NULL AND deleted_at IS NULL
            """,
            (model_id, self.world_id),
        ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "created_at": str(row["created_at"]),
                "last_accessed_at": (
                    str(row["last_accessed_at"]) if row["last_accessed_at"] is not None else None
                ),
            }
            for row in rows
        ]

    def soft_delete_agent_run_embeddings(
        self, run_ids: list[str], *, at: str | None = None
    ) -> None:
        """Tombstone the given run embeddings (over-capacity eviction). No-op on an empty list.

        Records ``deleted_at`` rather than deleting the row, so recall stops surfacing it but
        ``restore_agent_run_embedding`` can revive a mistaken eviction. Only stamps rows that are
        still live (``deleted_at IS NULL``), so a re-run keeps each row's original eviction time."""
        if not run_ids:
            return
        placeholders = ",".join("?" for _ in run_ids)
        self.conn.execute(
            f"UPDATE agent_run_embeddings SET deleted_at = ? "  # noqa: S608 - generated placeholders
            f"WHERE deleted_at IS NULL AND run_id IN ({placeholders})",
            (at or _now_iso(), *run_ids),
        )
        self.conn.commit()

    def soft_delete_reflections(self, ids: list[str], *, at: str | None = None) -> None:
        """Tombstone the given reflections (over-capacity eviction). No-op on an empty list."""
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self.conn.execute(
            f"UPDATE reflections SET deleted_at = ? "  # noqa: S608 - generated placeholders
            f"WHERE deleted_at IS NULL AND id IN ({placeholders})",
            (at or _now_iso(), *ids),
        )
        self.conn.commit()

    def restore_agent_run_embedding(self, run_id: str) -> bool:
        """Clear a run embedding's tombstone so recall can surface it again (recovery path).

        Returns ``True`` when a row with this id exists (tombstoned or not), ``False`` when none
        does — so a caller can surface a guided error instead of silently succeeding."""
        row = self.conn.execute(
            "SELECT run_id FROM agent_run_embeddings WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return False
        self.conn.execute(
            "UPDATE agent_run_embeddings SET deleted_at = NULL WHERE run_id = ?", (run_id,)
        )
        self.conn.commit()
        return True

    def restore_reflection(self, reflection_id: str) -> bool:
        """Clear a reflection's tombstone so recall can surface it again (recovery path)."""
        row = self.conn.execute(
            "SELECT id FROM reflections WHERE id = ?", (reflection_id,)
        ).fetchone()
        if row is None:
            return False
        self.conn.execute("UPDATE reflections SET deleted_at = NULL WHERE id = ?", (reflection_id,))
        self.conn.commit()
        return True

    # --- D1: recency-by-last-access write-back -------------------------------------------------
    # Called by ``agent.episodic`` right after a recall actually surfaces a memory, so its recency
    # clock resets to "now" instead of staying pinned to its write time (three-factor retrieval
    # recency =
    # decay since last *access*). ``run_id``/reflection ``id`` are each a table-wide primary key, so
    # no scope filter is needed here — the caller only ever passes ids it just read back from its
    # own (already scope-filtered) query.

    def touch_agent_run_embeddings(self, run_ids: list[str], *, at: str | None = None) -> None:
        """Stamp ``last_accessed_at`` on the given run embeddings. No-op on an empty list."""
        if not run_ids:
            return
        placeholders = ",".join("?" for _ in run_ids)
        self.conn.execute(
            f"UPDATE agent_run_embeddings SET last_accessed_at = ? "  # noqa: S608 - own placeholders
            f"WHERE run_id IN ({placeholders})",
            (at or _now_iso(), *run_ids),
        )
        self.conn.commit()

    def touch_reflections(self, ids: list[str], *, at: str | None = None) -> None:
        """Stamp ``last_accessed_at`` on the given reflections. No-op on an empty list."""
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self.conn.execute(
            f"UPDATE reflections SET last_accessed_at = ? "  # noqa: S608 - own placeholders
            f"WHERE id IN ({placeholders})",
            (at or _now_iso(), *ids),
        )
        self.conn.commit()

    # ------------------------------------------------------------------------------------------

    def replace_content_index(self, bundle: ContentBundle) -> None:
        """Sync content_index + content_fts to ``bundle`` incrementally, within the current scope.

        The bundle is the source of truth; this diffs the desired rows against the persisted ones
        *of this (world_id, version) scope* (keyed by ``ref``, compared on a content ``row_hash``)
        and only touches what changed: changed/new rows are upserted, vanished rows are deleted, and
        unchanged rows are left alone. Every existing-row read and DELETE is scope-filtered, so
        writing one scope can never read, update or delete another scope's rows (INV-2). content_fts
        is a plain fts5 table carrying the scope as UNINDEXED columns, so its rows are deleted by
        ``(ref, scope)`` and re-inserted for exactly the changed/new/removed refs.

        For a single-scope (default) project the end state matches the old drop-and-reinsert:
        same content_index rows and the same content_fts rows. Runs in a single transaction."""
        rows = list(_content_rows(bundle))
        desired: dict[str, tuple[str, str, str, str, str]] = {}
        desired_hash: dict[str, str] = {}
        for ref, object_type, object_id, title, body in rows:
            desired[ref] = (ref, object_type, object_id, title, body)
            desired_hash[ref] = _content_row_hash(title, body)

        existing_hash = {
            str(row["ref"]): str(row["row_hash"])
            for row in self.conn.execute(
                "SELECT ref, row_hash FROM content_index WHERE world_id = ? AND version = ?",
                (self.world_id, self.version),
            )
        }

        removed = [ref for ref in existing_hash if ref not in desired]
        # A legacy/back-filled row carries row_hash='' (never a real sha1), so it always counts as
        # changed and gets re-stamped on the first incremental sync -- correctness over the mtime
        # fast path.
        changed = [ref for ref, h in desired_hash.items() if existing_hash.get(ref) != h]

        try:
            for ref in removed:
                self.conn.execute(
                    "DELETE FROM content_index WHERE ref = ? AND world_id = ? AND version = ?",
                    (ref, self.world_id, self.version),
                )
                self.conn.execute(
                    "DELETE FROM content_fts WHERE ref = ? AND world_id = ? AND version = ?",
                    (ref, self.world_id, self.version),
                )
            for ref in changed:
                _r, object_type, object_id, title, body = desired[ref]
                self.conn.execute(
                    """
                    INSERT INTO content_index (
                        world_id, version, ref, object_type, object_id, title, body, row_hash
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(world_id, version, ref) DO UPDATE SET
                        object_type = excluded.object_type,
                        object_id = excluded.object_id,
                        title = excluded.title,
                        body = excluded.body,
                        row_hash = excluded.row_hash
                    """,
                    (
                        self.world_id,
                        self.version,
                        ref,
                        object_type,
                        object_id,
                        title,
                        body,
                        desired_hash[ref],
                    ),
                )
                # Plain fts5 has no upsert; delete this (ref, scope) row then re-insert.
                self.conn.execute(
                    "DELETE FROM content_fts WHERE ref = ? AND world_id = ? AND version = ?",
                    (ref, self.world_id, self.version),
                )
                self.conn.execute(
                    "INSERT INTO content_fts (ref, object_type, world_id, version, title, body) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        ref,
                        object_type,
                        self.world_id,
                        self.version,
                        cjk_fts_text(title),
                        cjk_fts_text(body),
                    ),
                )
        except Exception:
            self.conn.rollback()
            raise
        self.conn.commit()

    def get_vectors(
        self, model_id: str, *, table: str = "content_vectors"
    ) -> dict[str, tuple[str, int, bytes]]:
        """Persisted embeddings for ``model_id`` as ``{ref: (text_hash, dim, vector_blob)}``.

        Shared by the content-graph and inspiration-reference vector retrievers (``table``). The
        text_hash lets a retriever embed only rows whose text (or the model) changed, so
        re-opening a project never re-runs the embedder over unchanged rows.

        Scoped to the store's current (world_id, version) — a retriever only ever
        caches/embeds within its own scope. In the single default scope this returns exactly the
        same rows as before."""
        table = _vectors_table(table)
        rows = self.conn.execute(
            f"SELECT ref, text_hash, dim, vector FROM {table} "  # noqa: S608
            f"WHERE model_id = ? AND world_id = ? AND version = ?",
            (model_id, self.world_id, self.version),
        ).fetchall()
        return {
            str(row["ref"]): (str(row["text_hash"]), int(row["dim"]), bytes(row["vector"]))
            for row in rows
        }

    def upsert_vectors(
        self,
        model_id: str,
        rows: list[tuple[str, str, int, bytes]],
        *,
        table: str = "content_vectors",
    ) -> None:
        """Insert/replace ``(ref, text_hash, dim, vector_blob)`` rows for ``model_id``."""
        if not rows:
            return
        table = _vectors_table(table)
        self.conn.executemany(
            f"""
            INSERT INTO {table} (ref, model_id, text_hash, dim, vector, world_id, version)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(world_id, version, ref, model_id) DO UPDATE SET
                text_hash = excluded.text_hash, dim = excluded.dim, vector = excluded.vector
            """,  # noqa: S608
            [
                (ref, model_id, text_hash, dim, blob, self.world_id, self.version)
                for ref, text_hash, dim, blob in rows
            ],
        )
        self.conn.commit()

    def make_vector_backend(
        self,
        model_id: str,
        *,
        dim: int,
        table: str = "content_vectors",
        quantized: bool = False,
        ann: bool = False,
    ) -> VectorSearchBackend | None:
        """Build the disk-resident vec0 backend for ``table``, or ``None`` to fall back to numpy.

        With ``quantized=False`` (default) this builds the fp32 ``SqliteVecBackend`` (lossless,
        bit-identical to numpy). With ``quantized=True`` it builds the int8 two-stage
        ``SqliteVecInt8Backend``: a ~4× smaller int8 coarse index plus an fp32 rerank sidecar,
        recall ~0.999. The two use distinct vec0 table names so both can coexist in one DB.

        **Tier selection.** With ``ann=True`` *and* a corpus already at or above
        ``USEARCH_MIN_N`` persisted vectors, this builds the on-disk usearch HNSW
        ``UsearchBackend`` instead — a sub-linear ANN index for the large-N case, two-stage
        fp32-reranked to ~0.99 recall. The threshold is the safety valve: a small / eval corpus
        (well under the threshold) always stays on the exact sqlite-vec scan, so the acceptance
        recall gate (hit_rate 1.0) never sees ANN approximation. ``ann`` defaults to ``False``, so
        every existing caller keeps the exact backend; the ANN tier is strictly opt-in. If usearch
        is unavailable the build falls through to the sqlite-vec backend with a guided log line.

        Returns ``None`` (with a guided log line, never a crash) when sqlite-vec is unavailable or
        its extension cannot load on this connection -- the retriever then uses the numpy backend so
        environments without the extension stay functional.

        The vec0 index is created on this store's own connection (one file, FTS5 + vectors together)
        and, on first use, **backfilled once** from the existing ``content_vectors`` blob cache so a
        project that already has persisted fp32 vectors does not need to re-embed to populate the
        index (the int8 backend quantises each backfilled fp32 vector on upsert)."""
        from ..retrieval.vector_backend import (
            SqliteVecBackend,
            SqliteVecError,
            SqliteVecInt8Backend,
        )

        _vectors_table(table)  # validate the blob table name

        # Tier selection: only an explicit opt-in AND a large-enough corpus switch to the ANN
        # backend. The N check is what keeps small / eval corpora on the exact scan; it reads the
        # persisted blob-cache count (cheap COUNT, no vectors loaded).
        if ann and self._corpus_size(model_id, table=table) >= USEARCH_MIN_N:
            usearch_backend = self._make_usearch_backend(model_id, dim=int(dim), table=table)
            if usearch_backend is not None:
                return usearch_backend
            # usearch unavailable / failed -> fall through to the exact sqlite-vec backend below.

        backend: VectorSearchBackend
        try:
            if quantized:
                backend = SqliteVecInt8Backend(
                    self.conn,
                    dim=int(dim),
                    table=_vec0_int8_table(table),
                    world_id=self.world_id,
                    version=self.version,
                )
            else:
                backend = SqliteVecBackend(
                    self.conn,
                    dim=int(dim),
                    table=_vec0_table(table),
                    world_id=self.world_id,
                    version=self.version,
                )
            # Backfill is inside the guard: probing/inserting against a vec0 table that was
            # persisted at a different dimensionality raises sqlite3.OperationalError, which must
            # degrade to the numpy backend with a guided log line -- never a bare crash.
            self._backfill_vec0(model_id, backend, dim=int(dim), table=table)
        except (SqliteVecError, sqlite3.OperationalError) as exc:
            logger.info(
                "sqlite-vec unavailable for %s (%s); using the in-memory numpy vector backend.",
                table,
                exc,
            )
            return None
        return backend

    def _corpus_size(self, model_id: str, *, table: str) -> int:
        """Count of persisted vectors for ``model_id`` in the blob cache ``table`` (the tier knob).

        This is the N the tier selector thresholds on. It reads the authoritative blob cache (which
        is always populated before a search backend is built), so it is correct even on the very
        first open before any vec0/usearch index exists. Scoped to the current (world_id, version):
        the tier decision is per-scope, since each scope gets its own search index."""
        table = _vectors_table(table)
        row = self.conn.execute(
            f"SELECT COUNT(*) FROM {table} "  # noqa: S608 - validated name
            f"WHERE model_id = ? AND world_id = ? AND version = ?",
            (model_id, self.world_id, self.version),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def _make_usearch_backend(
        self, model_id: str, *, dim: int, table: str
    ) -> VectorSearchBackend | None:
        """Build the on-disk ``UsearchBackend``, backfilled from the blob cache, or ``None``.

        Returns ``None`` (guided log, no crash) when usearch is unavailable or the index cannot be
        built — the caller then falls back to the exact sqlite-vec backend. The ``.usearch`` file
        lives next to the runtime DB (or a temp file for an in-memory DB); the fp32 authority +
        keymap tables live in this connection, so a fresh index is backfilled once from the blob
        cache exactly like the vec0 backends."""
        from ..retrieval.vector_backend import UsearchBackend, UsearchError

        try:
            backend = UsearchBackend(
                self.conn,
                dim=int(dim),
                table=table,
                index_path=self._usearch_index_path(table),
                world_id=self.world_id,
                version=self.version,
            )
            self._backfill_usearch(model_id, backend, dim=int(dim), table=table)
        except (UsearchError, sqlite3.OperationalError) as exc:
            logger.info(
                "usearch unavailable for %s (%s); falling back to the sqlite-vec backend.",
                table,
                exc,
            )
            return None
        return backend

    def _usearch_index_path(self, table: str) -> str:
        """Path of the ``.usearch`` file for ``table`` within the current scope.

        Persistent runtime DBs get a sibling ``{db}.{table}.usearch`` so the ANN index survives
        re-opens alongside the DB. An in-memory DB has no on-disk home, so the index goes to a
        deterministic temp path keyed by the DB id — it is rebuildable from the (in-memory) fp32
        table anyway, so a transient temp file is fine.

        Each scope gets its own per-scope HNSW file (the index is not partitioned
        the way vec0 is). The canonical default scope keeps the exact pre-scope filename so an
        existing single-world ``.usearch`` index opens in place; only non-default scopes add a
        ``.{world_id}.{version}`` suffix."""
        table = _vectors_table(table)
        suffix = ""
        if (self.world_id, self.version) != (DEFAULT_WORLD_ID, DEFAULT_VERSION):
            safe = f"{self.world_id}.{self.version}".replace("/", "_").replace("\\", "_")
            suffix = f".{safe}"
        if self.path and self.path != ":memory:" and "mode=memory" not in self.path:
            return f"{self.path}.{table}{suffix}.usearch"
        import tempfile

        return str(Path(tempfile.gettempdir()) / f"owcopilot_{id(self)}_{table}{suffix}.usearch")

    def _backfill_usearch(
        self,
        model_id: str,
        backend: VectorSearchBackend,
        *,
        dim: int,
        table: str,
    ) -> None:
        """One-time populate of an empty usearch index from the fp32 blob cache for ``model_id``.

        Mirrors ``_backfill_vec0``: only runs when the index is empty (a freshly built / rebuilt
        backend whose fp32 authority table has no rows yet), so it never re-stamps an index the
        incremental sync already owns."""
        if backend.search(np.zeros(dim, dtype=np.float32), limit=1):
            return  # already populated; incremental sync owns it from here
        for ref, (_text_hash, stored_dim, blob) in self.get_vectors(model_id, table=table).items():
            if stored_dim != dim:
                continue  # a stale row from a different model dimensionality; skip, will re-embed
            backend.upsert(ref, np.frombuffer(blob, dtype=np.float32))

    def _backfill_vec0(
        self,
        model_id: str,
        backend: SqliteVecBackend | SqliteVecInt8Backend,
        *,
        dim: int,
        table: str,
    ) -> None:
        """One-time populate of an empty vec0 table from the fp32 blob cache for ``model_id``.

        Only runs when the vec0 table is empty (fresh / just-created): the retriever's incremental
        upsert/delete keeps it in step afterwards, so this never re-stamps an already-populated
        index."""
        if backend.search(np.zeros(dim, dtype=np.float32), limit=1):
            return  # already populated; incremental sync owns it from here
        for ref, (_text_hash, stored_dim, blob) in self.get_vectors(model_id, table=table).items():
            if stored_dim != dim:
                continue  # a stale row from a different model dimensionality; skip, will re-embed
            backend.upsert(ref, np.frombuffer(blob, dtype=np.float32))

    def prune_vectors(
        self, model_id: str, keep_refs: set[str], *, table: str = "content_vectors"
    ) -> None:
        """Drop cached vectors for refs no longer present, keeping the table in step.

        Scoped to the current (world_id, version): a reindex of one scope only prunes that scope's
        cached vectors, never another scope's. Single-world default behaviour is unchanged."""
        table = _vectors_table(table)
        stale = [
            str(row["ref"])
            for row in self.conn.execute(
                f"SELECT ref FROM {table} "  # noqa: S608
                f"WHERE model_id = ? AND world_id = ? AND version = ?",
                (model_id, self.world_id, self.version),
            ).fetchall()
            if str(row["ref"]) not in keep_refs
        ]
        if stale:
            self.conn.executemany(
                f"DELETE FROM {table} "  # noqa: S608
                f"WHERE model_id = ? AND ref = ? AND world_id = ? AND version = ?",
                [(model_id, ref, self.world_id, self.version) for ref in stale],
            )
            self.conn.commit()

    def relation_rows_for_entities(self, entity_ids: set[str]) -> list[sqlite3.Row]:
        """Relation index rows whose source or target is one of ``entity_ids``.

        Lets retrieval complete the relations of entities it already found, so a
        relationship/structure question retrieves the relevant relations even when its phrasing
        never matched the relation text directly -- the difference between answering and a
        false refusal."""
        if not entity_ids:
            return []
        # Scope-aware read. This is a full scan of the scope's relation rows, so
        # the (world_id, version) filter both keeps relation completion within the current scope and
        # is a concrete "降N" win — the scan now touches only this scope's relations, not the whole
        # multi-world table. The composite scope index serves the predicate.
        rows = self.conn.execute(
            "SELECT ref, object_type, title, body FROM content_index "
            "WHERE object_type = 'relation' AND world_id = ? AND version = ?",
            (self.world_id, self.version),
        ).fetchall()
        matched: list[sqlite3.Row] = []
        for row in rows:
            tokens = str(row["title"]).split()  # "source kind... target": ids are slug tokens
            if tokens and (tokens[0] in entity_ids or tokens[-1] in entity_ids):
                matched.append(row)
        return matched

    def reference_chunks_by_refs(
        self, refs: list[str], *, use: str | None = None
    ) -> dict[str, sqlite3.Row]:
        """Fetch full reference-chunk rows (with source title) for the given refs.

        Lets the hybrid reference retriever rank by ref, then materialise display rows with
        correct source metadata regardless of which leg (BM25 or vector) surfaced each ref.

        ``use`` restricts the result to sources whose ``allowed_uses`` permit that use. This is
        also what keeps the semantic leg honest: a vector hit never touches the BM25 query, so
        materialisation is the one place every candidate ref must pass through."""
        if not refs:
            return {}
        placeholders = ",".join("?" for _ in refs)
        use_clause, use_params = _allowed_use_filter(use)
        # Scope-aware read. Materialise only this scope's chunks (and join the
        # source within the same scope, so a same-id source in another scope cannot supply the
        # title). In the single default scope this is the same rows as before.
        rows = self.conn.execute(
            f"""
            SELECT c.ref, c.source_id, s.title AS source_title,
                   s.provenance AS source_provenance, c.title, c.body,
                   c.chunk_index, c.metadata_json
            FROM reference_chunks AS c
            LEFT JOIN reference_sources AS s
                ON s.id = c.source_id AND s.world_id = c.world_id AND s.version = c.version
            WHERE c.ref IN ({placeholders}) AND c.world_id = ? AND c.version = ?{use_clause}
            """,  # noqa: S608
            (*refs, self.world_id, self.version, *use_params),
        ).fetchall()
        return {str(row["ref"]): row for row in rows}

    # --- GraphRAG community reports (the macro-overview index) -----------------------------------

    def save_community_report(self, report: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO community_reports (
                id, level, title, summary, member_refs_json, fingerprint, created_at,
                parent_id, hier_level
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(report["id"]),
                str(report["level"]),
                str(report["title"]),
                str(report["summary"]),
                _json(list(report.get("member_refs", []))),
                str(report["fingerprint"]),
                _now_iso(),
                None if report.get("parent_id") is None else str(report["parent_id"]),
                int(report.get("hier_level", 0)),
            ),
        )
        self.conn.commit()

    def get_community_report(self, report_id: str, fingerprint: str) -> dict[str, Any] | None:
        """Return the cached report only when its fingerprint still matches — a changed community
        (members or their text) yields a new fingerprint, so the stale row is ignored and the
        caller regenerates just that one."""
        row = self.conn.execute(
            "SELECT * FROM community_reports WHERE id = ? AND fingerprint = ?",
            (report_id, fingerprint),
        ).fetchone()
        return _community_report_from_row(row) if row else None

    def list_community_reports(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM community_reports ORDER BY (level = 'global') DESC, id"
        ).fetchall()
        return [_community_report_from_row(row) for row in rows]

    def prune_community_reports(self, keep_ids: list[str]) -> None:
        """Drop reports for communities that no longer exist (e.g. after entities were removed)."""
        existing = {str(row["id"]) for row in self.conn.execute("SELECT id FROM community_reports")}
        for stale in sorted(existing - set(keep_ids)):
            self.conn.execute("DELETE FROM community_reports WHERE id = ?", (stale,))
        self.conn.commit()

    def replace_graph_edges(self, graph: ContentGraph) -> None:
        """Sync graph_edges to ``graph`` incrementally via a deterministic per-edge fingerprint.

        graph_edges has no natural key (its ``id`` is AUTOINCREMENT) and a MultiDiGraph can hold
        byte-identical parallel edges, so the fingerprint is
        ``sha1(source|target|kind|edge_type|valid_from|valid_until|#occurrence)`` -- the occurrence
        ordinal disambiguates true duplicates so each maps to its own stable row. Edges whose
        fingerprint already exists are skipped, new fingerprints are inserted, and fingerprints no
        longer present are deleted. The resulting row *set* is identical to the old
        drop-and-reinsert (same edges, same multiplicity); only the AUTOINCREMENT ``id`` values may
        differ, and nothing reads that column. Runs in a single transaction.

        The existing-fingerprint read and the removed DELETE are scope-filtered to
        the current (world_id, version), and inserts stamp it — so a write to one scope never
        deletes another scope's edges (INV-2). The fingerprint uniqueness is scope-qualified."""
        desired: dict[str, tuple[str, str, str, str, int | None, int | None]] = {}
        seen: dict[tuple[str, str, str, str, int | None, int | None], int] = {}
        for edge in graph.edge_refs():
            key = (
                edge.source,
                edge.target,
                edge.kind,
                edge.edge_type,
                edge.valid_from,
                edge.valid_until,
            )
            occurrence = seen.get(key, 0)
            seen[key] = occurrence + 1
            fingerprint = _edge_fingerprint(edge, occurrence)
            desired[fingerprint] = key

        existing = {
            str(row["edge_fingerprint"])
            for row in self.conn.execute(
                "SELECT edge_fingerprint FROM graph_edges WHERE world_id = ? AND version = ?",
                (self.world_id, self.version),
            )
        }
        removed = existing - desired.keys()
        added = desired.keys() - existing

        try:
            for fingerprint in sorted(removed):
                self.conn.execute(
                    "DELETE FROM graph_edges "
                    "WHERE edge_fingerprint = ? AND world_id = ? AND version = ?",
                    (fingerprint, self.world_id, self.version),
                )
            for fingerprint in sorted(added):
                source, target, kind, edge_type, valid_from, valid_until = desired[fingerprint]
                self.conn.execute(
                    """
                    INSERT INTO graph_edges (
                        source, target, kind, edge_type, valid_from, valid_until, edge_fingerprint,
                        world_id, version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source,
                        target,
                        kind,
                        edge_type,
                        valid_from,
                        valid_until,
                        fingerprint,
                        self.world_id,
                        self.version,
                    ),
                )
        except Exception:
            self.conn.rollback()
            raise
        self.conn.commit()

    def search_content(self, query: str, *, limit: int = 10) -> list[dict[str, str]]:
        match_query = build_fts_match_query(query)
        if match_query is None:
            return []
        # Scope-aware read. content_fts carries world_id/version as UNINDEXED
        # columns, so the MATCH is constrained to the store's current (world_id, version) — search
        # only sees this scope's rows. In the single default scope this matches the un-scoped result
        # exactly (every row is in that scope), while a multi-scope DB no longer scans foreign rows.
        # Display text comes from the authoritative content_index join: the FTS columns hold the
        # CJK-bigram rewrite (index tokens), which must never leak into displayed results.
        rows = self.conn.execute(
            """
            SELECT f.ref, f.object_type, c.title, c.body
            FROM content_fts AS f
            JOIN content_index AS c
                ON c.ref = f.ref AND c.world_id = f.world_id AND c.version = f.version
            WHERE content_fts MATCH ? AND f.world_id = ? AND f.version = ?
            LIMIT ?
            """,
            (match_query, self.world_id, self.version, limit),
        ).fetchall()
        return [
            {
                "ref": str(row["ref"]),
                "object_type": str(row["object_type"]),
                "title": str(row["title"]),
                "body": str(row["body"]),
            }
            for row in rows
        ]

    def replace_reference_index(
        self,
        sources: list[Any],
        chunks: list[Any],
    ) -> None:
        """Sync the three reference tables to ``sources``/``chunks`` incrementally.

        A reference source is a (possibly book-length) document; its ``text_hash`` covers its full
        text, so a source whose hash is unchanged has byte-identical chunks and is skipped entirely
        -- the biggest win, since the inspiration corpus is the largest. The diff is per source:
        sources gone from the bundle are pruned (with their chunks + fts rows), sources whose
        text_hash changed (or whose stored metadata/title differs) are re-chunked and re-inserted,
        and unchanged sources are left untouched.

        For a single-scope (default) project the end state matches the old drop-and-reinsert:
        same reference_sources / reference_chunks / reference_fts rows. One transaction.

        Every existing-source read and every delete is scope-filtered to the
        current (world_id, version), and inserts stamp it — so a write to one scope never reads or
        prunes another scope's reference rows (INV-2)."""
        source_titles = {source.id: source.title for source in sources}
        chunks_by_source: dict[str, list[Any]] = {}
        for chunk in chunks:
            chunks_by_source.setdefault(chunk.source_id, []).append(chunk)

        # (text_hash, title, source_type, original_filename, allowed_uses, metadata, created_at) is
        # everything persisted for a source row; comparing the whole tuple means a metadata-only
        # edit re-syncs too, while a genuinely unchanged book is skipped. text_hash alone gates the
        # expensive chunk work below. Scoped to the current (world_id, version).
        existing_sources = {
            str(row["id"]): row
            for row in self.conn.execute(
                """
                SELECT id, title, source_type, original_filename, allowed_uses_json,
                       provenance, text_hash, metadata_json, created_at
                FROM reference_sources
                WHERE world_id = ? AND version = ?
                """,
                (self.world_id, self.version),
            )
        }
        desired_ids = {source.id for source in sources}
        removed_ids = [sid for sid in existing_sources if sid not in desired_ids]

        try:
            for sid in removed_ids:
                self._delete_reference_source(sid)

            for source in sources:
                prior = existing_sources.get(source.id)
                if prior is not None and _reference_source_unchanged(prior, source):
                    continue  # whole book unchanged -- no re-chunk, no re-insert
                # Changed or new: replace the source row and all its chunks/fts rows wholesale. We
                # re-chunk only this one source's chunks (chunk ids are derived from the source, so
                # a text change reshuffles them); deleting by source_id first keeps stale chunks
                # from a shorter previous revision from lingering.
                self._delete_reference_source(source.id)
                self.conn.execute(
                    """
                    INSERT INTO reference_sources (
                        world_id, version, id, title, source_type, original_filename,
                        allowed_uses_json, provenance, text_hash, metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.world_id,
                        self.version,
                        source.id,
                        source.title,
                        source.source_type,
                        source.original_filename,
                        _json(list(source.allowed_uses)),
                        _provenance_value(source),
                        source.text_hash,
                        _json(source.metadata),
                        source.created_at,
                    ),
                )
                for chunk in chunks_by_source.get(source.id, []):
                    ref = f"reference_chunk:{chunk.id}"
                    self.conn.execute(
                        """
                        INSERT INTO reference_chunks (
                            world_id, version, ref, source_id, chunk_index, title, body,
                            metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            self.world_id,
                            self.version,
                            ref,
                            chunk.source_id,
                            chunk.chunk_index,
                            chunk.title,
                            chunk.body,
                            _json(chunk.metadata),
                        ),
                    )
                    self.conn.execute(
                        """
                        INSERT INTO reference_fts (
                            ref, source_id, source_title, world_id, version, title, body
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            ref,
                            chunk.source_id,
                            source_titles.get(chunk.source_id, ""),
                            self.world_id,
                            self.version,
                            cjk_fts_text(chunk.title),
                            cjk_fts_text(chunk.body),
                        ),
                    )
        except Exception:
            self.conn.rollback()
            raise
        self.conn.commit()

    def _delete_reference_source(self, source_id: str) -> None:
        """Remove a reference source and its chunks + fts rows, **within the current scope only**
        (plain fts5 needs manual deletes). Every delete is scope-filtered so pruning a source in one
        scope cannot touch a same-id source in another scope (INV-2)."""
        chunk_refs = [
            str(row["ref"])
            for row in self.conn.execute(
                "SELECT ref FROM reference_chunks "
                "WHERE source_id = ? AND world_id = ? AND version = ?",
                (source_id, self.world_id, self.version),
            )
        ]
        for ref in chunk_refs:
            self.conn.execute(
                "DELETE FROM reference_fts WHERE ref = ? AND world_id = ? AND version = ?",
                (ref, self.world_id, self.version),
            )
        self.conn.execute(
            "DELETE FROM reference_chunks WHERE source_id = ? AND world_id = ? AND version = ?",
            (source_id, self.world_id, self.version),
        )
        self.conn.execute(
            "DELETE FROM reference_sources WHERE id = ? AND world_id = ? AND version = ?",
            (source_id, self.world_id, self.version),
        )

    def list_reference_sources(self) -> list[dict[str, Any]]:
        # Scope-aware read — only the current scope's reference sources.
        rows = self.conn.execute(
            "SELECT * FROM reference_sources WHERE world_id = ? AND version = ? "
            "ORDER BY created_at, id",
            (self.world_id, self.version),
        ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "title": str(row["title"]),
                "source_type": str(row["source_type"]),
                "original_filename": row["original_filename"],
                "allowed_uses": json.loads(str(row["allowed_uses_json"])),
                "provenance": str(row["provenance"]),
                "text_hash": str(row["text_hash"]),
                "metadata": json.loads(str(row["metadata_json"])),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def search_reference_chunks(
        self,
        query: str,
        *,
        limit: int = 10,
        use: str | None = None,
        source_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Lexical recall over the inspiration library.

        ``use`` (see :data:`~owcopilot.inspiration.models.REFERENCE_USES`) restricts the search to
        sources whose ``allowed_uses`` permit it; ``None`` searches the whole library (library
        browsing, where the human is looking at their own shelf rather than grounding a draft).
        ``source_ids`` restricts candidates before ranking/LIMIT; ``None`` keeps the whole
        library and an empty set explicitly selects nothing."""
        match_query = build_fts_match_query(query)
        if match_query is None:
            return self._fallback_reference_search(
                query, limit=limit, use=use, source_ids=source_ids
            )
        # bm25 weights are positional, one per reference_fts column. Column order is now
        # (ref, source_id, source_title, world_id, version, title, body) after the scope columns
        # were added, so the title boost (4.0) and body (1.0) weights moved to positions 6/7; the
        # leading metadata columns stay at 0.0. Keeping this aligned preserves the original ranking.
        # Scope-aware read. reference_fts carries world_id/version as UNINDEXED
        # columns, so the MATCH is constrained to the current scope, and the chunk join is scope-
        # qualified so a same-ref chunk in another scope cannot be paired in. Single default scope:
        # identical rows to the un-scoped path.
        use_clause, use_params = _allowed_use_filter(use)
        source_clause, source_params = _reference_source_filter(source_ids)
        rows = self.conn.execute(
            f"""
            SELECT
                f.ref, f.source_id, f.source_title, c.title, c.body,
                c.chunk_index, c.metadata_json,
                bm25(reference_fts, 0.0, 0.0, 0.0, 0.0, 0.0, 4.0, 1.0) AS rank
            FROM reference_fts AS f
            JOIN reference_chunks AS c
                ON c.ref = f.ref AND c.world_id = f.world_id AND c.version = f.version
            WHERE reference_fts MATCH ? AND f.world_id = ? AND f.version = ?
                {use_clause}{source_clause}
            ORDER BY rank
            LIMIT ?
            """,  # noqa: S608
            (match_query, self.world_id, self.version, *use_params, *source_params, limit),
        ).fetchall()
        hits = [_reference_hit_from_row(row, score=-float(row["rank"])) for row in rows]
        if len(hits) < limit:
            seen = {str(hit["ref"]) for hit in hits}
            hits.extend(
                hit
                for hit in self._fallback_reference_search(
                    query, limit=limit, use=use, source_ids=source_ids
                )
                if hit["ref"] not in seen
            )
        return hits[:limit]

    def _fallback_reference_search(
        self,
        query: str,
        *,
        limit: int,
        use: str | None = None,
        source_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        # Scope-aware read. The lexical fallback is a full scan of the chunk
        # table, so scoping it to the current (world_id, version) both isolates the scope and is a
        # reduce-N win: it scans only this scope's chunks. The source join is scope-qualified for
        # the same reason as the FTS path. Single default scope: identical scan/result to the
        # un-scoped path.
        use_clause, use_params = _allowed_use_filter(use)
        source_clause, source_params = _reference_source_filter(source_ids)
        rows = self.conn.execute(
            f"""
            SELECT
                c.ref, c.source_id, s.title AS source_title, c.title, c.body,
                c.chunk_index, c.metadata_json
            FROM reference_chunks AS c
            LEFT JOIN reference_sources AS s
                ON s.id = c.source_id AND s.world_id = c.world_id AND s.version = c.version
            WHERE c.world_id = ? AND c.version = ?{use_clause}{source_clause}
            ORDER BY c.ref
            """,  # noqa: S608
            (self.world_id, self.version, *use_params, *source_params),
        ).fetchall()
        hits: list[dict[str, Any]] = []
        for row in rows:
            score = _lexical_score(
                query,
                [
                    str(row["ref"]),
                    str(row["source_title"] or ""),
                    str(row["title"]),
                    str(row["body"]),
                ],
            )
            if score <= 0:
                continue
            hits.append(_reference_hit_from_row(row, score=score))
        return sorted(hits, key=lambda hit: (-float(hit["score"]), str(hit["ref"])))[:limit]


FTS_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "for",
    "in",
    "is",
    "of",
    "the",
    "to",
    "what",
    "who",
}


# The FTS column shapes, shared by the executescript DDL, the scope migration and the CJK bigram
# migration (one source, three consumers — the rebuilds must recreate the exact same shape).
CONTENT_FTS_COLUMNS_DDL = (
    "ref UNINDEXED, object_type UNINDEXED, world_id UNINDEXED, version UNINDEXED, title, body"
)
REFERENCE_FTS_COLUMNS_DDL = (
    "ref UNINDEXED, source_id UNINDEXED, source_title UNINDEXED, "
    "world_id UNINDEXED, version UNINDEXED, title, body"
)

# Same CJK range as util.slugify (U+3400–U+9FFF: Extension A + the main block).
_CJK_RUN = re.compile(r"[㐀-鿿]{2,}")


def cjk_fts_text(text: str) -> str:
    """Rewrite CJK runs as space-joined bigrams for FTS indexing and matching.

    fts5's unicode61 tokenizer treats a consecutive CJK run as ONE token, so "雾隐盟" in a body
    only ever matched the identical full run — Chinese queries missed the FTS index and dropped
    into the O(N) lexical fallback on every search. Indexing and querying through this same
    bigram rewrite makes 2+ char Chinese queries first-class FTS citizens (the standard
    CJK-on-FTS5 practice) with zero effect on Latin text. Adjacent bigrams reassemble the
    original string, so a bigram *phrase* query is an exact substring match. Single CJK chars
    stay as-is (they still fall back to the bounded lexical scan)."""
    return _CJK_RUN.sub(
        lambda m: " ".join(m.group(0)[i : i + 2] for i in range(len(m.group(0)) - 1)), text
    )


def build_fts_match_query(query: str) -> str | None:
    """Turn natural-language input into a safe FTS5 MATCH expression.

    A CJK token expands to its bigram *phrase* (see :func:`cjk_fts_text`) so it matches the
    bigram-indexed rows as an exact substring; other tokens stay quoted terms as before."""
    tokens = [
        token
        for token in re.findall(r"[\w]+", query, flags=re.UNICODE)
        if token.lower() not in FTS_STOP_WORDS
    ]
    if not tokens:
        return None
    return " OR ".join(f'"{cjk_fts_text(token)}"' for token in tokens)


def _lexical_score(query: str, fields: list[str]) -> float:
    haystack = " ".join(fields).lower()
    score = 0.0
    for token in re.findall(r"[\w]+", query.lower(), flags=re.UNICODE):
        if token in FTS_STOP_WORDS or not token:
            continue
        if token in haystack:
            score += float(len(token))
    return score


def _patch_from_row(row: sqlite3.Row) -> dict[str, Any]:
    rollback_raw = row["rollback_ops_json"]
    return {
        "id": str(row["id"]),
        "issue_id": row["issue_id"],
        "status": str(row["status"]),
        "ops": json.loads(str(row["ops_json"])),
        "rationale": str(row["rationale"]),
        "evidence": json.loads(str(row["evidence_json"])),
        "origin": str(row["origin"]),
        "applied_by": row["applied_by"],
        "applied_at": row["applied_at"],
        "rollback_ops": json.loads(str(rollback_raw)) if rollback_raw is not None else None,
        "rolled_back_by": row["rolled_back_by"],
        "rolled_back_at": row["rolled_back_at"],
    }


def _review_item_from_row(row: sqlite3.Row) -> dict[str, Any]:
    keys = {desc[0] for desc in row.description} if hasattr(row, "description") else set(row.keys())
    result: dict[str, Any] = {
        "id": str(row["id"]),
        "item_type": str(row["item_type"]),
        "object_ref": str(row["object_ref"]),
        "payload": json.loads(str(row["payload_json"])),
        "issue_refs": json.loads(str(row["issue_refs_json"])),
        "status": str(row["status"]),
        "created_at": row["created_at"],
        "decided_by": row["decided_by"],
        "decided_at": row["decided_at"],
        "critic_verdict": row["critic_verdict"],
        "critic_score": row["critic_score"],
    }
    # critic_primary_dim may not exist on older DBs (added via _ensure_column)
    if "critic_primary_dim" in keys:
        result["critic_primary_dim"] = row["critic_primary_dim"]
    return result


def _issue_from_row(row: sqlite3.Row) -> Issue:
    payload = {
        "id": row["id"],
        "rule_code": row["rule_code"],
        "severity": row["severity"],
        "category": row["category"],
        "target_ref": row["target_ref"],
        "message": row["message"],
        "evidence": json.loads(str(row["evidence_json"])),
        "fingerprint": row["fingerprint"],
        "audit_run_id": row["audit_run_id"],
        "status": row["status"],
    }
    # 六要素的四列是 _ensure_column 后加的：旧库里可能整列不存在，取不到就退回模型默认值。
    keys = row.keys()
    for name in ("source_path", "source_line", "node_ref", "suggested_action"):
        if name in keys and row[name] is not None:
            payload[name] = row[name]
    return Issue.model_validate(payload)


def _content_rows(bundle: ContentBundle) -> list[tuple[str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str]] = []
    for entity in bundle.entities.values():
        rows.append(
            (
                f"entity:{entity.id}",
                "entity",
                entity.id,
                entity.name,
                " ".join(
                    [
                        entity.id,
                        entity.type.value,
                        entity.status,
                        entity.description,
                        " ".join(entity.aliases),
                        " ".join(entity.tags),
                        _metadata_text(entity.metadata),
                    ]
                ),
            )
        )
    for relation_index, relation in enumerate(bundle.relations):
        relation_id = f"{relation.source}:{relation.kind}:{relation.target}:{relation_index}"
        rows.append(
            (
                f"relation:{relation_id}",
                "relation",
                relation_id,
                f"{relation.source} {relation.kind} {relation.target}",
                _metadata_text(relation.metadata),
            )
        )
    for quest in bundle.quests.values():
        rows.append(
            (
                "quest:" + quest.id,
                "quest",
                quest.id,
                quest.title,
                " ".join(
                    [
                        quest.id,
                        quest.objective,
                        f"giver_npc={quest.giver_npc or ''}",
                        f"location={quest.location or ''}",
                        "prerequisites=" + " ".join(quest.prerequisites),
                        "dialogues=" + " ".join(quest.dialogue_refs),
                        "localization_keys=" + " ".join(quest.localization_keys),
                        f"timeline_order={quest.timeline_order or ''}",
                        " ".join(quest.tags),
                        _metadata_text(quest.metadata),
                    ]
                ),
            )
        )
    for event_ref in bundle.quest_event_refs.values():
        rows.append(
            (
                "quest_event_ref:" + event_ref.id,
                "quest_event_ref",
                event_ref.id,
                f"{event_ref.quest_id} {event_ref.ref_kind.value} {event_ref.event_id}",
                " ".join([event_ref.note, _metadata_text(event_ref.metadata)]),
            )
        )
    for region in bundle.regions.values():
        rows.append(
            (
                "region:" + region.id,
                "region",
                region.id,
                region.name,
                " ".join(
                    [
                        region.id,
                        f"level_min={region.level_min or ''}",
                        f"level_max={region.level_max or ''}",
                        "themes=" + " ".join(region.themes),
                        "allowed=" + " ".join(region.allowed_content),
                        "banned=" + " ".join(region.banned_content),
                        _metadata_text(region.metadata),
                    ]
                ),
            )
        )
    for poi in bundle.pois.values():
        rows.append(
            (
                "poi:" + poi.id,
                "poi",
                poi.id,
                poi.name,
                " ".join(
                    [
                        poi.id,
                        poi.purpose,
                        f"region_id={poi.region_id or ''}",
                        f"controlling_faction={poi.controlling_faction or ''}",
                        f"level_min={poi.level_min or ''}",
                        f"level_max={poi.level_max or ''}",
                        " ".join(poi.tags),
                        _metadata_text(poi.metadata),
                    ]
                ),
            )
        )
    for dialogue in bundle.dialogues.values():
        rows.append(
            (
                "dialogue:" + dialogue.id,
                "dialogue",
                dialogue.id,
                dialogue.text_key,
                " ".join(
                    [
                        dialogue.id,
                        dialogue.text or "",
                        f"speaker_id={dialogue.speaker_id or ''}",
                        f"quest_id={dialogue.quest_id or ''}",
                        f"locale={dialogue.locale or ''}",
                        f"ui_max_len={dialogue.ui_max_len or ''}",
                        _metadata_text(dialogue.metadata),
                    ]
                ),
            )
        )
    for tree in bundle.dialogue_trees.values():
        rows.append(
            (
                "dialogue_tree:" + tree.id,
                "dialogue_tree",
                tree.id,
                tree.title or tree.id,
                " ".join(
                    [
                        tree.id,
                        f"quest_id={tree.quest_id or ''}",
                        f"root_node={tree.root_node or ''}",
                        "participants=" + " ".join(tree.participants),
                        " ".join(tree.tags),
                        _dialogue_tree_body(tree),
                        _metadata_text(tree.metadata),
                    ]
                ),
            )
        )
    for text in bundle.localized_texts.values():
        rows.append(
            (
                "localized_text:" + text.id,
                "localized_text",
                text.id,
                text.text_key,
                " ".join(
                    [
                        text.text,
                        f"locale={text.locale}",
                        f"ui_max_len={text.ui_max_len or ''}",
                        _metadata_text(text.metadata),
                    ]
                ),
            )
        )
    for term in bundle.terms.values():
        rows.append(
            (
                "term:" + term.id,
                "term",
                term.id,
                term.canonical,
                " ".join([term.description, " ".join(term.aliases), " ".join(term.forbidden)]),
            )
        )
    return rows


def _content_row_hash(title: str, body: str) -> str:
    """Content fingerprint for a content_index row.

    Mirrors the vector layer's text_hash (sha1 over the row's indexable text) so the incremental
    sync re-stamps a row only when its title or body actually changed. A NUL separator keeps
    ``(title, body)`` unambiguous against ``(title+body, "")``."""
    return hashlib.sha1(f"{title}\x00{body}".encode()).hexdigest()


def _edge_fingerprint(edge: Any, occurrence: int) -> str:
    """Deterministic, stable key for a graph edge row.

    graph_edges has no natural key and a MultiDiGraph can hold byte-identical parallel edges, so
    the fingerprint folds in an occurrence ordinal: the first identical edge is ``#0``, the second
    ``#1`` and so on. This keeps each parallel edge mapped to its own row (preserving multiplicity)
    while staying deterministic across re-opens."""
    parts = [
        str(edge.source),
        str(edge.target),
        str(edge.kind),
        str(edge.edge_type),
        "" if edge.valid_from is None else str(edge.valid_from),
        "" if edge.valid_until is None else str(edge.valid_until),
        f"#{occurrence}",
    ]
    return hashlib.sha1("\x00".join(parts).encode()).hexdigest()


def _reference_source_unchanged(prior: sqlite3.Row, source: Any) -> bool:
    """True when a persisted reference source row matches the bundle source byte-for-byte.

    text_hash gates the (expensive) chunk rebuild, but a metadata/title-only edit must still
    re-sync the source row, so every persisted column is compared. ``_json`` is the same canonical
    serializer used on write, so the JSON columns compare exactly."""
    return (
        str(prior["text_hash"]) == str(source.text_hash)
        and str(prior["title"]) == str(source.title)
        and str(prior["source_type"]) == str(source.source_type)
        and prior["original_filename"] == source.original_filename
        and str(prior["allowed_uses_json"]) == _json(list(source.allowed_uses))
        and str(prior["provenance"]) == _provenance_value(source)
        and str(prior["metadata_json"]) == _json(source.metadata)
        and str(prior["created_at"]) == str(source.created_at)
    )


def _provenance_value(source: Any) -> str:
    """素材来源归属的落库取值。

    素材模型给的是 ``Provenance`` 枚举，但这个函数的入参是鸭子类型（``replace_reference_index``
    对 source 只要求几个属性），所以直接给字符串的调用方也接受；两处（写入与"是否变更"比对）
    必须走同一个换算，否则每次 sync 都会误判成变更、把整本书重新分块。"""
    # 延迟导入：``owcopilot.inspiration`` 会导入本模块（它的检索器读这个库），模块级导入会成环。
    from ..inspiration.models import Provenance

    raw = getattr(source, "provenance", None) or Provenance.THIRD_PARTY_REFERENCE
    return str(getattr(raw, "value", raw))


# Tier threshold: the minimum persisted-vector count at which ``make_vector_backend(ann=True)``
# switches from the exact sqlite-vec scan to the on-disk usearch HNSW ANN backend. Below this, even
# an opt-in caller stays on the exact scan — that is what guarantees small / eval corpora (a few
# hundred rows) keep recall 1.0 and never see ANN approximation. 5_000 is the order where the O(N)
# brute scan starts to dominate latency while the HNSW build cost stays modest.
USEARCH_MIN_N = 5_000

_VECTOR_TABLES = {"content_vectors", "reference_vectors", "community_vectors"}

# The vec0 virtual table that backs each blob cache table. The blob table stays the authoritative
# fp32 source (and the cross-backend fallback); the vec0 table is the disk-resident search index.
_VEC0_TABLES = {
    "content_vectors": "content_vec",
    "reference_vectors": "reference_vec",
    "community_vectors": "community_vec",
}

# The int8 vec0 index table per blob cache table. Distinct from the fp32 ``_VEC0_TABLES`` so
# a project can hold both the fp32 and the int8 search index side by side without name collision;
# the int8 backend additionally creates its own ``{name}_fp32`` rerank sidecar.
_VEC0_INT8_TABLES = {
    "content_vectors": "content_vec_i8",
    "reference_vectors": "reference_vec_i8",
    "community_vectors": "community_vec_i8",
}


def _vectors_table(table: str) -> str:
    """Whitelist the vectors table name before it is interpolated into SQL."""
    if table not in _VECTOR_TABLES:
        raise ValueError(f"unknown vectors table: {table!r}")
    return table


def _vec0_table(table: str) -> str:
    """The validated vec0 virtual-table name for a given blob cache ``table``."""
    if table not in _VEC0_TABLES:
        raise ValueError(f"unknown vectors table: {table!r}")
    return _VEC0_TABLES[table]


def _vec0_int8_table(table: str) -> str:
    """The validated int8 vec0 virtual-table name for a given blob cache ``table``."""
    if table not in _VEC0_INT8_TABLES:
        raise ValueError(f"unknown vectors table: {table!r}")
    return _VEC0_INT8_TABLES[table]


def _reference_source_filter(source_ids: set[str] | None) -> tuple[str, tuple[str, ...]]:
    """Select reference candidates before top-k without widening an explicit empty selection."""
    if source_ids is None:
        return "", ()
    values = tuple(sorted(source_ids))
    if not values:
        return " AND 0", ()
    placeholders = ",".join("?" for _ in values)
    return f" AND c.source_id IN ({placeholders})", values


def _allowed_use_filter(use: str | None) -> tuple[str, tuple[str, ...]]:
    """Build the SQL fragment that keeps a reference read to sources permitting ``use``.

    ``allowed_uses`` is stored as a JSON array of strings (``_json`` writes it compactly, so a
    value appears as ``"inspiration"`` with its quotes). Matching the *quoted* token is therefore a
    member test rather than a substring test — ``"drafting"`` matches neither ``"redrafting"`` nor
    ``"drafting_extra"``. (SQLite's LIKE is ASCII case-insensitive, so the only extra thing it can
    match is the same use spelled in another case.) The vocabulary is validated first, so no
    caller-controlled text reaches the pattern; the EXISTS form leaves the query plan of the
    un-filtered read untouched when ``use`` is None."""
    if use is None:
        return "", ()
    # Deferred import: ``owcopilot.inspiration`` imports this module (its retriever reads from the
    # store), so a module-level import here would close the cycle.
    from ..inspiration.models import REFERENCE_USES

    if use not in REFERENCE_USES:
        raise ValueError(
            f"未知的素材用途「{use}」。可用值：{'、'.join(sorted(REFERENCE_USES))}；"
            "不传则检索整个素材库。"
        )
    clause = (
        " AND EXISTS (SELECT 1 FROM reference_sources AS src"
        " WHERE src.id = c.source_id AND src.world_id = c.world_id"
        " AND src.version = c.version AND src.allowed_uses_json LIKE ?)"
    )
    return clause, (f'%"{use}"%',)


def _reference_hit_from_row(row: sqlite3.Row, *, score: float) -> dict[str, Any]:
    return {
        "ref": str(row["ref"]),
        "object_type": "reference_chunk",
        "source_id": str(row["source_id"]),
        "source_title": str(row["source_title"] or ""),
        "title": str(row["title"]),
        "body": str(row["body"]),
        "chunk_index": int(row["chunk_index"]),
        "metadata": json.loads(str(row["metadata_json"])),
        "score": score,
    }


def _community_report_from_row(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    parent_id = row["parent_id"] if "parent_id" in keys else None
    hier_level = row["hier_level"] if "hier_level" in keys else 0
    return {
        "id": str(row["id"]),
        "level": str(row["level"]),
        "title": str(row["title"]),
        "summary": str(row["summary"]),
        "member_refs": json.loads(str(row["member_refs_json"])),
        "fingerprint": str(row["fingerprint"]),
        "parent_id": None if parent_id is None else str(parent_id),
        "hier_level": 0 if hier_level is None else int(hier_level),
    }


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _dialogue_tree_body(tree: DialogueTree) -> str:
    """The searchable text of one dialogue tree: its wiring, not just its prose.

    A tree's *structure* is what a reader has to see — which node points where, which choice is
    gated on what, which variable a choice mutates, which line has no ``text_key``. Indexing only
    the spoken lines would put a tree in the index while still hiding every structural defect, so
    each node and each choice contributes its link/gate/effect fields as ``name=value`` tokens
    alongside the text. Node and choice ids are emitted bare so a query naming ``n_orphan`` (or a
    dangling ``next_node`` target that exists nowhere else) can retrieve the tree that mentions it.
    """
    parts: list[str] = []
    for node_id, node in tree.nodes.items():
        parts.append(
            " ".join(
                [
                    f"node={node_id}",
                    node_id,
                    f"speaker_id={node.speaker_id or ''}",
                    f"text_key={node.text_key or ''}",
                    f"next_node={node.next_node or ''}",
                    node.text or "",
                ]
            )
        )
        for index, choice in enumerate(node.choices):
            effects = " ".join(f"{e.var}:{e.op}={e.value}" for e in choice.effects)
            parts.append(
                " ".join(
                    [
                        f"choice={node_id}#{index}",
                        f"next_node={choice.next_node or ''}",
                        f"condition={choice.condition or ''}",
                        f"effects={effects}",
                        f"text_key={choice.text_key or ''}",
                        choice.text or "",
                    ]
                )
            )
    return " ".join(parts)


def _metadata_text(metadata: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in sorted(metadata.items()):
        if isinstance(value, list):
            parts.append(f"{key}=" + " ".join(str(item) for item in value))
        else:
            parts.append(f"{key}={value}")
    return " ".join(parts)
