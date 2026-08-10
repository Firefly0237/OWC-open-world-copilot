from __future__ import annotations

import importlib.util
import json
import re
import sys
import tomllib
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from owcopilot.teamdb import schema

ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = ROOT / "contracts/n1_2_team_schema_registry.json"
GOOD_SQL = ROOT / "tests/schema_fixtures/n12_good_migration.sql"
BAD_SQL = ROOT / "tests/schema_fixtures/n12_bad_migration.sql"


def _load_linter() -> ModuleType:
    path = ROOT / "scripts/lint_team_schema.py"
    spec = importlib.util.spec_from_file_location("lint_team_schema", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_migration(name: str, relative_path: str) -> ModuleType:
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _registry_with_fixture(registry: dict[str, Any], name: str) -> dict[str, Any]:
    extended = deepcopy(registry)
    extended["tables"].append(
        {
            "name": name,
            "migration": "fixture",
            "ownership_scope": "tenant_container",
            "scope_columns": ["tenant_id", "container_id"],
            "force_rls": True,
            "runtime_policy": f"{name}_ownership_scope",
            "migration_owner_policy": f"{name}_migration_owner",
            "runtime_grants": ["SELECT", "INSERT", "UPDATE", "DELETE"],
            "immutable": False,
            "positive_isolation_test": (
                "tests/platform/test_team_schema_unit.py::test_linter_accepts_matched_control"
            ),
            "negative_isolation_test": (
                "tests/platform/test_team_schema_unit.py::"
                "test_linter_rejects_missing_scope_force_role_and_negative_case"
            ),
        }
    )
    return extended


class _OperationCollector:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: Any) -> None:
        self.statements.append(str(statement))


def _current_upgrade_sql() -> str:
    migrations = (
        _load_migration(
            "n12_migration_0001", "infra/migrations/versions/n12_0001_control_plane.py"
        ),
        _load_migration("n12_migration_0002", "infra/migrations/versions/n12_0002_canonical.py"),
        _load_migration("n21_migration_0001", "infra/migrations/versions/n21_0001_intake_truth.py"),
    )
    collector = _OperationCollector()
    for migration in migrations:
        cast(Any, migration).op = collector
        migration.upgrade()
    return ";\n".join(collector.statements) + ";\n"


@pytest.fixture(scope="module")
def registry() -> dict[str, Any]:
    observed: object = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    assert isinstance(observed, dict)
    return cast(dict[str, Any], observed)


def test_registry_and_repository_sources_are_closed(registry: dict[str, Any]) -> None:
    linter = _load_linter()
    assert linter.validate_registry(registry) == []
    assert linter.validate_repository_sources(ROOT, registry) == []


@pytest.mark.parametrize("role_key", ["migration_owner", "runtime"])
@pytest.mark.parametrize("edge_column", ["member_column", "roleid_column"])
def test_registry_rejects_role_membership_edges(
    registry: dict[str, Any], role_key: str, edge_column: str
) -> None:
    linter = _load_linter()
    expected_edges = {
        "member_column": "FORBIDDEN",
        "roleid_column": "FORBIDDEN",
    }
    assert registry["roles"][role_key]["pg_auth_members_edges"] == expected_edges
    weakened = deepcopy(registry)
    weakened["roles"][role_key]["pg_auth_members_edges"][edge_column] = "ALLOWED"
    assert (
        f"roles.{role_key} must forbid pg_auth_members member and roleid edges"
        in linter.validate_registry(weakened)
    )


def test_registry_rejects_non_utc_transaction_timezone(registry: dict[str, Any]) -> None:
    linter = _load_linter()
    assert registry["ownership_context"]["transaction_timezone"] == "UTC"
    weakened = deepcopy(registry)
    weakened["ownership_context"]["transaction_timezone"] = "Europe/London"
    assert "ownership_context.transaction_timezone must be 'UTC'" in linter.validate_registry(
        weakened
    )


