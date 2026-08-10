"""Fail-closed current team schema registry and tenant-owned migration linter.

The SQL lint entry point is intentionally usable without SQLAlchemy/Alembic so a future migration
can be checked from ``alembic upgrade ... --sql`` output in a minimal CI job.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Any

EXPECTED_HEAD = "n21_0001"
EXPECTED_IMAGE = (
    "postgres:17.10-bookworm@"
    "sha256:4f736ae292687621d4dbe0d499ffd024a36bd2ee7d8ca6f2ccd4c800f047b394"
)
EXPECTED_CONTRACTS = {
    "ArtifactIdentity",
    "OwnershipScope",
    "ContentObjectVersion",
    "RevisionMembership",
    "ContentRevision",
    "ReleasePointer",
    "SourceSegment",
    "IntakeRun",
    "TriageVerdict",
}
FORBIDDEN_RUNTIME_GRANTS = {"CREATE", "TRUNCATE", "REFERENCES"}
REQUIRED_ROLE_ATTRIBUTES = {
    "LOGIN",
    "NOINHERIT",
    "NOSUPERUSER",
    "NOCREATEDB",
    "NOCREATEROLE",
    "NOREPLICATION",
    "NOBYPASSRLS",
}


def load_registry(path: Path) -> dict[str, Any]:
    """Load a JSON object, rejecting duplicate keys and non-object roots."""

    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate)
    if not isinstance(value, dict):
        raise ValueError("schema registry root must be an object")
    return value


def validate_registry(registry: dict[str, Any]) -> list[str]:
    """Validate the frozen role, scope, table, and claim boundaries."""

    errors: list[str] = []
    if registry.get("schema_version") != 3 or registry.get("node") != "N2.1":
        errors.append("registry must identify the N2.1 schema version 3 boundary")
    database = registry.get("database")
    if not isinstance(database, dict):
        return ["registry.database must be an object"]
    if database.get("engine") != "PostgreSQL" or database.get("version") != "17.10":
        errors.append("database must be PostgreSQL 17.10")
    if database.get("driver") != "postgresql+psycopg":
        errors.append("database driver must be postgresql+psycopg")
    if database.get("alembic_head") != EXPECTED_HEAD:
        errors.append(f"alembic head must be {EXPECTED_HEAD}")
    if database.get("image") != EXPECTED_IMAGE:
        errors.append("PostgreSQL image must be pinned to the verified RepoDigest")
    if database.get("pgvector_claim") != "NOT_IMPLEMENTED":
        errors.append("the current team registry must not claim pgvector implementation")

    roles = registry.get("roles")
    if not isinstance(roles, dict):
        errors.append("registry.roles must be an object")
    else:
        expected_membership_edges = {
            "member_column": "FORBIDDEN",
            "roleid_column": "FORBIDDEN",
        }
        for key, expected_name in (
            ("migration_owner", "owcopilot_migration"),
            ("runtime", "owcopilot_app"),
        ):
            role = roles.get(key)
            if not isinstance(role, dict):
                errors.append(f"roles.{key} must be an object")
                continue
            if role.get("name") != expected_name:
                errors.append(f"roles.{key}.name must be {expected_name}")
            attributes = role.get("required_attributes")
            if not isinstance(attributes, list) or set(attributes) != REQUIRED_ROLE_ATTRIBUTES:
                errors.append(f"roles.{key} must freeze the non-elevated role attributes")
            if role.get("pg_auth_members_edges") != expected_membership_edges:
                errors.append(f"roles.{key} must forbid pg_auth_members member and roleid edges")
        runtime = roles.get("runtime")
        if isinstance(runtime, dict):
            forbidden = runtime.get("forbidden_capabilities")
            required_forbidden = {
                "OWNER",
                "CREATE",
                "TRUNCATE",
                "REFERENCES",
                "SET ROLE owcopilot_migration",
            }
            if not isinstance(forbidden, list) or set(forbidden) != required_forbidden:
                errors.append("runtime role forbidden capabilities are incomplete")
        owner = roles.get("migration_owner")
        if not isinstance(owner, dict) or owner.get("force_rls_policy") != (
            "USING (true) WITH CHECK (true)"
        ):
            errors.append("FORCE RLS requires the explicit migration-owner policy")

    context = registry.get("ownership_context")
    if not isinstance(context, dict):
        errors.append("registry.ownership_context must be an object")
    else:
        expected_context = {
            "transaction_local_only": True,
            "transaction_timezone": "UTC",
            "tenant_guc": "owcopilot.tenant_id",
            "container_guc": "owcopilot.container_id",
            "authentication_source": "memberships",
            "guc_is_authentication": False,
        }
        for key, value in expected_context.items():
            if context.get(key) != value:
                errors.append(f"ownership_context.{key} must be {value!r}")

    contract_consumption = registry.get("contract_consumption")
    if (
        not isinstance(contract_consumption, list)
        or set(contract_consumption) != EXPECTED_CONTRACTS
    ):
        errors.append("registry contract consumption differs from the current N2.1 boundary")

    global_tables = registry.get("global_control_tables")
    expected_global = [
        {
            "name": "alembic_version",
            "owner": "owcopilot_migration",
            "tenant_data": False,
            "rls_exemption": "global schema-head probe only",
            "runtime_grants": ["SELECT"],
        }
    ]
    if global_tables != expected_global:
        errors.append("alembic_version must be the sole read-only global control-table exception")

    tables = registry.get("tables")
    if not isinstance(tables, list) or not tables:
        return [*errors, "registry.tables must be a non-empty array"]
    seen: set[str] = set()
    for index, item in enumerate(tables):
        prefix = f"tables[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            continue
        name = item.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            errors.append(f"{prefix}.name must be a bare lowercase SQL identifier")
            continue
        if name in seen:
            errors.append(f"duplicate registered table: {name}")
        seen.add(name)
        scope = item.get("ownership_scope")
        expected_columns = (
            ["tenant_id"]
            if scope == "tenant_only"
            else [
                "tenant_id",
                "container_id",
            ]
        )
        if scope not in {"tenant_only", "tenant_container"}:
            errors.append(f"{name}: ownership_scope is invalid")
        if item.get("scope_columns") != expected_columns:
            errors.append(f"{name}: scope_columns do not match ownership_scope")
        if item.get("force_rls") is not True:
            errors.append(f"{name}: FORCE RLS is mandatory")
        if item.get("runtime_policy") != f"{name}_ownership_scope":
            errors.append(f"{name}: runtime policy name is not deterministic")
        if item.get("migration_owner_policy") != f"{name}_migration_owner":
            errors.append(f"{name}: migration-owner policy is missing")
        grants = item.get("runtime_grants")
        if not isinstance(grants, list) or not grants:
            errors.append(f"{name}: runtime_grants must be explicit")
        else:
            grant_set = set(grants)
            if grant_set & FORBIDDEN_RUNTIME_GRANTS or "ALL" in grant_set:
                errors.append(f"{name}: runtime grants include a forbidden privilege")
            if item.get("immutable") is True and grant_set & {"UPDATE", "DELETE"}:
                errors.append(f"{name}: immutable table grants mutation privileges")
        for test_key in ("positive_isolation_test", "negative_isolation_test"):
            value = item.get(test_key)
            if not isinstance(value, str) or "::test_" not in value:
                errors.append(f"{name}: {test_key} must identify an executable test")
        expected_state_machine = {
            "source_artifacts": "active_to_logically_deleted_only",
            "intake_runs": (
                "uploaded_or_processing_failed_to_segmented_or_pdf_handoff_then_"
                "awaiting_human_or_rejected"
            ),
        }.get(name)
        if expected_state_machine is not None and item.get("state_machine") != (
            expected_state_machine
        ):
            errors.append(f"{name}: state_machine boundary is not frozen")
    if len(seen) != 20:
        errors.append(f"N2.1 registry must contain exactly 20 tables, found {len(seen)}")
    return errors


def _table_blocks(sql: str) -> dict[str, str]:
    pattern = re.compile(
        r"create\s+table\s+(?:if\s+not\s+exists\s+)?([a-z][a-z0-9_]*)\s*"
        r"\((.*?)\)\s*;",
        flags=re.IGNORECASE | re.DOTALL,
    )
    return {match.group(1).lower(): match.group(2) for match in pattern.finditer(sql)}


def _create_table_count(sql: str) -> int:
    """Count CREATE TABLE tokens so unsupported identifier syntax cannot evade lint."""

    without_block_comments = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    without_line_comments = re.sub(r"--[^\r\n]*", "", without_block_comments)
    return len(
        re.findall(
            r"\bcreate\s+(?:temporary\s+|temp\s+)?table\b",
            without_line_comments,
            flags=re.IGNORECASE,
        )
    )


def _statement_containing(sql: str, start: str, table: str, end: str) -> str | None:
    pattern = re.compile(
        rf"{start}\b[^;]*\b{re.escape(table)}\b[^;]*{end}\s*;",
        flags=re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(sql)
    return match.group(0) if match else None


def _function_body(sql: str, function_name: str) -> str | None:
    """Return one PL/pgSQL function body without trusting tokens elsewhere in the migration."""

    pattern = re.compile(
        rf"create\s+function\s+{re.escape(function_name)}\s*\([^)]*\).*?"
        r"as\s+\$(?P<tag>[a-z0-9_]+)\$(?P<body>.*?)\$(?P=tag)\$\s*;",
        flags=re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(sql)
    return match.group("body").lower() if match else None


class _StaticMigrationError(ValueError):
    """A migration upgrade cannot be rendered by the deliberately small safe interpreter."""


def _static_value(node: ast.expr, environment: dict[str, Any]) -> Any:
    """Evaluate data-only AST nodes without importing or executing migration code."""

    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in environment:
            raise _StaticMigrationError(f"unknown static name {node.id!r}")
        return environment[node.id]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        values = [_static_value(item, environment) for item in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(values)
        if isinstance(node, ast.Set):
            return set(values)
        return values
    if isinstance(node, ast.Dict):
        if any(key is None for key in node.keys):
            raise _StaticMigrationError("dictionary unpacking is not statically supported")
        return {
            _static_value(key, environment): _static_value(value, environment)
            for key, value in zip(node.keys, node.values, strict=True)
            if key is not None
        }
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _static_value(node.left, environment) + _static_value(node.right, environment)
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for item in node.values:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                parts.append(item.value)
                continue
            if not isinstance(item, ast.FormattedValue):
                raise _StaticMigrationError("unsupported f-string component")
            value = _static_value(item.value, environment)
            if item.conversion == ord("r"):
                value = repr(value)
            elif item.conversion == ord("s"):
                value = str(value)
            elif item.conversion == ord("a"):
                value = ascii(value)
            elif item.conversion != -1:
                raise _StaticMigrationError("unsupported f-string conversion")
            format_spec = (
                ""
                if item.format_spec is None
                else str(_static_value(item.format_spec, environment))
            )
            parts.append(format(value, format_spec))
        return "".join(parts)
    raise _StaticMigrationError(
        f"unsupported static expression {type(node).__name__} on line {node.lineno}"
    )


def _bind_static_target(target: ast.expr, value: Any, environment: dict[str, Any]) -> None:
    if isinstance(target, ast.Name):
        environment[target.id] = value
        return
    if isinstance(target, (ast.Tuple, ast.List)):
        try:
            values = list(value)
        except TypeError as exc:
            raise _StaticMigrationError("cannot unpack a non-iterable static value") from exc
        if len(target.elts) != len(values):
            raise _StaticMigrationError("static loop unpacking cardinality mismatch")
        for child, child_value in zip(target.elts, values, strict=True):
            _bind_static_target(child, child_value, environment)
        return
    raise _StaticMigrationError(
        f"unsupported assignment target {type(target).__name__} on line {target.lineno}"
    )


def _static_upgrade_sql(path: Path) -> tuple[str, str, str]:
    """Render one migration's trusted subset without importing the Python module.

    Supported constructs are intentionally limited to module constants, no-argument local
    helpers, assignments, static ``for`` loops/f-strings, and ``op.execute``. Any other upgrade
    operation fails closed so a future migration cannot silently escape schema lint.
    """

    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise _StaticMigrationError(f"cannot parse migration: {exc}") from exc

    module_environment: dict[str, Any] = {}
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions[node.name] = node
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            try:
                value = _static_value(node.value, module_environment)
                _bind_static_target(node.targets[0], value, module_environment)
            except _StaticMigrationError:
                # Imports and executable module setup are never evaluated. Only constants needed by
                # upgrade will later be required through the fail-closed expression evaluator.
                continue

    revision = module_environment.get("revision")
    if type(revision) is not str or not revision:
        raise _StaticMigrationError("migration revision must be a static non-empty string")
    upgrade = functions.get("upgrade")
    if not isinstance(upgrade, ast.FunctionDef) or upgrade.args.args:
        raise _StaticMigrationError("migration must define a synchronous zero-argument upgrade")

    statements: list[str] = []

    def execute_body(
        body: list[ast.stmt],
        environment: dict[str, Any],
        active_helpers: set[str],
    ) -> None:
        for statement in body:
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
                if isinstance(statement.value.value, str):
                    continue  # function docstring
            if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
                value = _static_value(statement.value, environment)
                _bind_static_target(statement.targets[0], value, environment)
                continue
            if isinstance(statement, ast.For):
                if statement.orelse:
                    raise _StaticMigrationError("static migration loops cannot have else clauses")
                iterable = _static_value(statement.iter, environment)
                try:
                    values = list(iterable)
                except TypeError as exc:
                    raise _StaticMigrationError("static migration loop is not iterable") from exc
                for value in values:
                    _bind_static_target(statement.target, value, environment)
                    execute_body(statement.body, environment, active_helpers)
                continue
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
                call = statement.value
                function = call.func
                if (
                    isinstance(function, ast.Attribute)
                    and isinstance(function.value, ast.Name)
                    and function.value.id == "op"
                ):
                    if function.attr != "execute" or len(call.args) != 1 or call.keywords:
                        raise _StaticMigrationError(
                            f"unsupported Alembic op.{function.attr} on line {statement.lineno}; "
                            "use statically renderable op.execute or extend the linter first"
                        )
                    rendered = _static_value(call.args[0], environment)
                    if type(rendered) is not str:
                        raise _StaticMigrationError(
                            f"op.execute argument on line {statement.lineno} is not static SQL"
                        )
                    statements.append(rendered)
                    continue
                if (
                    isinstance(function, ast.Name)
                    and not call.args
                    and not call.keywords
                    and function.id in functions
                ):
                    helper = functions[function.id]
                    if not isinstance(helper, ast.FunctionDef) or helper.args.args:
                        raise _StaticMigrationError(
                            f"upgrade helper {function.id!r} must be synchronous and zero-argument"
                        )
                    if function.id in active_helpers:
                        raise _StaticMigrationError(
                            f"recursive migration helper {function.id!r} is forbidden"
                        )
                    execute_body(
                        helper.body,
                        dict(environment),
                        {*active_helpers, function.id},
                    )
                    continue
            if isinstance(statement, ast.Pass):
                continue
            raise _StaticMigrationError(
                f"unsupported upgrade statement {type(statement).__name__} "
                f"on line {statement.lineno}"
            )

    execute_body(upgrade.body, dict(module_environment), {"upgrade"})
    rendered_sql = ";\n".join(statements) + (";\n" if statements else "")
    return revision, rendered_sql, source


def lint_sql(sql: str, registry: dict[str, Any] | None = None) -> list[str]:
    """Lint rendered PostgreSQL migration SQL or a future migration fixture."""

    errors: list[str] = []
    registered = {
        str(item["name"]): item
        for item in (registry or {}).get("tables", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    blocks = _table_blocks(sql)
    create_table_count = _create_table_count(sql)
    if create_table_count != len(blocks):
        errors.append(
            "migration contains CREATE TABLE syntax the fail-closed linter cannot parse "
            f"({create_table_count} statements, {len(blocks)} parsed)"
        )
    for table, raw_block in blocks.items():
        block = re.sub(r"\s+", " ", raw_block.lower())
        item = registered.get(table)
        if item is None:
            qualifier = "tenant-owned " if re.search(r"\btenant_id\b", block) else ""
            errors.append(f"{table}: unregistered {qualifier}table")
        if not re.search(r"\btenant_id\b", block):
            if item is not None:
                errors.append(f"{table}: registered table lacks tenant_id OwnershipScope")
            continue
        tenant_only = item is not None and item.get("ownership_scope") == "tenant_only"
        if not tenant_only and not re.search(r"\bcontainer_id\b", block):
            errors.append(f"{table}: tenant-owned table lacks container_id OwnershipScope")
        if table != "containers" and not tenant_only:
            if not re.search(r"foreign\s+key\s*\(\s*tenant_id\s*,\s*container_id\s*\)", block):
                errors.append(f"{table}: missing composite OwnershipScope FK")

        for verb in ("enable", "force"):
            if not re.search(
                rf"alter\s+table\s+{re.escape(table)}\s+{verb}\s+row\s+level\s+security",
                sql,
                flags=re.IGNORECASE,
            ):
                errors.append(f"{table}: missing {verb.upper()} ROW LEVEL SECURITY")

        runtime_policy = _statement_containing(
            sql,
            rf"create\s+policy\s+{re.escape(table)}_ownership_scope\s+on",
            table,
            r"with\s+check\s*\([^;]+\)",
        )
        if runtime_policy is None:
            errors.append(f"{table}: missing runtime USING/WITH CHECK policy")
        else:
            lowered = runtime_policy.lower()
            for token in (
                "to owcopilot_app",
                "using (",
                "with check (",
                "current_setting('owcopilot.tenant_id', true)",
            ):
                if token not in lowered:
                    errors.append(f"{table}: runtime policy lacks {token}")
            if not tenant_only and "current_setting('owcopilot.container_id', true)" not in lowered:
                errors.append(f"{table}: runtime policy lacks container GUC")

        owner_policy = _statement_containing(
            sql,
            rf"create\s+policy\s+{re.escape(table)}_migration_owner\s+on",
            table,
            r"with\s+check\s*\(\s*true\s*\)",
        )
        if owner_policy is None or "to owcopilot_migration" not in owner_policy.lower():
            errors.append(f"{table}: missing FORCE-RLS migration-owner policy")

        if (
            _statement_containing(sql, r"revoke\s+all\s+on\s+table", table, r"from\s+public")
            is None
        ):
            errors.append(f"{table}: missing REVOKE ALL FROM PUBLIC")
        if (
            _statement_containing(sql, r"revoke\s+all\s+on\s+table", table, r"from\s+owcopilot_app")
            is None
        ):
            errors.append(f"{table}: runtime grants were not reset before explicit GRANT")
        grants = re.findall(
            rf"grant\s+([^;]+?)\s+on\s+table\s+([^;]*\b{re.escape(table)}\b[^;]*)"
            r"\s+to\s+owcopilot_app\s*;",
            sql,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not grants:
            errors.append(f"{table}: no explicit runtime GRANT")
        for privilege_text, _ in grants:
            privileges = {part.strip().upper() for part in privilege_text.split(",")}
            forbidden = privileges & (FORBIDDEN_RUNTIME_GRANTS | {"ALL", "ALL PRIVILEGES"})
            if forbidden:
                errors.append(f"{table}: forbidden runtime GRANT {sorted(forbidden)!r}")

        if item is not None and item.get("immutable") is True:
            immutable_trigger = re.search(
                rf"create\s+trigger\s+{re.escape(table)}_immutable\s+"
                rf"before\s+update\s+or\s+delete\s+on\s+{re.escape(table)}\s+"
                r"for\s+each\s+row\s+execute\s+function\s+"
                r"owcopilot_reject_immutable_mutation\s*\(\s*\)\s*;",
                sql,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if immutable_trigger is None:
                errors.append(f"{table}: missing UPDATE/DELETE immutable trigger")

        lowered_sql = sql.lower()
        normalized_sql = re.sub(r"\s+", " ", lowered_sql)
        if table == "source_artifacts":
            source_trigger = re.search(
                r"create\s+trigger\s+source_artifacts_logical_delete_guard\s+"
                r"before\s+insert\s+or\s+update\s+or\s+delete\s+on\s+source_artifacts\s+"
                r"for\s+each\s+row\s+execute\s+function\s+"
                r"owcopilot_guard_source_artifact_mutation\s*\(\s*\)\s*;",
                sql,
                flags=re.IGNORECASE | re.DOTALL,
            )
            required_source_tokens = (
                "source_artifacts must start active",
                "source_artifacts permits logical delete only",
                "source_artifacts identity and audit fields are frozen",
                "source_artifacts logical delete is one-way null to timestamp",
            )
            if source_trigger is None or any(
                token not in normalized_sql for token in required_source_tokens
            ):
                errors.append("source_artifacts: logical-delete-only state guard is incomplete")
        if table == "intake_runs":
            intake_trigger = re.search(
                r"create\s+trigger\s+intake_runs_state_guard\s+"
                r"before\s+insert\s+or\s+update\s+or\s+delete\s+on\s+intake_runs\s+"
                r"for\s+each\s+row\s+execute\s+function\s+"
                r"owcopilot_guard_intake_run_mutation\s*\(\s*\)\s*;",
                sql,
                flags=re.IGNORECASE | re.DOTALL,
            )
            required_intake_tokens = (
                "new.status <> 'uploaded'",
                "old.status = 'uploaded' and new.status in ( 'segmented', 'ocr_required', "
                "'processing_failed', 'rejected' )",
                "old.status = 'processing_failed' and new.status in ( 'segmented', "
                "'ocr_required', 'processing_failed' )",
                "old.status = 'segmented'",
                "new.status in ('awaiting_human', 'rejected')",
                "pdf handoff states require a segment-free pdf artifact version",
                "processing_code = 'pdf_ocr_required'",
                "status = 'processing_failed'",
                "intake_runs identity and audit fields are frozen",
                "intake_runs cannot be deleted",
            )
            if intake_trigger is None or any(
                token not in normalized_sql for token in required_intake_tokens
            ):
                errors.append("intake_runs: transition and frozen-identity guard is incomplete")
        if table == "artifact_versions":
            if "content_hash = encode(sha256(content_bytes), 'hex')" not in lowered_sql:
                errors.append("artifact_versions: exact byte hash binding is missing")
            if "extension = '.pdf' and mime_type = 'application/pdf'" not in normalized_sql:
                errors.append("artifact_versions: born-digital PDF format pair is missing")
            version_guard = _function_body(sql, "owcopilot_guard_artifact_version_insert")
            version_trigger = re.search(
                r"create\s+trigger\s+artifact_versions_active_artifact_guard\s+"
                r"before\s+insert\s+on\s+artifact_versions\s+for\s+each\s+row\s+"
                r"execute\s+function\s+owcopilot_guard_artifact_version_insert\s*\(\s*\)\s*;",
                sql,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if (
                version_trigger is None
                or version_guard is None
                or "artifact_versions requires an active source artifact" not in version_guard
                or "logical_deleted_at" not in version_guard
                or "for key share" not in version_guard
            ):
                errors.append("artifact_versions: active source-artifact guard is incomplete")
        if table == "source_segments":
            if (
                "owcopilot_valid_source_locator(locator_kind, locator_jsonb)" not in lowered_sql
                or "not locator ?& array['kind', 'paragraph_index']" not in lowered_sql
                or "key_count <> 5" not in lowered_sql
                or "expected_kind = 'pdf_text_line'" not in lowered_sql
                or "key_count <> 15" not in lowered_sql
                or "pdf_points_top_left_display" not in lowered_sql
                or "born_digital_text_line_baseline" not in lowered_sql
            ):
                errors.append("source_segments: exact fail-closed locator guard is incomplete")
            if (
                "text_hash = encode(sha256(convert_to(text_content, 'utf8')), 'hex')"
                not in lowered_sql
            ):
                errors.append("source_segments: exact UTF-8 text hash binding is missing")
            segment_guard = _function_body(sql, "owcopilot_guard_source_segment_insert")
            segment_trigger = re.search(
                r"create\s+trigger\s+source_segments_stage_guard\s+before\s+insert\s+on\s+"
                r"source_segments\s+for\s+each\s+row\s+execute\s+function\s+"
                r"owcopilot_guard_source_segment_insert\s*\(\s*\)\s*;",
                sql,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if (
                segment_trigger is None
                or segment_guard is None
                or "run_status not in ('uploaded', 'processing_failed')" not in segment_guard
                or "source_segments requires an active source artifact" not in segment_guard
                or "source locator kind does not match artifact format" not in segment_guard
            ):
                errors.append("source_segments: retryable-stage insert guard is incomplete")
        if table == "raw_triage_outputs":
            if "output_hash = encode(sha256(convert_to(output_text, 'utf8')), 'hex')" not in (
                lowered_sql
            ):
                errors.append("raw_triage_outputs: exact UTF-8 output hash binding is missing")
            if re.search(r"output_text\s*<>\s*''", lowered_sql):
                errors.append("raw_triage_outputs: empty raw model output must remain persistable")
            raw_guard = _function_body(sql, "owcopilot_guard_raw_triage_output_insert")
            raw_trigger = re.search(
                r"create\s+trigger\s+raw_triage_outputs_stage_guard\s+before\s+insert\s+on\s+"
                r"raw_triage_outputs\s+for\s+each\s+row\s+execute\s+function\s+"
                r"owcopilot_guard_raw_triage_output_insert\s*\(\s*\)\s*;",
                sql,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if (
                raw_trigger is None
                or raw_guard is None
                or "run_status <> 'segmented'" not in raw_guard
                or "raw_triage_outputs requires an active source artifact" not in raw_guard
            ):
                errors.append("raw_triage_outputs: SEGMENTED-stage insert guard is incomplete")
        if table == "triage_verdicts":
            verdict_trigger = re.search(
                r"create\s+trigger\s+triage_verdicts_raw_guard\s+before\s+insert\s+on\s+"
                r"triage_verdicts\s+for\s+each\s+row\s+execute\s+function\s+"
                r"owcopilot_guard_triage_verdict\s*\(\s*\)\s*;",
                sql,
                flags=re.IGNORECASE | re.DOTALL,
            )
            verdict_guard = _function_body(sql, "owcopilot_guard_triage_verdict")
            if verdict_trigger is None or verdict_guard is None or (
                "empty or invalid raw triage output requires unknown abstention"
                not in verdict_guard
                or "jsonb_typeof(parsed_raw) <> 'object'" not in verdict_guard
                or "run_status <> 'segmented'" not in verdict_guard
                or "end_offset > char_length(segment_text)" not in verdict_guard
                or "triage verdict evidence must exactly match immutable segment text"
                not in verdict_guard
            ):
                errors.append("triage_verdicts: invalid-raw UNKNOWN abstention guard is incomplete")
        if table == "human_triage_decisions":
            decision_guard = _function_body(sql, "owcopilot_guard_human_triage_decision")
            decision_trigger = re.search(
                r"create\s+trigger\s+human_triage_decisions_submission_guard\s+before\s+"
                r"insert\s+on\s+human_triage_decisions\s+for\s+each\s+row\s+execute\s+"
                r"function\s+owcopilot_guard_human_triage_decision\s*\(\s*\)\s*;",
                sql,
                flags=re.IGNORECASE | re.DOTALL,
            )
            coverage_trigger = re.search(
                r"create\s+constraint\s+trigger\s+human_triage_decisions_complete\s+after\s+"
                r"insert\s+on\s+human_triage_decisions\s+deferrable\s+initially\s+deferred\s+"
                r"for\s+each\s+row\s+execute\s+function\s+"
                r"owcopilot_assert_human_decision_coverage\s*\(\s*\)\s*;",
                sql,
                flags=re.IGNORECASE | re.DOTALL,
            )
            coverage_guard = _function_body(sql, "owcopilot_assert_human_decision_coverage")
            if (
                decision_trigger is None
                or coverage_trigger is None
                or decision_guard is None
                or coverage_guard is None
                or "run_status <> 'awaiting_human'" not in decision_guard
                or "new.decision = 'confirm'" not in decision_guard
                or "complete five-kind rights declaration" not in decision_guard
                or "decision_count <> segment_count" not in coverage_guard
            ):
                errors.append(
                    "human_triage_decisions: stage, rights, role, or deferred "
                    "coverage guard is incomplete"
                )
        if table == "rights_declarations":
            rights_guard = _function_body(sql, "owcopilot_guard_rights_declaration")
            rights_trigger = re.search(
                r"create\s+trigger\s+rights_declarations_active_guard\s+before\s+insert\s+on\s+"
                r"rights_declarations\s+for\s+each\s+row\s+execute\s+function\s+"
                r"owcopilot_guard_rights_declaration\s*\(\s*\)\s*;",
                sql,
                flags=re.IGNORECASE | re.DOTALL,
            )
            complete_trigger = re.search(
                r"create\s+constraint\s+trigger\s+rights_declarations_complete_uses\s+after\s+"
                r"insert\s+on\s+rights_declarations\s+deferrable\s+initially\s+deferred\s+"
                r"for\s+each\s+row\s+execute\s+function\s+"
                r"owcopilot_assert_allowed_use_completeness\s*\(\s*\)\s*;",
                sql,
                flags=re.IGNORECASE | re.DOTALL,
            )
            complete_guard = _function_body(sql, "owcopilot_assert_allowed_use_completeness")
            if (
                rights_trigger is None
                or complete_trigger is None
                or rights_guard is None
                or complete_guard is None
                or "requires an active source artifact" not in rights_guard
                or "use_count <> 5" not in complete_guard
                or "matching_actor_count <> 5" not in complete_guard
            ):
                errors.append(
                    "rights_declarations: active-artifact or deferred five-use guard is incomplete"
                )
        if table == "allowed_uses":
            allowed_guard = _function_body(sql, "owcopilot_guard_allowed_use")
            allowed_trigger = re.search(
                r"create\s+trigger\s+allowed_uses_actor_guard\s+before\s+insert\s+on\s+"
                r"allowed_uses\s+for\s+each\s+row\s+execute\s+function\s+"
                r"owcopilot_guard_allowed_use\s*\(\s*\)\s*;",
                sql,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if (
                allowed_trigger is None
                or allowed_guard is None
                or "allowed_uses.declared_by must match rights declaration actor"
                not in allowed_guard
            ):
                errors.append("allowed_uses: human declarer binding guard is incomplete")
        if table == "lineage_anchors":
            lineage_guard = _function_body(sql, "owcopilot_guard_lineage_anchor")
            lineage_trigger = re.search(
                r"create\s+trigger\s+lineage_anchors_governance_guard\s+before\s+insert\s+on\s+"
                r"lineage_anchors\s+for\s+each\s+row\s+execute\s+function\s+"
                r"owcopilot_guard_lineage_anchor\s*\(\s*\)\s*;",
                sql,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if (
                lineage_trigger is None
                or lineage_guard is None
                or "lineage requires an active source artifact" not in lineage_guard
                or "lineage requires complete human decisions for every segment"
                not in lineage_guard
                or "source_evidence requires a final human canon_candidate decision"
                not in lineage_guard
                or "inspiration_lineage requires a final human reference or mixed decision"
                not in lineage_guard
            ):
                errors.append(
                    "lineage_anchors: active, complete, or final-role guard is incomplete"
                )

        if item is None:
            positive = f"-- owcopilot: isolation-positive {table}"
            negative = f"-- owcopilot: isolation-negative {table}"
            if positive not in sql.lower():
                errors.append(f"{table}: missing positive isolation-test annotation")
            if negative not in sql.lower():
                errors.append(f"{table}: missing negative isolation-test annotation")
    return errors


def validate_repository_sources(root: Path, registry: dict[str, Any]) -> list[str]:
    """Bind the registry to migration/role source instead of trusting JSON alone."""

    errors: list[str] = []
    test_functions: dict[Path, set[str] | None] = {}

    def validate_test_nodeid(table: str, key: str, value: object) -> None:
        if not isinstance(value, str):
            # ``validate_registry`` owns the shape error.  Avoid a duplicate,
            # less useful diagnostic here.
            return
        parts = value.split("::")
        if len(parts) != 2:
            errors.append(f"{table}: {key} must name one top-level test function")
            return
        raw_path, function_name = parts
        if (
            "\\" in raw_path
            or not re.fullmatch(r"tests/[A-Za-z0-9_./-]+\.py", raw_path)
            or any(part in {"", ".", ".."} for part in raw_path.split("/"))
        ):
            errors.append(f"{table}: {key} must use a safe repo-relative tests/*.py path")
            return
        if re.fullmatch(r"test_[A-Za-z0-9_]+", function_name) is None:
            errors.append(f"{table}: {key} must name one top-level test function")
            return

        tests_root = (root / "tests").resolve()
        path = (root / Path(*raw_path.split("/"))).resolve()
        try:
            path.relative_to(tests_root)
        except ValueError:
            errors.append(f"{table}: {key} escapes the repository tests directory")
            return
        if not path.is_file():
            errors.append(f"{table}: {key} test file does not exist: {raw_path}")
            return

        if path not in test_functions:
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (OSError, SyntaxError, UnicodeError) as exc:
                errors.append(f"{table}: cannot parse isolation test file {raw_path}: {exc}")
                test_functions[path] = None
                return
            test_functions[path] = {
                node.name
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
        available = test_functions[path]
        if available is not None and function_name not in available:
            errors.append(f"{table}: {key} top-level test function does not exist: {value}")

    versions_root = root / "infra/migrations/versions"
    migration_paths = sorted(versions_root.glob("*.py")) if versions_root.is_dir() else []
    if not migration_paths:
        errors.append(f"no migration sources discovered under {versions_root}")
    loaded: dict[str, str] = {}
    rendered_sql: dict[str, str] = {}
    for path in migration_paths:
        try:
            revision, sql, source = _static_upgrade_sql(path)
        except (OSError, UnicodeError, _StaticMigrationError) as exc:
            errors.append(f"{path}: cannot statically collect upgrade SQL: {exc}")
            continue
        if revision in loaded:
            errors.append(f"duplicate migration revision {revision!r}: {path}")
            continue
        loaded[revision] = source
        rendered_sql[revision] = sql
        errors.extend(f"{path}: {item}" for item in lint_sql(sql, registry))
    for item in registry.get("tables", []):
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        table_revision = item.get("migration")
        source = rendered_sql.get(str(table_revision), "")
        if isinstance(name, str) and name not in _table_blocks(source):
            errors.append(f"{name}: not created by registered migration {table_revision}")
        if isinstance(name, str):
            for key in ("positive_isolation_test", "negative_isolation_test"):
                validate_test_nodeid(name, key, item.get(key))

    role_path = root / "infra/team/postgres/initdb/001_roles.sh"
    if not role_path.is_file():
        errors.append(f"missing PostgreSQL role bootstrap: {role_path}")
    else:
        role_source = role_path.read_text(encoding="utf-8")
        for role in ("owcopilot_migration", "owcopilot_app"):
            if f"CREATE ROLE {role}" not in role_source or f"ALTER ROLE {role}" not in role_source:
                errors.append(f"role bootstrap does not create and reassert {role}")
        for attribute in REQUIRED_ROLE_ATTRIBUTES:
            if attribute not in role_source:
                errors.append(f"role bootstrap lacks {attribute}")
        for token in (
            "REVOKE owcopilot_migration FROM owcopilot_app",
            "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
            "REVOKE ALL ON SCHEMA public FROM owcopilot_app",
        ):
            if token not in role_source:
                errors.append(f"role bootstrap lacks boundary: {token}")
    first_migration = loaded.get("n12_0001", "")
    for token in (
        "REVOKE ALL ON TABLE alembic_version FROM PUBLIC",
        "REVOKE ALL ON TABLE alembic_version FROM owcopilot_app",
        "GRANT SELECT ON TABLE alembic_version TO owcopilot_app",
    ):
        if token not in first_migration:
            errors.append(f"alembic_version probe boundary is missing: {token}")
    return errors


def _default_root() -> Path:
    return Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sql", nargs="*", type=Path, help="rendered migration SQL to lint")
    parser.add_argument(
        "--registry",
        type=Path,
        default=_default_root() / "contracts/n1_2_team_schema_registry.json",
    )
    parser.add_argument("--root", type=Path, default=_default_root())
    args = parser.parse_args(argv)

    try:
        registry = load_registry(args.registry)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"team-schema-lint: FAIL: cannot load registry: {exc}", file=sys.stderr)
        return 2
    errors = [*validate_registry(registry), *validate_repository_sources(args.root, registry)]
    for sql_path in args.sql:
        try:
            sql = sql_path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"{sql_path}: cannot read: {exc}")
            continue
        errors.extend(f"{sql_path}: {item}" for item in lint_sql(sql, registry))
    if errors:
        for error in errors:
            print(f"team-schema-lint: FAIL: {error}", file=sys.stderr)
        return 1
    print(f"team-schema-lint: PASS ({len(registry['tables'])} tables, head={EXPECTED_HEAD})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
