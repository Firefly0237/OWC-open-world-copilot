-- Matched control for the future tenant-owned table gate.
-- owcopilot: isolation-positive future_scope_notes
-- owcopilot: isolation-negative future_scope_notes
CREATE TABLE future_scope_notes (
    tenant_id uuid NOT NULL,
    container_id uuid NOT NULL,
    note_id uuid NOT NULL,
    body text NOT NULL,
    PRIMARY KEY (tenant_id, container_id, note_id),
    FOREIGN KEY (tenant_id, container_id)
        REFERENCES containers (tenant_id, container_id) ON DELETE CASCADE
);

ALTER TABLE future_scope_notes ENABLE ROW LEVEL SECURITY;
ALTER TABLE future_scope_notes FORCE ROW LEVEL SECURITY;
CREATE POLICY future_scope_notes_ownership_scope ON future_scope_notes
    FOR ALL TO owcopilot_app
    USING (
        tenant_id = NULLIF(current_setting('owcopilot.tenant_id', true), '')::uuid
        AND container_id = NULLIF(current_setting('owcopilot.container_id', true), '')::uuid
    )
    WITH CHECK (
        tenant_id = NULLIF(current_setting('owcopilot.tenant_id', true), '')::uuid
        AND container_id = NULLIF(current_setting('owcopilot.container_id', true), '')::uuid
    );
CREATE POLICY future_scope_notes_migration_owner ON future_scope_notes
    FOR ALL TO owcopilot_migration USING (true) WITH CHECK (true);
REVOKE ALL ON TABLE future_scope_notes FROM PUBLIC;
REVOKE ALL ON TABLE future_scope_notes FROM owcopilot_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE future_scope_notes TO owcopilot_app;
