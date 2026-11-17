"""Create immutable canonical revisions, scoped pointers, and operation events.

Revision ID: n12_0002
Revises: n12_0001
"""

from __future__ import annotations

from alembic import op

revision = "n12_0002"
down_revision = "n12_0001"
branch_labels = None
depends_on = None

_TENANT = "NULLIF(current_setting('owcopilot.tenant_id', true), '')::uuid"
_CONTAINER = "NULLIF(current_setting('owcopilot.container_id', true), '')::uuid"


def upgrade() -> None:
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
            IF NOT FOUND OR migration_role.rolname IS NULL
               OR migration_role.rolsuper OR migration_role.rolbypassrls
               OR migration_role.rolcreatedb OR migration_role.rolcreaterole
               OR migration_role.rolreplication OR migration_role.rolinherit
               OR NOT migration_role.rolcanlogin THEN
                RAISE EXCEPTION 'migration role is missing or elevated';
            END IF;
            SELECT * INTO app_role FROM pg_roles WHERE rolname = 'owcopilot_app';
            IF NOT FOUND OR app_role.rolsuper OR app_role.rolbypassrls
               OR app_role.rolcreatedb OR app_role.rolcreaterole
               OR app_role.rolreplication OR app_role.rolinherit
               OR NOT app_role.rolcanlogin THEN
                RAISE EXCEPTION 'runtime role is missing or elevated';
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

        CREATE TABLE content_object_versions (
            tenant_id uuid NOT NULL,
            container_id uuid NOT NULL,
            logical_id text NOT NULL,
            object_version_id text NOT NULL,
            object_type text NOT NULL,
            payload_json text NOT NULL,
            payload_jsonb jsonb NOT NULL,
            content_hash char(64) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_content_object_versions
                PRIMARY KEY (tenant_id, container_id, object_version_id),
            CONSTRAINT uq_content_object_versions_logical_version
                UNIQUE (tenant_id, container_id, logical_id, object_version_id),
            CONSTRAINT fk_content_object_versions_container
                FOREIGN KEY (tenant_id, container_id)
                REFERENCES containers (tenant_id, container_id) ON DELETE CASCADE,
            CONSTRAINT ck_content_object_versions_logical_nonempty CHECK (logical_id <> ''),
            CONSTRAINT ck_content_object_versions_type_nonempty CHECK (object_type <> ''),
            CONSTRAINT ck_content_object_versions_id_valid
                CHECK (object_version_id ~ '^objv_[0-9a-f]{64}$'),
            CONSTRAINT ck_content_object_versions_hash_valid
                CHECK (content_hash ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_content_object_versions_payload_twins
                CHECK (payload_jsonb = payload_json::jsonb)
        );

        CREATE TABLE content_revisions (
            tenant_id uuid NOT NULL,
            container_id uuid NOT NULL,
            revision_id text NOT NULL,
            parent_revision text,
            manifest_hash char(64) NOT NULL,
            created_at timestamptz NOT NULL,
            created_by text NOT NULL,
            import_run_id uuid,
            CONSTRAINT pk_content_revisions
                PRIMARY KEY (tenant_id, container_id, revision_id),
            CONSTRAINT fk_content_revisions_container
                FOREIGN KEY (tenant_id, container_id)
                REFERENCES containers (tenant_id, container_id) ON DELETE CASCADE,
            CONSTRAINT fk_content_revisions_parent
                FOREIGN KEY (tenant_id, container_id, parent_revision)
                REFERENCES content_revisions (tenant_id, container_id, revision_id)
                DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT fk_content_revisions_import_run
                FOREIGN KEY (tenant_id, container_id, import_run_id)
                REFERENCES bootstrap_import_runs (tenant_id, container_id, import_run_id)
                DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT ck_content_revisions_id_valid
                CHECK (revision_id ~ '^rev_[0-9a-f]{64}$'),
            CONSTRAINT ck_content_revisions_parent_valid CHECK (
                parent_revision IS NULL OR parent_revision ~ '^rev_[0-9a-f]{64}$'
            ),
            CONSTRAINT ck_content_revisions_manifest_hash_valid
                CHECK (manifest_hash ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_content_revisions_created_by_nonempty CHECK (created_by <> '')
        );

        CREATE TABLE revision_memberships (
            tenant_id uuid NOT NULL,
            container_id uuid NOT NULL,
            revision_id text NOT NULL,
            logical_id text NOT NULL,
            object_version_id text,
            tombstone boolean NOT NULL,
            CONSTRAINT pk_revision_memberships
                PRIMARY KEY (tenant_id, container_id, revision_id, logical_id),
            CONSTRAINT fk_revision_memberships_container
                FOREIGN KEY (tenant_id, container_id)
                REFERENCES containers (tenant_id, container_id) ON DELETE CASCADE,
            CONSTRAINT fk_revision_memberships_revision
                FOREIGN KEY (tenant_id, container_id, revision_id)
                REFERENCES content_revisions (tenant_id, container_id, revision_id)
                ON DELETE CASCADE,
            CONSTRAINT fk_revision_memberships_object_version
                FOREIGN KEY (tenant_id, container_id, logical_id, object_version_id)
                REFERENCES content_object_versions (
                    tenant_id, container_id, logical_id, object_version_id
                ),
            CONSTRAINT ck_revision_memberships_logical_nonempty CHECK (logical_id <> ''),
            CONSTRAINT ck_revision_memberships_live_or_tombstone CHECK (
                (object_version_id IS NOT NULL AND tombstone = false)
                OR (object_version_id IS NULL AND tombstone = true)
            )
        );

        CREATE TABLE release_pointers (
            tenant_id uuid NOT NULL,
            container_id uuid NOT NULL,
            authoring_head text,
            serving_head text,
            updated_at timestamptz NOT NULL DEFAULT now(),
            lock_version bigint NOT NULL DEFAULT 0,
            CONSTRAINT pk_release_pointers PRIMARY KEY (tenant_id, container_id),
            CONSTRAINT fk_release_pointers_container
                FOREIGN KEY (tenant_id, container_id)
                REFERENCES containers (tenant_id, container_id) ON DELETE CASCADE,
            CONSTRAINT fk_release_pointers_authoring_head
                FOREIGN KEY (tenant_id, container_id, authoring_head)
                REFERENCES content_revisions (tenant_id, container_id, revision_id)
                DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT fk_release_pointers_serving_head
                FOREIGN KEY (tenant_id, container_id, serving_head)
                REFERENCES content_revisions (tenant_id, container_id, revision_id)
                DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT ck_release_pointers_serving_requires_authoring
                CHECK (serving_head IS NULL OR authoring_head IS NOT NULL),
            CONSTRAINT ck_release_pointers_lock_version_nonnegative CHECK (lock_version >= 0)
        );

        CREATE TABLE operation_events (
            tenant_id uuid NOT NULL,
            container_id uuid NOT NULL,
            event_id uuid NOT NULL,
            event_type text NOT NULL,
            subject_id uuid NOT NULL,
            revision_id text,
            actor_id uuid NOT NULL,
            occurred_at timestamptz NOT NULL DEFAULT now(),
            payload_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
            CONSTRAINT pk_operation_events PRIMARY KEY (tenant_id, container_id, event_id),
            CONSTRAINT fk_operation_events_container
                FOREIGN KEY (tenant_id, container_id)
                REFERENCES containers (tenant_id, container_id) ON DELETE CASCADE,
            CONSTRAINT fk_operation_events_revision
                FOREIGN KEY (tenant_id, container_id, revision_id)
                REFERENCES content_revisions (tenant_id, container_id, revision_id)
                DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT ck_operation_events_type_nonempty CHECK (event_type <> ''),
            CONSTRAINT ck_operation_events_subject_non_nil CHECK (
                subject_id <> '00000000-0000-0000-0000-000000000000'::uuid
            ),
            CONSTRAINT ck_operation_events_actor_non_nil CHECK (
                actor_id <> '00000000-0000-0000-0000-000000000000'::uuid
            )
        );

        ALTER TABLE bootstrap_import_runs
            ADD CONSTRAINT fk_bootstrap_import_runs_genesis_revision
            FOREIGN KEY (tenant_id, container_id, genesis_revision_id)
            REFERENCES content_revisions (tenant_id, container_id, revision_id)
            DEFERRABLE INITIALLY DEFERRED;
        ALTER TABLE bootstrap_import_runs
            ADD CONSTRAINT ck_bootstrap_import_runs_genesis_revision_valid CHECK (
                genesis_revision_id IS NULL OR genesis_revision_id ~ '^rev_[0-9a-f]{64}$'
            );

        CREATE TRIGGER content_object_versions_immutable
        BEFORE UPDATE OR DELETE ON content_object_versions
        FOR EACH ROW EXECUTE FUNCTION owcopilot_reject_immutable_mutation();
        CREATE TRIGGER content_revisions_immutable
        BEFORE UPDATE OR DELETE ON content_revisions
        FOR EACH ROW EXECUTE FUNCTION owcopilot_reject_immutable_mutation();
        CREATE TRIGGER revision_memberships_immutable
        BEFORE UPDATE OR DELETE ON revision_memberships
        FOR EACH ROW EXECUTE FUNCTION owcopilot_reject_immutable_mutation();
        CREATE TRIGGER operation_events_immutable
        BEFORE UPDATE OR DELETE ON operation_events
        FOR EACH ROW EXECUTE FUNCTION owcopilot_reject_immutable_mutation();
        """
    )

    scope_policy = f"tenant_id = {_TENANT} AND container_id = {_CONTAINER}"
    for table in (
        "content_object_versions",
        "content_revisions",
        "revision_memberships",
        "release_pointers",
        "operation_events",
    ):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_ownership_scope ON {table} "
            f"FOR ALL TO owcopilot_app USING ({scope_policy}) WITH CHECK ({scope_policy})"
        )
        op.execute(
            f"CREATE POLICY {table}_migration_owner ON {table} "
            "FOR ALL TO owcopilot_migration USING (true) WITH CHECK (true)"
        )

    op.execute(
        """
        REVOKE ALL ON TABLE content_object_versions, content_revisions,
            revision_memberships, release_pointers, operation_events FROM PUBLIC;
        REVOKE ALL ON TABLE content_object_versions, content_revisions,
            revision_memberships, release_pointers, operation_events FROM owcopilot_app;

        GRANT SELECT, INSERT ON TABLE content_object_versions, content_revisions,
            revision_memberships, operation_events TO owcopilot_app;
        GRANT SELECT, INSERT, UPDATE ON TABLE release_pointers TO owcopilot_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $role_guard$
        BEGIN
            IF current_user <> 'owcopilot_migration' THEN
                RAISE EXCEPTION 'migrations must run as owcopilot_migration, got %', current_user;
            END IF;
        END
        $role_guard$;

        ALTER TABLE bootstrap_import_runs
            DROP CONSTRAINT fk_bootstrap_import_runs_genesis_revision,
            DROP CONSTRAINT ck_bootstrap_import_runs_genesis_revision_valid;
        DROP TABLE operation_events;
        DROP TABLE release_pointers;
        DROP TABLE revision_memberships;
        DROP TABLE content_revisions;
        DROP TABLE content_object_versions;
        """
    )