def test_repository_source_validation_rejects_fabricated_isolation_nodeids(
    registry: dict[str, Any],
) -> None:
    linter = _load_linter()
    fabricated = deepcopy(registry)
    fabricated["tables"][0]["positive_isolation_test"] = (
        "tests/platform/test_team_schema_unit.py::test_isolation_case_that_does_not_exist"
    )
    errors = linter.validate_repository_sources(ROOT, fabricated)
    assert any(
        "positive_isolation_test top-level test function does not exist" in error
        for error in errors
    )


def test_repository_source_validation_discovers_unregistered_future_migration(
    tmp_path: Path,
    registry: dict[str, Any],
) -> None:
    linter = _load_linter()
    for relative in (
        "infra/migrations/versions/n12_0001_control_plane.py",
        "infra/migrations/versions/n12_0002_canonical.py",
        "infra/migrations/versions/n21_0001_intake_truth.py",
        "infra/team/postgres/initdb/001_roles.sh",
        "tests/platform/test_team_pg_integration.py",
        "tests/intake/test_n21_team_pg_integration.py",
    ):
        source = ROOT / relative
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    future = tmp_path / "infra/migrations/versions/n22_0001_unregistered.py"
    future.write_text(
        '''from alembic import op

revision = "n22_0001"
down_revision = "n21_0001"

raise RuntimeError("static schema lint must never import migration modules")

def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE future_scope_leak (
            tenant_id uuid NOT NULL,
            container_id uuid NOT NULL
        );
        """
    )
''',
        encoding="utf-8",
    )

    errors = linter.validate_repository_sources(tmp_path, registry)

    assert any("future_scope_leak: unregistered tenant-owned table" in error for error in errors)
    assert not any("must never import migration modules" in error for error in errors)


def test_metadata_matches_registry_and_compiles_for_postgresql(registry: dict[str, Any]) -> None:
    registered = {item["name"] for item in registry["tables"]}
    assert set(schema.metadata.tables) == registered
    assert schema.EXPECTED_ALEMBIC_HEAD == "n21_0001"
    assert schema.TENANT_SCOPED_TABLES == registered
    assert schema.TENANT_ONLY_TABLES == {"tenants", "memberships"}
    assert schema.IMMUTABLE_TABLES == {
        "content_object_versions",
        "content_revisions",
        "revision_memberships",
        "legacy_archive_registry",
        "operation_events",
        "artifact_versions",
        "source_segments",
        "raw_triage_outputs",
        "triage_verdicts",
        "human_triage_decisions",
        "rights_declarations",
        "allowed_uses",
        "lineage_anchors",
    }
    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    rendered = [
        str(CreateTable(table).compile(dialect=dialect))
        for table in schema.metadata.tables.values()
    ]
    assert len(rendered) == 20
    assert all("tenant_id UUID NOT NULL" in ddl or "tenant_id UUID" in ddl for ddl in rendered)
    object_ddl = next(ddl for ddl in rendered if "CREATE TABLE content_object_versions" in ddl)
    assert "payload_json TEXT NOT NULL" in object_ddl
    assert "payload_jsonb JSONB NOT NULL" in object_ddl
    bootstrap_ddl = next(ddl for ddl in rendered if "CREATE TABLE bootstrap_import_runs" in ddl)
    assert "fk_bootstrap_import_runs_genesis_revision" in bootstrap_ddl
    assert "DEFERRABLE INITIALLY DEFERRED" in bootstrap_ddl


