"""SQLite runtime storage.

Content files remain the source of truth. This store holds rebuildable runtime state:
audit runs, issues, patches, graph edges, search index rows and telemetry.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..audit.models import AuditRun, Issue
from ..content.hash import content_hash
from ..content.models import ContentBundle
from ..graph.index import ContentGraph


class SQLiteStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        # WAL lets the Workbench read while the CLI writes (and vice versa); busy_timeout keeps
        # short lock contention from surfacing as immediate "database is locked" errors.
        # On :memory: databases WAL is a no-op and SQLite just reports "memory".
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.initialize()

    def close(self) -> None:
        self.conn.close()

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
                ref TEXT PRIMARY KEY,
                object_type TEXT NOT NULL,
                object_id TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS content_vectors (
                ref TEXT NOT NULL,
                model_id TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                dim INTEGER NOT NULL,
                vector BLOB NOT NULL,
                PRIMARY KEY (ref, model_id)
            );

            CREATE TABLE IF NOT EXISTS reference_vectors (
                ref TEXT NOT NULL,
                model_id TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                dim INTEGER NOT NULL,
                vector BLOB NOT NULL,
                PRIMARY KEY (ref, model_id)
            );

            CREATE TABLE IF NOT EXISTS graph_edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                kind TEXT NOT NULL,
                edge_type TEXT NOT NULL,
                valid_from INTEGER,
                valid_until INTEGER
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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS content_fts USING fts5(
                ref UNINDEXED,
                object_type UNINDEXED,
                title,
                body
            );

            CREATE TABLE IF NOT EXISTS reference_sources (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                source_type TEXT NOT NULL,
                original_filename TEXT,
                allowed_uses_json TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reference_chunks (
                ref TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_reference_chunks_source_id
                ON reference_chunks(source_id);

            CREATE VIRTUAL TABLE IF NOT EXISTS reference_fts USING fts5(
                ref UNINDEXED,
                source_id UNINDEXED,
                source_title UNINDEXED,
                title,
                body
            );

            CREATE TABLE IF NOT EXISTS community_reports (
                id TEXT PRIMARY KEY,             -- community id (c0…) or "_global"
                level TEXT NOT NULL,             -- community | global
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                member_refs_json TEXT NOT NULL,  -- provenance: the canon ids this report covers
                fingerprint TEXT NOT NULL,       -- cache key (members + their content hash)
                created_at TEXT NOT NULL
            );
            """
        )
        # Older runtime DBs predate the rollback column; content files are the source of truth,
        # but runtime DBs should still upgrade in place rather than force a delete.
        self._ensure_column("patches", "rollback_ops_json", "TEXT")
        self._ensure_column("patches", "rolled_back_by", "TEXT")
        self._ensure_column("patches", "rolled_back_at", "TEXT")
        # The critic's final verdict/score, recorded at draft time so reviewer calibration can pair
        # it with the human decision later. Older runtime DBs upgrade in place.
        self._ensure_column("review_items", "critic_verdict", "TEXT")
        self._ensure_column("review_items", "critic_score", "REAL")
        self.conn.commit()

    def _ensure_column(self, table: str, column: str, column_type: str) -> None:
        existing = {str(row["name"]) for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")

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
                fingerprint, audit_run_id, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def list_issues(
        self,
        *,
        severity: str | None = None,
        rule_code: str | None = None,
        status: str | None = None,
    ) -> list[Issue]:
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
        rows = self.conn.execute(f"SELECT * FROM issues{where} ORDER BY id", values).fetchall()
        return [_issue_from_row(row) for row in rows]

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
                decided_by, decided_at, critic_verdict, critic_score
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def replace_content_index(self, bundle: ContentBundle) -> None:
        self.conn.execute("DELETE FROM content_index")
        self.conn.execute("DELETE FROM content_fts")
        rows = list(_content_rows(bundle))
        self.conn.executemany(
            """
            INSERT INTO content_index (ref, object_type, object_id, title, body)
            VALUES (?, ?, ?, ?, ?)
            """,
            rows,
        )
        self.conn.executemany(
            "INSERT INTO content_fts (ref, object_type, title, body) VALUES (?, ?, ?, ?)",
            [(ref, object_type, title, body) for ref, object_type, _object_id, title, body in rows],
        )
        self.conn.commit()

    def get_vectors(
        self, model_id: str, *, table: str = "content_vectors"
    ) -> dict[str, tuple[str, int, bytes]]:
        """Persisted embeddings for ``model_id`` as ``{ref: (text_hash, dim, vector_blob)}``.

        Shared by the content-graph and inspiration-reference vector retrievers (``table``). The
        text_hash lets a retriever embed only rows whose text (or the model) changed, so
        re-opening a project never re-runs the embedder over unchanged rows."""
        table = _vectors_table(table)
        rows = self.conn.execute(
            f"SELECT ref, text_hash, dim, vector FROM {table} WHERE model_id = ?",  # noqa: S608
            (model_id,),
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
            INSERT INTO {table} (ref, model_id, text_hash, dim, vector)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(ref, model_id) DO UPDATE SET
                text_hash = excluded.text_hash, dim = excluded.dim, vector = excluded.vector
            """,  # noqa: S608
            [(ref, model_id, text_hash, dim, blob) for ref, text_hash, dim, blob in rows],
        )
        self.conn.commit()

    def prune_vectors(
        self, model_id: str, keep_refs: set[str], *, table: str = "content_vectors"
    ) -> None:
        """Drop cached vectors for refs no longer present, keeping the table in step."""
        table = _vectors_table(table)
        stale = [
            str(row["ref"])
            for row in self.conn.execute(
                f"SELECT ref FROM {table} WHERE model_id = ?",
                (model_id,),  # noqa: S608
            ).fetchall()
            if str(row["ref"]) not in keep_refs
        ]
        if stale:
            self.conn.executemany(
                f"DELETE FROM {table} WHERE model_id = ? AND ref = ?",  # noqa: S608
                [(model_id, ref) for ref in stale],
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
        rows = self.conn.execute(
            "SELECT ref, object_type, title, body FROM content_index WHERE object_type = 'relation'"
        ).fetchall()
        matched: list[sqlite3.Row] = []
        for row in rows:
            tokens = str(row["title"]).split()  # "source kind... target": ids are slug tokens
            if tokens and (tokens[0] in entity_ids or tokens[-1] in entity_ids):
                matched.append(row)
        return matched

    def reference_chunks_by_refs(self, refs: list[str]) -> dict[str, sqlite3.Row]:
        """Fetch full reference-chunk rows (with source title) for the given refs.

        Lets the hybrid reference retriever rank by ref, then materialise display rows with
        correct source metadata regardless of which leg (BM25 or vector) surfaced each ref."""
        if not refs:
            return {}
        placeholders = ",".join("?" for _ in refs)
        rows = self.conn.execute(
            f"""
            SELECT c.ref, c.source_id, s.title AS source_title, c.title, c.body,
                   c.chunk_index, c.metadata_json
            FROM reference_chunks AS c
            LEFT JOIN reference_sources AS s ON s.id = c.source_id
            WHERE c.ref IN ({placeholders})
            """,  # noqa: S608
            refs,
        ).fetchall()
        return {str(row["ref"]): row for row in rows}

    # --- GraphRAG community reports (the macro-overview index) -----------------------------------

    def save_community_report(self, report: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO community_reports (
                id, level, title, summary, member_refs_json, fingerprint, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(report["id"]),
                str(report["level"]),
                str(report["title"]),
                str(report["summary"]),
                _json(list(report.get("member_refs", []))),
                str(report["fingerprint"]),
                _now_iso(),
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
        self.conn.execute("DELETE FROM graph_edges")
        self.conn.executemany(
            """
            INSERT INTO graph_edges (source, target, kind, edge_type, valid_from, valid_until)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    edge.source,
                    edge.target,
                    edge.kind,
                    edge.edge_type,
                    edge.valid_from,
                    edge.valid_until,
                )
                for edge in graph.edge_refs()
            ],
        )
        self.conn.commit()

    def search_content(self, query: str, *, limit: int = 10) -> list[dict[str, str]]:
        match_query = build_fts_match_query(query)
        if match_query is None:
            return []
        rows = self.conn.execute(
            """
            SELECT ref, object_type, title, body
            FROM content_fts
            WHERE content_fts MATCH ?
            LIMIT ?
            """,
            (match_query, limit),
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
        self.conn.execute("DELETE FROM reference_sources")
        self.conn.execute("DELETE FROM reference_chunks")
        self.conn.execute("DELETE FROM reference_fts")
        self.conn.executemany(
            """
            INSERT INTO reference_sources (
                id, title, source_type, original_filename, allowed_uses_json,
                text_hash, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    source.id,
                    source.title,
                    source.source_type,
                    source.original_filename,
                    _json(list(source.allowed_uses)),
                    source.text_hash,
                    _json(source.metadata),
                    source.created_at,
                )
                for source in sources
            ],
        )
        source_titles = {source.id: source.title for source in sources}
        self.conn.executemany(
            """
            INSERT INTO reference_chunks (
                ref, source_id, chunk_index, title, body, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    f"reference_chunk:{chunk.id}",
                    chunk.source_id,
                    chunk.chunk_index,
                    chunk.title,
                    chunk.body,
                    _json(chunk.metadata),
                )
                for chunk in chunks
            ],
        )
        self.conn.executemany(
            """
            INSERT INTO reference_fts (ref, source_id, source_title, title, body)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    f"reference_chunk:{chunk.id}",
                    chunk.source_id,
                    source_titles.get(chunk.source_id, ""),
                    chunk.title,
                    chunk.body,
                )
                for chunk in chunks
            ],
        )
        self.conn.commit()

    def list_reference_sources(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM reference_sources ORDER BY created_at, id"
        ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "title": str(row["title"]),
                "source_type": str(row["source_type"]),
                "original_filename": row["original_filename"],
                "allowed_uses": json.loads(str(row["allowed_uses_json"])),
                "text_hash": str(row["text_hash"]),
                "metadata": json.loads(str(row["metadata_json"])),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def search_reference_chunks(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        match_query = build_fts_match_query(query)
        if match_query is None:
            return self._fallback_reference_search(query, limit=limit)
        rows = self.conn.execute(
            """
            SELECT
                f.ref, f.source_id, f.source_title, f.title, f.body,
                c.chunk_index, c.metadata_json,
                bm25(reference_fts, 0.0, 0.0, 4.0, 1.0) AS rank
            FROM reference_fts AS f
            JOIN reference_chunks AS c ON c.ref = f.ref
            WHERE reference_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (match_query, limit),
        ).fetchall()
        hits = [_reference_hit_from_row(row, score=-float(row["rank"])) for row in rows]
        if len(hits) < limit:
            seen = {str(hit["ref"]) for hit in hits}
            hits.extend(
                hit
                for hit in self._fallback_reference_search(query, limit=limit)
                if hit["ref"] not in seen
            )
        return hits[:limit]

    def _fallback_reference_search(self, query: str, *, limit: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT
                c.ref, c.source_id, s.title AS source_title, c.title, c.body,
                c.chunk_index, c.metadata_json
            FROM reference_chunks AS c
            LEFT JOIN reference_sources AS s ON s.id = c.source_id
            ORDER BY c.ref
            """
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


def build_fts_match_query(query: str) -> str | None:
    """Turn natural-language input into a safe FTS5 MATCH expression."""
    tokens = [
        token
        for token in re.findall(r"[\w]+", query, flags=re.UNICODE)
        if token.lower() not in FTS_STOP_WORDS
    ]
    if not tokens:
        return None
    return " OR ".join(f'"{token}"' for token in tokens)


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
    return {
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


def _issue_from_row(row: sqlite3.Row) -> Issue:
    return Issue.model_validate(
        {
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
    )


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


_VECTOR_TABLES = {"content_vectors", "reference_vectors"}


def _vectors_table(table: str) -> str:
    """Whitelist the vectors table name before it is interpolated into SQL."""
    if table not in _VECTOR_TABLES:
        raise ValueError(f"unknown vectors table: {table!r}")
    return table


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
    return {
        "id": str(row["id"]),
        "level": str(row["level"]),
        "title": str(row["title"]),
        "summary": str(row["summary"]),
        "member_refs": json.loads(str(row["member_refs_json"])),
        "fingerprint": str(row["fingerprint"]),
    }


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _metadata_text(metadata: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in sorted(metadata.items()):
        if isinstance(value, list):
            parts.append(f"{key}=" + " ".join(str(item) for item in value))
        else:
            parts.append(f"{key}={value}")
    return " ".join(parts)
