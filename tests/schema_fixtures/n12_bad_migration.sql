-- Deliberately unsafe negative control: no container scope, no FORCE RLS, incomplete policy,
-- broad runtime privilege, no owner policy, and no negative isolation case.
-- owcopilot: isolation-positive future_scope_leak
CREATE TABLE future_scope_leak (
    tenant_id uuid NOT NULL,
    leak_id uuid PRIMARY KEY,
    body text NOT NULL
);

ALTER TABLE future_scope_leak ENABLE ROW LEVEL SECURITY;
CREATE POLICY future_scope_leak_ownership_scope ON future_scope_leak
    FOR ALL TO owcopilot_app
    USING (tenant_id = NULLIF(current_setting('owcopilot.tenant_id', true), '')::uuid);
GRANT ALL PRIVILEGES ON TABLE future_scope_leak TO owcopilot_app;