def test_n21_primary_actor_and_version_bindings_are_explicit() -> None:
    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    ddl_by_table = {
        name: str(CreateTable(schema.metadata.tables[name]).compile(dialect=dialect))
        for name in (
            "source_artifacts",
            "artifact_versions",
            "intake_runs",
            "source_segments",
            "raw_triage_outputs",
            "triage_verdicts",
            "human_triage_decisions",
            "rights_declarations",
            "allowed_uses",
            "lineage_anchors",
        )
    }
    required_non_nil_constraints = {
        "source_artifacts": ("artifact_non_nil", "created_by_non_nil"),
        "artifact_versions": ("version_non_nil", "created_by_non_nil"),
        "intake_runs": ("run_non_nil", "created_by_non_nil"),
        "source_segments": ("segment_non_nil",),
        "raw_triage_outputs": ("raw_output_non_nil",),
        "triage_verdicts": ("verdict_non_nil",),
        "human_triage_decisions": ("decision_non_nil", "decided_by_non_nil"),
        "rights_declarations": ("rights_declaration_non_nil", "declared_by_non_nil"),
        "allowed_uses": ("allowed_use_non_nil", "declared_by_non_nil"),
        "lineage_anchors": ("lineage_anchor_non_nil", "created_by_non_nil"),
    }
    for table_name, suffixes in required_non_nil_constraints.items():
        for suffix in suffixes:
            assert f"ck_{table_name}_{suffix}" in ddl_by_table[table_name]

    raw_ddl = ddl_by_table["raw_triage_outputs"]
    artifact_version_ddl = ddl_by_table["artifact_versions"]
    verdict_ddl = ddl_by_table["triage_verdicts"]
    human_ddl = ddl_by_table["human_triage_decisions"]
    assert "FOREIGN KEY(tenant_id, container_id, run_id, artifact_version_id)" in raw_ddl
    assert "FOREIGN KEY(tenant_id, container_id, segment_id, artifact_version_id)" in raw_ddl
    assert (
        "FOREIGN KEY(tenant_id, container_id, raw_output_id, artifact_version_id, segment_id)"
        in verdict_ddl
    )
    assert (
        "FOREIGN KEY(tenant_id, container_id, verdict_id, artifact_version_id, segment_id)"
        in human_ddl
    )
    assert "UNIQUE (tenant_id, container_id, run_id, segment_id)" in human_ddl
    assert (
        "UNIQUE (tenant_id, container_id, artifact_version_id)"
        in ddl_by_table["rights_declarations"]
    )
    assert "content_hash = encode(sha256(content_bytes), 'hex')" in artifact_version_ddl
    canonical_object_ddl = str(
        CreateTable(schema.metadata.tables["content_object_versions"]).compile(dialect=dialect)
    )
    assert "content_bytes" not in canonical_object_ddl


def test_actual_upgrade_sql_passes_scope_and_role_lint(registry: dict[str, Any]) -> None:
    sql = _current_upgrade_sql()
    linter = _load_linter()
    assert linter.lint_sql(sql, registry) == []
    assert sql.count("FORCE ROW LEVEL SECURITY") == 20
    assert sql.count("TO owcopilot_migration USING (true) WITH CHECK (true)") == 20
    for table in schema.IMMUTABLE_TABLES:
        assert f"CREATE TRIGGER {table}_immutable" in sql
    assert "CREATE TRIGGER source_artifacts_logical_delete_guard" in sql
    assert "CREATE TRIGGER intake_runs_state_guard" in sql
    assert "CREATE TRIGGER triage_verdicts_raw_guard" in sql


