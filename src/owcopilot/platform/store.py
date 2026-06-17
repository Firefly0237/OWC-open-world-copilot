"""SQLite control-plane store for platform metadata (tenants / users / memberships / audit log).

The canon stays file-backed; this small relational store holds only *who can touch what*. SQLite
keeps dev/test/CI single-file and $0; a production deployment points the same schema at Postgres
(see deploy/). It never stores canon content.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .models import AuditEntry, Membership, Role, Tenant, User


class PlatformStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        # check_same_thread=False: this is a long-lived singleton shared across the API threadpool
        # (unlike the per-request content SQLiteStore). SQLite serialises access internally.
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init()

    def close(self) -> None:
        self.conn.close()

    def _init(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tenants (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS memberships (
                tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, role TEXT NOT NULL,
                PRIMARY KEY (tenant_id, user_id));
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL DEFAULT '');
            CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit_log(tenant_id);
            """
        )
        self.conn.commit()

    # --- tenants / users / memberships ---
    def create_tenant(self, tenant: Tenant) -> Tenant:
        self.conn.execute(
            "INSERT INTO tenants (id, name, created_at) VALUES (?, ?, ?)",
            (tenant.id, tenant.name, tenant.created_at),
        )
        self.conn.commit()
        return tenant

    def create_user(self, user: User) -> User:
        self.conn.execute(
            "INSERT INTO users (id, email, created_at) VALUES (?, ?, ?)",
            (user.id, user.email, user.created_at),
        )
        self.conn.commit()
        return user

    def add_membership(self, membership: Membership) -> Membership:
        self.conn.execute(
            "INSERT OR REPLACE INTO memberships (tenant_id, user_id, role) VALUES (?, ?, ?)",
            (membership.tenant_id, membership.user_id, membership.role.value),
        )
        self.conn.commit()
        return membership

    def role_of(self, tenant_id: str, user_id: str) -> Role | None:
        row = self.conn.execute(
            "SELECT role FROM memberships WHERE tenant_id = ? AND user_id = ?",
            (tenant_id, user_id),
        ).fetchone()
        return Role(row["role"]) if row is not None else None

    def list_tenants_for_user(self, user_id: str) -> list[tuple[str, Role]]:
        rows = self.conn.execute(
            "SELECT tenant_id, role FROM memberships WHERE user_id = ? ORDER BY tenant_id",
            (user_id,),
        ).fetchall()
        return [(str(r["tenant_id"]), Role(r["role"])) for r in rows]

    # --- audit log ---
    def record(self, entry: AuditEntry) -> None:
        self.conn.execute(
            "INSERT INTO audit_log (at, tenant_id, user_id, action, target) VALUES (?, ?, ?, ?, ?)",
            (entry.at, entry.tenant_id, entry.user_id, entry.action, entry.target),
        )
        self.conn.commit()

    def audit_for_tenant(self, tenant_id: str) -> list[AuditEntry]:
        rows = self.conn.execute(
            "SELECT at, tenant_id, user_id, action, target FROM audit_log "
            "WHERE tenant_id = ? ORDER BY id",
            (tenant_id,),
        ).fetchall()
        return [AuditEntry(**dict(r)) for r in rows]
