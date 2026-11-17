# 平台元数据的 SQLite 控制面存储（租户/用户/成员关系/审计日志）。
"""SQLite control-plane store for platform metadata (tenants / users / memberships / audit log).

The canon stays file-backed; this small relational store holds only *who can touch what*. SQLite
keeps every deployment single-file and $0 — there is no other database backend. It never stores
canon content.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
            CREATE TABLE IF NOT EXISTS dev_tokens (
                id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
                tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, role TEXT NOT NULL,
                issued_at TEXT NOT NULL, expires_at TEXT NOT NULL, issued_by TEXT NOT NULL,
                revoked_at TEXT, revoked_by TEXT);
            CREATE INDEX IF NOT EXISTS idx_dev_tokens_tenant
                ON dev_tokens(tenant_id, issued_at);
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

    def upsert_user(self, user: User) -> User:
        """Create a platform user or refresh the mutable contact address for that id."""
        self.conn.execute(
            "INSERT INTO users (id, email, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET email = excluded.email",
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

    def list_memberships(self, tenant_id: str) -> list[dict[str, str]]:
        rows = self.conn.execute(
            "SELECT m.tenant_id, m.user_id, u.email, m.role "
            "FROM memberships m JOIN users u ON u.id = m.user_id "
            "WHERE m.tenant_id = ? ORDER BY m.user_id",
            (tenant_id,),
        ).fetchall()
        return [
            {
                "tenant_id": str(row["tenant_id"]),
                "user_id": str(row["user_id"]),
                "email": str(row["email"]),
                "role": str(row["role"]),
            }
            for row in rows
        ]

    def remove_membership(self, tenant_id: str, user_id: str) -> bool:
        cursor = self.conn.execute(
            "DELETE FROM memberships WHERE tenant_id = ? AND user_id = ?",
            (tenant_id, user_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    # --- revocable development tokens ---
    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def register_dev_token(
        self,
        *,
        token_id: str,
        token: str,
        tenant_id: str,
        user_id: str,
        role: Role,
        issued_by: str,
        expires_at: str,
    ) -> dict[str, Any]:
        issued_at = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO dev_tokens "
            "(id, token_hash, tenant_id, user_id, role, issued_at, expires_at, issued_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                token_id,
                self._token_hash(token),
                tenant_id,
                user_id,
                role.value,
                issued_at,
                expires_at,
                issued_by,
            ),
        )
        self.conn.commit()
        return {
            "id": token_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "role": role.value,
            "issued_at": issued_at,
            "expires_at": expires_at,
            "issued_by": issued_by,
            "revoked_at": None,
            "revoked_by": None,
        }

    def list_dev_tokens(self, tenant_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, tenant_id, user_id, role, issued_at, expires_at, issued_by, "
            "revoked_at, revoked_by FROM dev_tokens WHERE tenant_id = ? "
            "ORDER BY issued_at DESC, id DESC",
            (tenant_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def revoke_dev_token(self, tenant_id: str, token_id: str, *, revoked_by: str) -> bool:
        revoked_at = datetime.now(UTC).isoformat()
        cursor = self.conn.execute(
            "UPDATE dev_tokens SET revoked_at = ?, revoked_by = ? "
            "WHERE tenant_id = ? AND id = ? AND revoked_at IS NULL",
            (revoked_at, revoked_by, tenant_id, token_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def token_is_revoked(self, token: str) -> bool:
        row = self.conn.execute(
            "SELECT revoked_at FROM dev_tokens WHERE token_hash = ?",
            (self._token_hash(token),),
        ).fetchone()
        return row is not None and row["revoked_at"] is not None

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
