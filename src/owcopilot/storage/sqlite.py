"""SQLite runtime storage.

Content files remain the source of truth. This store holds rebuildable runtime state:
audit runs, issues, patches, graph edges, search index rows and telemetry.
"""

from __future__ import annotations

import json
import re
import sqlite3
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
                decided_at TEXT
            );

            CREATE TABLE IF NOT EXISTS content_index (
                ref TEXT PRIMARY KEY,
                object_type TEXT NOT NULL,
                object_id TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL
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
            """
        )
        # Older runtime DBs predate the rollback column; content files are the source of truth,
        # but runtime DBs should still upgrade in place rather than force a delete.
        self._ensure_column("patches", "rollback_ops_json", "TEXT")
        self._ensure_column("patches", "rolled_back_by", "TEXT")
        self._ensure_column("patches", "rolled_back_at", "TEXT")
        self.conn.commit()

    def _ensure_column(self, table: str, column: str, column_type: str) -> None:
        existing = {
            str(row["name"]) for row in self.conn.execute(f"PRAGMA table_info({table})")
        }
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
        issue_id = issue.id or content_hash(issue.model_dump(mode="json", exclude_none=True))
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
                decided_by, decided_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        self.conn.commit()

    def get_review_item(self, item_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM review_items WHERE id = ?", (item_id,)
        ).fetchone()
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
    ) -> dict[str, Any]:
        if self.get_review_item(item_id) is None:
            raise KeyError(item_id)
        self.conn.execute(
            """
            UPDATE review_items SET status = ?,
                decided_by = COALESCE(?, decided_by),
                decided_at = COALESCE(?, decided_at)
            WHERE id = ?
            """,
            (status, decided_by, decided_at, item_id),
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
