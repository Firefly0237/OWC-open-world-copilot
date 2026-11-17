"""Create the N1.2 tenant control plane and bootstrap ledger.

Revision ID: n12_0001
Revises: None
"""

from __future__ import annotations

from alembic import op

revision = "n12_0001"
down_revision = None
branch_labels = None
depends_on = None

_TENANT = "NULLIF(current_setting('owcopilot.tenant_id', true), '')::uuid"
_CONTAINER = "NULLIF(current_setting('owcopilot.container_id', true), '')::uuid"


def _assert_roles() -> None:
    op.execute(
        """
        DO $role_guard$
        DECLARE
            migration_role record;
            app_role record;
        BEGIN
            IF current_user <> 'owcopilot_migration' THEN
                RAISE EXCEPTION 'migrations must run as owcopilot_migration, got %', current_user;
            END IF;

            SELECT * INTO migration_role FROM pg_roles
             WHERE rolname = 'owcopilot_migration';
            IF NOT FOUND OR migration_role.rolname IS NULL THEN
                RAISE EXCEPTION 'owcopilot_migration role is missing';
            END IF;
            SELECT * INTO app_role FROM pg_roles WHERE rolname = 'owcopilot_app';
            IF NOT FOUND OR app_role.rolname IS NULL THEN
                RAISE EXCEPTION 'owcopilot_app role is missing';
            END IF;
            IF migration_role.rolsuper OR migration_role.rolbypassrls
               OR migration_role.rolcreatedb OR migration_role.rolcreaterole
               OR migration_role.rolreplication OR migration_role.rolinherit
               OR NOT migration_role.rolcanlogin THEN
                RAISE EXCEPTION 'migration role has forbidden elevated attributes';
            END IF;
            IF app_role.rolsuper OR app_role.rolbypassrls
               OR app_role.rolcreatedb OR app_role.rolcreaterole
               OR app_role.rolreplication OR app_role.rolinherit
               OR NOT app_role.rolcanlogin THEN
                RAISE EXCEPTION 'runtime role has forbidden elevated attributes';
            END IF;
            IF EXISTS (
                SELECT 1 FROM pg_auth_members
                 WHERE member IN (
                    SELECT oid FROM pg_roles
                     WHERE rolname IN ('owcopilot_app', 'owcopilot_migration')
                 )
                    OR roleid IN (
                    SELECT oid FROM pg_roles
                     WHERE rolname IN ('owcopilot_app', 'owcopilot_migration')
                 )
            ) THEN
                RAISE EXCEPTION 'application roles must have no membership edges';
            END IF;
        END
        $role_guard$;
        """
    )