@pytest.mark.parametrize(
    ("trigger", "expected_error"),
    [
        (
            "triage_verdicts_immutable",
            "triage_verdicts: missing UPDATE/DELETE immutable trigger",
        ),
        (
            "source_artifacts_logical_delete_guard",
            "source_artifacts: logical-delete-only state guard is incomplete",
        ),
        (
            "intake_runs_state_guard",
            "intake_runs: transition and frozen-identity guard is incomplete",
        ),
        (
            "triage_verdicts_raw_guard",
            "triage_verdicts: invalid-raw UNKNOWN abstention guard is incomplete",
        ),
    ],
)
def test_linter_rejects_missing_n21_guards(
    registry: dict[str, Any], trigger: str, expected_error: str
) -> None:
    sql = _current_upgrade_sql()
    weakened = re.sub(
        rf"CREATE TRIGGER {trigger}\b.*?;",
        "",
        sql,
        count=1,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert weakened != sql
    assert expected_error in _load_linter().lint_sql(weakened, registry)


def test_linter_accepts_matched_control(registry: dict[str, Any]) -> None:
    linter = _load_linter()
    extended = _registry_with_fixture(registry, "future_scope_notes")
    assert linter.lint_sql(GOOD_SQL.read_text(encoding="utf-8"), extended) == []


def test_linter_rejects_missing_scope_force_role_and_negative_case(
    registry: dict[str, Any],
) -> None:
    linter = _load_linter()
    extended = _registry_with_fixture(registry, "future_scope_leak")
    errors = linter.lint_sql(BAD_SQL.read_text(encoding="utf-8"), extended)
    joined = "\n".join(errors)
    assert "lacks container_id OwnershipScope" in joined
    assert "missing composite OwnershipScope FK" in joined
    assert "missing FORCE ROW LEVEL SECURITY" in joined
    assert "missing runtime USING/WITH CHECK policy" in joined
    assert "missing FORCE-RLS migration-owner policy" in joined
    assert "missing REVOKE ALL FROM PUBLIC" in joined
    assert "forbidden runtime GRANT" in joined
    assert "missing REVOKE ALL FROM PUBLIC" in joined


def test_linter_rejects_unregistered_tenant_owned_table(registry: dict[str, Any]) -> None:
    linter = _load_linter()
    errors = linter.lint_sql(GOOD_SQL.read_text(encoding="utf-8"), registry)
    assert "future_scope_notes: unregistered tenant-owned table" in errors


def test_linter_rejects_registered_table_without_tenant_scope(
    registry: dict[str, Any],
) -> None:
    linter = _load_linter()
    extended = _registry_with_fixture(registry, "future_scope_notes")
    errors = linter.lint_sql(
        "CREATE TABLE future_scope_notes (container_id uuid NOT NULL);",
        extended,
    )
    assert "future_scope_notes: registered table lacks tenant_id OwnershipScope" in errors


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE public.future_scope_notes (tenant_id uuid NOT NULL);",
        'CREATE TABLE "future_scope_notes" (tenant_id uuid NOT NULL);',
        "CREATE TEMP TABLE future_scope_notes (tenant_id uuid NOT NULL);",
    ],
)
def test_linter_rejects_create_table_syntax_it_cannot_parse(
    registry: dict[str, Any], sql: str
) -> None:
    linter = _load_linter()
    extended = _registry_with_fixture(registry, "future_scope_notes")
    errors = linter.lint_sql(sql, extended)
    assert any(
        "CREATE TABLE syntax the fail-closed linter cannot parse" in error for error in errors
    )


def test_role_bootstrap_and_compose_pin_exact_postgresql_image() -> None:
    role_source = (ROOT / "infra/team/postgres/initdb/001_roles.sh").read_text(encoding="utf-8")
    assert "NOBYPASSRLS" in role_source
    assert "NOSUPERUSER" in role_source
    assert "REVOKE owcopilot_migration FROM owcopilot_app" in role_source
    digest = "sha256:4f736ae292687621d4dbe0d499ffd024a36bd2ee7d8ca6f2ccd4c800f047b394"
    for path in (ROOT / "docker-compose.yml", ROOT / "infra/team/docker-compose.yml"):
        text = path.read_text(encoding="utf-8")
        assert f"postgres:17.10-bookworm@{digest}" in text
    local_compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert '"127.0.0.1:${OWCOPILOT_POSTGRES_PORT:-5432}:5432"' in local_compose