def upgrade() -> None:
    _assert_roles()
    op.execute(
        """
        CREATE TABLE tenants (
            tenant_id uuid PRIMARY KEY,
            slug text NOT NULL UNIQUE,
            display_name text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_tenants_non_nil CHECK (
                tenant_id <> '00000000-0000-0000-0000-000000000000'::uuid
            ),
            CONSTRAINT ck_tenants_slug_nonempty CHECK (slug <> ''),
            CONSTRAINT ck_tenants_display_name_nonempty CHECK (display_name <> '')
        );

        CREATE TABLE memberships (
            tenant_id uuid NOT NULL,
            subject_id uuid NOT NULL,
            role text NOT NULL,
            active boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_memberships PRIMARY KEY (tenant_id, subject_id),
            CONSTRAINT fk_memberships_tenant FOREIGN KEY (tenant_id)
                REFERENCES tenants (tenant_id) ON DELETE CASCADE,
            CONSTRAINT ck_memberships_subject_non_nil CHECK (
                subject_id <> '00000000-0000-0000-0000-000000000000'::uuid
            ),
            CONSTRAINT ck_memberships_role_known CHECK (
                role IN ('owner', 'editor', 'reviewer', 'viewer')
            )
        );

        CREATE TABLE containers (
            tenant_id uuid NOT NULL,
            container_id uuid NOT NULL,
            slug text NOT NULL,
            display_name text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_containers PRIMARY KEY (tenant_id, container_id),
            CONSTRAINT uq_containers_tenant_slug UNIQUE (tenant_id, slug),
            CONSTRAINT fk_containers_tenant FOREIGN KEY (tenant_id)
                REFERENCES tenants (tenant_id) ON DELETE CASCADE,
            CONSTRAINT ck_containers_non_nil CHECK (
                container_id <> '00000000-0000-0000-0000-000000000000'::uuid
            ),
            CONSTRAINT ck_containers_slug_nonempty CHECK (slug <> ''),
            CONSTRAINT ck_containers_display_name_nonempty CHECK (display_name <> '')
        );

        CREATE TABLE bootstrap_import_runs (
            tenant_id uuid NOT NULL,
            container_id uuid NOT NULL,
            import_run_id uuid NOT NULL,
            idempotency_key text NOT NULL,
            source_manifest_hash char(64) NOT NULL,
            status text NOT NULL,
            started_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz,
            genesis_revision_id text,
            error_code text,
            CONSTRAINT pk_bootstrap_import_runs
                PRIMARY KEY (tenant_id, container_id, import_run_id),
            CONSTRAINT uq_bootstrap_import_runs_idempotency
                UNIQUE (tenant_id, container_id, idempotency_key),
            CONSTRAINT fk_bootstrap_import_runs_container
                FOREIGN KEY (tenant_id, container_id)
                REFERENCES containers (tenant_id, container_id) ON DELETE CASCADE,
            CONSTRAINT ck_bootstrap_import_runs_idempotency_nonempty
                CHECK (idempotency_key <> ''),
            CONSTRAINT ck_bootstrap_import_runs_manifest_sha256
                CHECK (source_manifest_hash ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_bootstrap_import_runs_status_known
                CHECK (status IN ('building', 'committed')),
            CONSTRAINT ck_bootstrap_import_runs_state_shape CHECK (
                (status = 'building' AND finished_at IS NULL
                    AND genesis_revision_id IS NULL AND error_code IS NULL)
                OR (status = 'committed' AND finished_at IS NOT NULL
                    AND genesis_revision_id IS NOT NULL AND error_code IS NULL)
            )
        );

        CREATE TABLE legacy_archive_registry (
            tenant_id uuid NOT NULL,
            container_id uuid NOT NULL,
            archive_id uuid NOT NULL,
            relative_path text NOT NULL,
            entry_kind text NOT NULL,
            sha256 char(64) NOT NULL,
            byte_size bigint NOT NULL,
            record_class text NOT NULL,
            disposition text NOT NULL,
            source_status text NOT NULL,
            registered_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_legacy_archive_registry PRIMARY KEY (
                tenant_id, container_id, archive_id, relative_path
            ),
            CONSTRAINT fk_legacy_archive_registry_container
                FOREIGN KEY (tenant_id, container_id)
                REFERENCES containers (tenant_id, container_id) ON DELETE CASCADE,
            CONSTRAINT ck_legacy_archive_registry_relative_path_nonempty
                CHECK (relative_path <> ''),
            CONSTRAINT ck_legacy_archive_registry_entry_kind_known CHECK (
                entry_kind IN ('directory_manifest', 'file', 'sqlite_record')
            ),
            CONSTRAINT ck_legacy_archive_registry_sha256_valid
                CHECK (sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_legacy_archive_registry_byte_size_nonnegative CHECK (byte_size >= 0),
            CONSTRAINT ck_legacy_archive_registry_record_class_known CHECK (
                record_class IN ('archive-only', 'blocker', 'unknown')
            ),
            CONSTRAINT ck_legacy_archive_registry_disposition_known CHECK (
                disposition IN (
                    'restorable_archive_bundle',
                    'deterministic_directory_manifest',
                    'preserve_read_only',
                    'must_migrate_or_resolve',
                    'unknown_requires_adjudication'
                )
            )
        );

        CREATE FUNCTION owcopilot_reject_immutable_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $immutable$
        BEGIN
            RAISE EXCEPTION 'immutable table % rejects %', TG_TABLE_NAME, TG_OP
                USING ERRCODE = '55000';
        END
        $immutable$;
        REVOKE ALL ON FUNCTION owcopilot_reject_immutable_mutation() FROM PUBLIC;

        CREATE TRIGGER legacy_archive_registry_immutable
        BEFORE UPDATE OR DELETE ON legacy_archive_registry
        FOR EACH ROW EXECUTE FUNCTION owcopilot_reject_immutable_mutation();

        CREATE FUNCTION owcopilot_enforce_bootstrap_run_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $bootstrap_transition$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF current_user = 'owcopilot_migration' THEN
                    RETURN OLD;
                END IF;
                RAISE EXCEPTION 'runtime role cannot delete a bootstrap import run'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
               OR NEW.container_id IS DISTINCT FROM OLD.container_id
               OR NEW.import_run_id IS DISTINCT FROM OLD.import_run_id
               OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
               OR NEW.source_manifest_hash IS DISTINCT FROM OLD.source_manifest_hash
               OR NEW.started_at IS DISTINCT FROM OLD.started_at THEN
                RAISE EXCEPTION 'bootstrap import identity and source fields are immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.status = 'building' AND NEW.status = 'committed'
               AND OLD.finished_at IS NULL AND OLD.genesis_revision_id IS NULL
               AND NEW.finished_at IS NOT NULL AND NEW.genesis_revision_id IS NOT NULL
               AND NEW.error_code IS NULL THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'invalid bootstrap import transition % -> %', OLD.status, NEW.status
                USING ERRCODE = '55000';
        END
        $bootstrap_transition$;
        REVOKE ALL ON FUNCTION owcopilot_enforce_bootstrap_run_transition() FROM PUBLIC;
        CREATE TRIGGER bootstrap_import_runs_state_machine
        BEFORE UPDATE OR DELETE ON bootstrap_import_runs
        FOR EACH ROW EXECUTE FUNCTION owcopilot_enforce_bootstrap_run_transition();
        """
    )

    tenant_policy = f"tenant_id = {_TENANT}"
    scope_policy = f"tenant_id = {_TENANT} AND container_id = {_CONTAINER}"
    for table, predicate in (
        ("tenants", tenant_policy),
        ("memberships", tenant_policy),
        ("containers", scope_policy),
        ("bootstrap_import_runs", scope_policy),
        ("legacy_archive_registry", scope_policy),
    ):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_ownership_scope ON {table} "
            f"FOR ALL TO owcopilot_app USING ({predicate}) WITH CHECK ({predicate})"
        )
        op.execute(
            f"CREATE POLICY {table}_migration_owner ON {table} "
            "FOR ALL TO owcopilot_migration USING (true) WITH CHECK (true)"
        )

    op.execute(
        """
        REVOKE ALL ON TABLE tenants, memberships, containers,
            bootstrap_import_runs, legacy_archive_registry FROM PUBLIC;
        REVOKE ALL ON TABLE tenants, memberships, containers,
            bootstrap_import_runs, legacy_archive_registry FROM owcopilot_app;

        GRANT SELECT ON TABLE tenants, memberships, containers TO owcopilot_app;
        GRANT SELECT, INSERT, UPDATE ON TABLE bootstrap_import_runs TO owcopilot_app;
        GRANT SELECT, INSERT ON TABLE legacy_archive_registry TO owcopilot_app;

        REVOKE ALL ON TABLE alembic_version FROM PUBLIC;
        REVOKE ALL ON TABLE alembic_version FROM owcopilot_app;
        GRANT SELECT ON TABLE alembic_version TO owcopilot_app;
        """
    )


def downgrade() -> None:
    _assert_roles()
    op.execute(
        """
        DROP TABLE legacy_archive_registry;
        DROP TABLE bootstrap_import_runs;
        DROP TABLE containers;
        DROP TABLE memberships;
        DROP TABLE tenants;
        DROP FUNCTION owcopilot_enforce_bootstrap_run_transition();
        DROP FUNCTION owcopilot_reject_immutable_mutation();
        """
    )