def test_postgres_ci_verifies_the_running_container_against_the_pinned_digest() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    step_start = workflow.index("      - name: Start pinned PostgreSQL")
    step_end = workflow.index("\n      - name:", step_start + 1)
    step = workflow[step_start:step_end]

    digest = "sha256:4f736ae292687621d4dbe0d499ffd024a36bd2ee7d8ca6f2ccd4c800f047b394"
    assert "set -euo pipefail" in step
    assert f"postgres_digest='{digest}'" in step
    assert 'postgres_ref="postgres:17.10-bookworm@${postgres_digest}"' in step
    assert 'configured_image="$(docker inspect -f \'{{.Config.Image}}\' "$pg_container")"' in step
    assert 'running_image_id="$(docker inspect -f \'{{.Image}}\' "$pg_container")"' in step
    assert (
        'resolved_image_id="$(docker image inspect "$postgres_ref" --format \'{{.Id}}\')"' in step
    )
    assert 'test "$configured_image" = "$postgres_ref"' in step
    assert 'test -n "$running_image_id"' in step
    assert 'test "$running_image_id" = "$resolved_image_id"' in step
    assert "docker image inspect postgres:17.10-bookworm " not in step


def test_postgres_ci_verifies_upgrade_against_the_configured_alembic_head() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    step_start = workflow.index("      - name: Verify empty and previous-revision Alembic upgrades")
    step_end = workflow.index("\n      - name:", step_start + 1)
    step = workflow[step_start:step_end]

    assert 'ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()' in step
    assert "assert expected_head is not None" in step
    assert "assert observed_head == expected_head" in step
    assert '== "n12_0002"' not in step
    assert "n12_0002 unexpectedly accepted" not in step


def test_alembic_version_is_the_only_global_read_only_exception(
    registry: dict[str, Any],
) -> None:
    assert registry["global_control_tables"] == [
        {
            "name": "alembic_version",
            "owner": "owcopilot_migration",
            "tenant_data": False,
            "rls_exemption": "global schema-head probe only",
            "runtime_grants": ["SELECT"],
        }
    ]
    migration = (ROOT / "infra/migrations/versions/n12_0001_control_plane.py").read_text(
        encoding="utf-8"
    )
    assert "REVOKE ALL ON TABLE alembic_version FROM PUBLIC" in migration
    assert "REVOKE ALL ON TABLE alembic_version FROM owcopilot_app" in migration
    assert "GRANT SELECT ON TABLE alembic_version TO owcopilot_app" in migration


def test_dependency_pins_match_postgres_and_dev_extras() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extras = project["optional-dependencies"]
    expected = {
        "SQLAlchemy>=2.0.51,<2.1",
        "alembic>=1.18.5,<2",
        "psycopg[binary]>=3.3.4,<4",
    }
    assert set(extras["postgres"]) == expected
    assert expected <= set(extras["dev"])


def test_bootstrap_state_machine_and_archive_protocol_are_exact() -> None:
    migration = (ROOT / "infra/migrations/versions/n12_0001_control_plane.py").read_text(
        encoding="utf-8"
    )
    assert "status IN ('building', 'committed')" in migration
    assert "OLD.status = 'building' AND NEW.status = 'committed'" in migration
    assert "runtime role cannot delete a bootstrap import run" in migration
    assert "source_status <> ''" not in migration
    for disposition in (
        "restorable_archive_bundle",
        "deterministic_directory_manifest",
        "preserve_read_only",
        "must_migrate_or_resolve",
        "unknown_requires_adjudication",
    ):
        assert disposition in migration


def test_alembic_environment_has_no_connectable_fallback() -> None:
    ini = (ROOT / "alembic.ini").read_text(encoding="utf-8")
    env = (ROOT / "infra/migrations/env.py").read_text(encoding="utf-8")
    assert "change-me" not in ini
    assert "sqlalchemy.url =\n" in ini
    assert "OWCOPILOT_MIGRATION_DATABASE_URL is required" in env
    assert 'migration_url.drivername != "postgresql+psycopg"' in env
    assert 'migration_url.username != "owcopilot_migration"' in env
