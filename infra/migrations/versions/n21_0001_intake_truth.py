"""Create N2.1 immutable intake, triage, rights, and lineage truth.

Revision ID: n21_0001
Revises: n12_0002
"""

from __future__ import annotations

from alembic import op

revision = "n21_0001"
down_revision = "n12_0002"
branch_labels = None
depends_on = None

_TENANT = "NULLIF(current_setting('owcopilot.tenant_id', true), '')::uuid"
_CONTAINER = "NULLIF(current_setting('owcopilot.container_id', true), '')::uuid"
_TABLES = (
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
_IMMUTABLE_TABLES = (
    "artifact_versions",
    "source_segments",
    "raw_triage_outputs",
    "triage_verdicts",
    "human_triage_decisions",
    "rights_declarations",
    "allowed_uses",
    "lineage_anchors",
)


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
            SELECT * INTO app_role FROM pg_roles WHERE rolname = 'owcopilot_app';
            IF migration_role.rolname IS NULL OR app_role.rolname IS NULL
               OR migration_role.rolsuper OR migration_role.rolbypassrls
               OR migration_role.rolcreatedb OR migration_role.rolcreaterole
               OR migration_role.rolreplication OR migration_role.rolinherit
               OR app_role.rolsuper OR app_role.rolbypassrls OR app_role.rolcreatedb
               OR app_role.rolcreaterole OR app_role.rolreplication OR app_role.rolinherit
               OR NOT migration_role.rolcanlogin OR NOT app_role.rolcanlogin THEN
                RAISE EXCEPTION 'application roles are missing or elevated';
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

        CREATE FUNCTION owcopilot_valid_source_locator(expected_kind text, locator jsonb)
        RETURNS boolean
        LANGUAGE plpgsql
        IMMUTABLE
        STRICT
        SET search_path = pg_catalog, public
        AS $locator_guard$
        DECLARE
            key_count integer;
            row_value numeric;
            column_value numeric;
            a1 text;
            letters text;
            referenced_row numeric;
            referenced_column integer := 0;
            position integer;
            page_value numeric;
            line_value numeric;
            x0_value numeric;
            top_value numeric;
            x1_value numeric;
            bottom_value numeric;
            page_width_value numeric;
            page_height_value numeric;
            coordinate_key text;
        BEGIN
            IF jsonb_typeof(locator) <> 'object'
               OR locator->>'kind' IS DISTINCT FROM expected_kind THEN
                RETURN false;
            END IF;
            SELECT count(*) INTO key_count FROM jsonb_object_keys(locator);

            IF expected_kind = 'docx_paragraph' THEN
                IF key_count <> 2
                   OR NOT locator ?& ARRAY['kind', 'paragraph_index']
                   OR jsonb_typeof(locator->'paragraph_index') <> 'number'
                   OR locator->>'paragraph_index' !~ '^(0|[1-9][0-9]*)$' THEN
                    RETURN false;
                END IF;
                RETURN (locator->>'paragraph_index')::numeric <= 2147483647;
            END IF;

            IF expected_kind = 'docx_table_cell' THEN
                IF key_count <> 4
                   OR NOT locator ?& ARRAY['kind', 'table_index', 'row_index', 'column_index']
                   OR jsonb_typeof(locator->'table_index') <> 'number'
                   OR jsonb_typeof(locator->'row_index') <> 'number'
                   OR jsonb_typeof(locator->'column_index') <> 'number'
                   OR locator->>'table_index' !~ '^(0|[1-9][0-9]*)$'
                   OR locator->>'row_index' !~ '^(0|[1-9][0-9]*)$'
                   OR locator->>'column_index' !~ '^(0|[1-9][0-9]*)$' THEN
                    RETURN false;
                END IF;
                RETURN (locator->>'table_index')::numeric <= 2147483647
                   AND (locator->>'row_index')::numeric <= 2147483647
                   AND (locator->>'column_index')::numeric <= 2147483647;
            END IF;

            IF expected_kind = 'xlsx_cell' THEN
                IF key_count <> 5
                   OR NOT locator ?& ARRAY[
                       'kind', 'sheet_name', 'row_index', 'column_index', 'cell_reference'
                   ]
                   OR jsonb_typeof(locator->'sheet_name') <> 'string'
                   OR jsonb_typeof(locator->'row_index') <> 'number'
                   OR jsonb_typeof(locator->'column_index') <> 'number'
                   OR jsonb_typeof(locator->'cell_reference') <> 'string'
                   OR locator->>'sheet_name' = ''
                   OR char_length(locator->>'sheet_name') > 31
                   OR locator->>'row_index' !~ '^[1-9][0-9]*$'
                   OR locator->>'column_index' !~ '^[1-9][0-9]*$'
                   OR locator->>'cell_reference' !~ '^[A-Z]{1,3}[1-9][0-9]*$' THEN
                    RETURN false;
                END IF;
                row_value := (locator->>'row_index')::numeric;
                column_value := (locator->>'column_index')::numeric;
                IF row_value > 1048576 OR column_value > 16384 THEN
                    RETURN false;
                END IF;
                a1 := locator->>'cell_reference';
                letters := substring(a1 FROM '^([A-Z]{1,3})');
                referenced_row := substring(a1 FROM '([1-9][0-9]*)$')::numeric;
                FOR position IN 1..char_length(letters) LOOP
                    referenced_column := referenced_column * 26
                        + ascii(substr(letters, position, 1)) - 64;
                END LOOP;
                RETURN referenced_row = row_value AND referenced_column = column_value;
            END IF;

            IF expected_kind = 'pdf_text_line' THEN
                IF key_count <> 15
                   OR NOT locator ?& ARRAY[
                       'kind', 'page_index', 'line_index', 'x0', 'top', 'x1', 'bottom',
                       'page_width', 'page_height', 'coordinate_space', 'extraction_mode',
                       'validation_backend', 'validation_backend_version',
                       'extraction_backend', 'extraction_backend_version'
                   ]
                   OR jsonb_typeof(locator->'page_index') <> 'number'
                   OR jsonb_typeof(locator->'line_index') <> 'number'
                   OR jsonb_typeof(locator->'x0') <> 'number'
                   OR jsonb_typeof(locator->'top') <> 'number'
                   OR jsonb_typeof(locator->'x1') <> 'number'
                   OR jsonb_typeof(locator->'bottom') <> 'number'
                   OR jsonb_typeof(locator->'page_width') <> 'number'
                   OR jsonb_typeof(locator->'page_height') <> 'number'
                   OR jsonb_typeof(locator->'coordinate_space') <> 'string'
                   OR jsonb_typeof(locator->'extraction_mode') <> 'string'
                   OR jsonb_typeof(locator->'validation_backend') <> 'string'
                   OR jsonb_typeof(locator->'validation_backend_version') <> 'string'
                   OR jsonb_typeof(locator->'extraction_backend') <> 'string'
                   OR jsonb_typeof(locator->'extraction_backend_version') <> 'string'
                   OR locator->>'page_index' !~ '^(0|[1-9][0-9]*)$'
                   OR locator->>'line_index' !~ '^(0|[1-9][0-9]*)$'
                   OR locator->>'coordinate_space' <> 'pdf_points_top_left_display'
                   OR locator->>'extraction_mode' <> 'born_digital_text_line_baseline'
                   OR locator->>'validation_backend' <> 'pypdfium2'
                   OR locator->>'extraction_backend' <> 'pdfplumber'
                   OR locator->>'validation_backend_version' = ''
                   OR locator->>'extraction_backend_version' = ''
                   OR char_length(locator->>'validation_backend_version') > 128
                   OR char_length(locator->>'extraction_backend_version') > 128
                   OR octet_length(locator->>'validation_backend_version') <>
                      char_length(locator->>'validation_backend_version')
                   OR octet_length(locator->>'extraction_backend_version') <>
                      char_length(locator->>'extraction_backend_version') THEN
                    RETURN false;
                END IF;
                FOREACH coordinate_key IN ARRAY ARRAY[
                    'x0', 'top', 'x1', 'bottom', 'page_width', 'page_height'
                ] LOOP
                    IF locator->>coordinate_key !~
                       '^-?((0|[1-9][0-9]*)\\.[0-9]+([eE][+-]?[0-9]+)?|'
                       '(0|[1-9][0-9]*)[eE][+-]?[0-9]+)$' THEN
                        RETURN false;
                    END IF;
                END LOOP;
                page_value := (locator->>'page_index')::numeric;
                line_value := (locator->>'line_index')::numeric;
                x0_value := (locator->>'x0')::numeric;
                top_value := (locator->>'top')::numeric;
                x1_value := (locator->>'x1')::numeric;
                bottom_value := (locator->>'bottom')::numeric;
                page_width_value := (locator->>'page_width')::numeric;
                page_height_value := (locator->>'page_height')::numeric;
                RETURN page_value <= 2147483647
                   AND line_value <= 2147483647
                   AND page_width_value > 0
                   AND page_height_value > 0
                   AND x0_value >= 0
                   AND top_value >= 0
                   AND x1_value > x0_value
                   AND bottom_value > top_value
                   AND x1_value <= page_width_value
                   AND bottom_value <= page_height_value;
            END IF;

            RETURN false;
        EXCEPTION
            WHEN invalid_text_representation OR numeric_value_out_of_range THEN
                RETURN false;
        END
        $locator_guard$;

        CREATE FUNCTION owcopilot_valid_triage_evidence(evidence jsonb)
        RETURNS boolean
        LANGUAGE plpgsql
        IMMUTABLE
        STRICT
        SET search_path = pg_catalog, public
        AS $evidence_guard$
        DECLARE
            span jsonb;
            key_count integer;
            start_value numeric;
            end_value numeric;
        BEGIN
            IF jsonb_typeof(evidence) <> 'array' THEN
                RETURN false;
            END IF;
            FOR span IN SELECT value FROM jsonb_array_elements(evidence) LOOP
                IF jsonb_typeof(span) <> 'object' THEN
                    RETURN false;
                END IF;
                SELECT count(*) INTO key_count FROM jsonb_object_keys(span);
                IF key_count <> 3
                   OR NOT span ?& ARRAY['start', 'end', 'quote']
                   OR jsonb_typeof(span->'start') <> 'number'
                   OR jsonb_typeof(span->'end') <> 'number'
                   OR jsonb_typeof(span->'quote') <> 'string'
                   OR span->>'start' !~ '^(0|[1-9][0-9]*)$'
                   OR span->>'end' !~ '^(0|[1-9][0-9]*)$'
                   OR span->>'quote' = '' THEN
                    RETURN false;
                END IF;
                start_value := (span->>'start')::numeric;
                end_value := (span->>'end')::numeric;
                IF start_value > 2147483647 OR end_value > 2147483647
                   OR end_value <= start_value THEN
                    RETURN false;
                END IF;
            END LOOP;
            RETURN true;
        EXCEPTION
            WHEN invalid_text_representation OR numeric_value_out_of_range THEN
                RETURN false;
        END
        $evidence_guard$;

        REVOKE ALL ON FUNCTION owcopilot_valid_source_locator(text, jsonb) FROM PUBLIC;
        REVOKE ALL ON FUNCTION owcopilot_valid_triage_evidence(jsonb) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION owcopilot_valid_source_locator(text, jsonb)
            TO owcopilot_app, owcopilot_migration;
        GRANT EXECUTE ON FUNCTION owcopilot_valid_triage_evidence(jsonb)
            TO owcopilot_app, owcopilot_migration;

        CREATE TABLE source_artifacts (
            tenant_id uuid NOT NULL,
            container_id uuid NOT NULL,
            artifact_id uuid NOT NULL,
            display_name text NOT NULL,
            created_by uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            logical_deleted_at timestamptz,
            CONSTRAINT pk_source_artifacts PRIMARY KEY (tenant_id, container_id, artifact_id),
            CONSTRAINT fk_source_artifacts_container FOREIGN KEY (tenant_id, container_id)
                REFERENCES containers (tenant_id, container_id) ON DELETE CASCADE,
            CONSTRAINT ck_source_artifacts_artifact_non_nil CHECK (
                artifact_id <> '00000000-0000-0000-0000-000000000000'::uuid
            ),
            CONSTRAINT ck_source_artifacts_display_name_nonempty CHECK (display_name <> ''),
            CONSTRAINT ck_source_artifacts_created_by_non_nil CHECK (
                created_by <> '00000000-0000-0000-0000-000000000000'::uuid
            ),
            CONSTRAINT ck_source_artifacts_logical_delete_not_before_creation CHECK (
                logical_deleted_at IS NULL OR logical_deleted_at >= created_at
            )
        );

        CREATE TABLE artifact_versions (
            tenant_id uuid NOT NULL,
            container_id uuid NOT NULL,
            artifact_version_id uuid NOT NULL,
            artifact_id uuid NOT NULL,
            content_hash char(64) NOT NULL,
            mime_type text NOT NULL,
            extension text NOT NULL,
            byte_size bigint NOT NULL,
            content_bytes bytea NOT NULL,
            created_by uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_artifact_versions
                PRIMARY KEY (tenant_id, container_id, artifact_version_id),
            CONSTRAINT uq_artifact_versions_artifact_hash
                UNIQUE (tenant_id, container_id, artifact_id, content_hash),
            CONSTRAINT fk_artifact_versions_container
                FOREIGN KEY (tenant_id, container_id)
                REFERENCES containers (tenant_id, container_id) ON DELETE RESTRICT,
            CONSTRAINT fk_artifact_versions_artifact
                FOREIGN KEY (tenant_id, container_id, artifact_id)
                REFERENCES source_artifacts (tenant_id, container_id, artifact_id)
                ON DELETE RESTRICT,
            CONSTRAINT ck_artifact_versions_version_non_nil CHECK (
                artifact_version_id <> '00000000-0000-0000-0000-000000000000'::uuid
            ),
            CONSTRAINT ck_artifact_versions_content_hash_valid CHECK (
                content_hash ~ '^[0-9a-f]{64}$'
            ),
            CONSTRAINT ck_artifact_versions_content_hash_matches CHECK (
                content_hash = encode(sha256(content_bytes), 'hex')
            ),
            CONSTRAINT ck_artifact_versions_byte_size_matches CHECK (
                byte_size > 0 AND byte_size = octet_length(content_bytes)
            ),
            CONSTRAINT ck_artifact_versions_supported_format_pair CHECK (
                (extension = '.docx' AND mime_type =
                    'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
                OR (extension = '.pdf' AND mime_type = 'application/pdf')
                OR (extension = '.xlsx' AND mime_type =
                    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            ),
            CONSTRAINT ck_artifact_versions_created_by_non_nil CHECK (
                created_by <> '00000000-0000-0000-0000-000000000000'::uuid
            )
        );

        CREATE TABLE intake_runs (
            tenant_id uuid NOT NULL,
            container_id uuid NOT NULL,
            run_id uuid NOT NULL,
            artifact_version_id uuid NOT NULL,
            original_filename text NOT NULL,
            status text NOT NULL,
            created_by uuid NOT NULL,
            started_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz,
            rejection_code text,
            processing_code text,
            CONSTRAINT pk_intake_runs PRIMARY KEY (tenant_id, container_id, run_id),
            CONSTRAINT uq_intake_runs_run_version
                UNIQUE (tenant_id, container_id, run_id, artifact_version_id),
            CONSTRAINT uq_intake_runs_artifact_version
                UNIQUE (tenant_id, container_id, artifact_version_id),
            CONSTRAINT fk_intake_runs_container FOREIGN KEY (tenant_id, container_id)
                REFERENCES containers (tenant_id, container_id) ON DELETE RESTRICT,
            CONSTRAINT fk_intake_runs_artifact_version
                FOREIGN KEY (tenant_id, container_id, artifact_version_id)
                REFERENCES artifact_versions (tenant_id, container_id, artifact_version_id)
                ON DELETE RESTRICT,
            CONSTRAINT ck_intake_runs_run_non_nil CHECK (
                run_id <> '00000000-0000-0000-0000-000000000000'::uuid
            ),
            CONSTRAINT ck_intake_runs_created_by_non_nil CHECK (
                created_by <> '00000000-0000-0000-0000-000000000000'::uuid
            ),
            CONSTRAINT ck_intake_runs_filename_nonempty CHECK (original_filename <> ''),
            CONSTRAINT ck_intake_runs_status_known CHECK (
                status IN (
                    'UPLOADED', 'SEGMENTED', 'OCR_REQUIRED', 'PROCESSING_FAILED',
                    'AWAITING_HUMAN', 'REJECTED'
                )
            ),
            CONSTRAINT ck_intake_runs_state_shape CHECK (
                (status = 'REJECTED' AND finished_at IS NOT NULL
                    AND rejection_code IS NOT NULL AND processing_code IS NULL)
                OR (status = 'OCR_REQUIRED' AND finished_at IS NULL
                    AND rejection_code IS NULL
                    AND processing_code = 'PDF_OCR_REQUIRED')
                OR (status = 'PROCESSING_FAILED' AND finished_at IS NULL
                    AND rejection_code IS NULL AND processing_code IN (
                        'CORRUPT_PDF', 'PDF_ENCRYPTED',
                        'PDF_LAYOUT_BACKEND_UNAVAILABLE', 'PDF_MAGIC_MISMATCH',
                        'PDF_PAGE_DIMENSION_EXCEEDED', 'PDF_PAGE_LIMIT_EXCEEDED',
                        'PDF_PROCESSING_FAILED', 'PDF_TEXT_LIMIT_EXCEEDED',
                        'PDF_VALIDATION_BACKEND_UNAVAILABLE',
                        'UNLOCATABLE_PDF_TEXT'
                    ))
                OR (status = 'AWAITING_HUMAN' AND finished_at IS NOT NULL
                    AND rejection_code IS NULL AND processing_code IS NULL)
                OR (status IN ('UPLOADED', 'SEGMENTED') AND finished_at IS NULL
                    AND rejection_code IS NULL AND processing_code IS NULL)
            ),
            CONSTRAINT ck_intake_runs_finish_not_before_start CHECK (
                finished_at IS NULL OR finished_at >= started_at
            ),
            CONSTRAINT ck_intake_runs_rejection_code_nonempty CHECK (
                rejection_code IS NULL OR rejection_code <> ''
            ),
            CONSTRAINT ck_intake_runs_processing_code_known CHECK (
                processing_code IS NULL OR processing_code IN (
                    'PDF_OCR_REQUIRED', 'CORRUPT_PDF', 'PDF_ENCRYPTED',
                    'PDF_LAYOUT_BACKEND_UNAVAILABLE', 'PDF_MAGIC_MISMATCH',
                    'PDF_PAGE_DIMENSION_EXCEEDED', 'PDF_PAGE_LIMIT_EXCEEDED',
                    'PDF_PROCESSING_FAILED', 'PDF_TEXT_LIMIT_EXCEEDED',
                    'PDF_VALIDATION_BACKEND_UNAVAILABLE',
                    'UNLOCATABLE_PDF_TEXT'
                )
            )
        );

        CREATE TABLE source_segments (
            tenant_id uuid NOT NULL,
            container_id uuid NOT NULL,
            segment_id uuid NOT NULL,
            artifact_version_id uuid NOT NULL,
            ordinal integer NOT NULL,
            locator_kind text NOT NULL,
            locator_jsonb jsonb NOT NULL,
            text_content text NOT NULL,
            text_hash char(64) NOT NULL,
            untrusted_input boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_source_segments PRIMARY KEY (tenant_id, container_id, segment_id),
            CONSTRAINT uq_source_segments_version_ordinal
                UNIQUE (tenant_id, container_id, artifact_version_id, ordinal),
            CONSTRAINT uq_source_segments_segment_version
                UNIQUE (tenant_id, container_id, segment_id, artifact_version_id),
            CONSTRAINT fk_source_segments_container FOREIGN KEY (tenant_id, container_id)
                REFERENCES containers (tenant_id, container_id) ON DELETE RESTRICT,
            CONSTRAINT fk_source_segments_artifact_version
                FOREIGN KEY (tenant_id, container_id, artifact_version_id)
                REFERENCES artifact_versions (tenant_id, container_id, artifact_version_id)
                ON DELETE RESTRICT,
            CONSTRAINT ck_source_segments_segment_non_nil CHECK (
                segment_id <> '00000000-0000-0000-0000-000000000000'::uuid
            ),
            CONSTRAINT ck_source_segments_ordinal_nonnegative CHECK (ordinal >= 0),
            CONSTRAINT ck_source_segments_locator_kind_known CHECK (
                locator_kind IN (
                    'docx_paragraph', 'docx_table_cell', 'pdf_text_line', 'xlsx_cell'
                )
            ),
            CONSTRAINT ck_source_segments_locator_exact CHECK (
                owcopilot_valid_source_locator(locator_kind, locator_jsonb)
            ),
            CONSTRAINT ck_source_segments_text_nonempty CHECK (text_content <> ''),
            CONSTRAINT ck_source_segments_text_hash_valid CHECK (
                text_hash ~ '^[0-9a-f]{64}$'
            ),
            CONSTRAINT ck_source_segments_text_hash_matches CHECK (
                text_hash = encode(sha256(convert_to(text_content, 'UTF8')), 'hex')
            ),
            CONSTRAINT ck_source_segments_always_untrusted CHECK (untrusted_input = true)
        );

        CREATE TABLE raw_triage_outputs (
            tenant_id uuid NOT NULL,
            container_id uuid NOT NULL,
            raw_output_id uuid NOT NULL,
            run_id uuid NOT NULL,
            segment_id uuid NOT NULL,
            artifact_version_id uuid NOT NULL,
            provider text NOT NULL,
            model text NOT NULL,
            output_text text NOT NULL,
            output_hash char(64) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_raw_triage_outputs
                PRIMARY KEY (tenant_id, container_id, raw_output_id),
            CONSTRAINT uq_raw_triage_outputs_identity_version_segment
                UNIQUE (
                    tenant_id, container_id, raw_output_id, artifact_version_id, segment_id
                ),
            CONSTRAINT uq_raw_triage_outputs_run_segment
                UNIQUE (tenant_id, container_id, run_id, segment_id),
            CONSTRAINT fk_raw_triage_outputs_container
                FOREIGN KEY (tenant_id, container_id)
                REFERENCES containers (tenant_id, container_id) ON DELETE RESTRICT,
            CONSTRAINT fk_raw_triage_outputs_run_version
                FOREIGN KEY (tenant_id, container_id, run_id, artifact_version_id)
                REFERENCES intake_runs (
                    tenant_id, container_id, run_id, artifact_version_id
                ) ON DELETE RESTRICT,
            CONSTRAINT fk_raw_triage_outputs_segment
                FOREIGN KEY (tenant_id, container_id, segment_id, artifact_version_id)
                REFERENCES source_segments (
                    tenant_id, container_id, segment_id, artifact_version_id
                ) ON DELETE RESTRICT,
            CONSTRAINT ck_raw_triage_outputs_raw_output_non_nil CHECK (
                raw_output_id <> '00000000-0000-0000-0000-000000000000'::uuid
            ),
            CONSTRAINT ck_raw_triage_outputs_raw_provider_model_nonempty CHECK (
                provider <> '' AND model <> ''
            ),
            CONSTRAINT ck_raw_triage_outputs_output_hash_valid CHECK (
                output_hash ~ '^[0-9a-f]{64}$'
            ),
            CONSTRAINT ck_raw_triage_outputs_output_hash_matches CHECK (
                output_hash = encode(sha256(convert_to(output_text, 'UTF8')), 'hex')
            )
        );

        CREATE TABLE triage_verdicts (
            tenant_id uuid NOT NULL,
            container_id uuid NOT NULL,
            verdict_id uuid NOT NULL,
            raw_output_id uuid NOT NULL,
            segment_id uuid NOT NULL,
            artifact_version_id uuid NOT NULL,
            content_role text NOT NULL,
            confidence double precision NOT NULL,
            policy_threshold double precision NOT NULL,
            evidence_spans_jsonb jsonb NOT NULL,
            abstain boolean NOT NULL,
            abstain_reason text,
            derivation_policy_version text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_triage_verdicts
                PRIMARY KEY (tenant_id, container_id, verdict_id),
            CONSTRAINT uq_triage_verdicts_verdict_version_segment
                UNIQUE (tenant_id, container_id, verdict_id, artifact_version_id, segment_id),
            CONSTRAINT uq_triage_verdicts_raw_policy
                UNIQUE (tenant_id, container_id, raw_output_id, derivation_policy_version),
            CONSTRAINT fk_triage_verdicts_container
                FOREIGN KEY (tenant_id, container_id)
                REFERENCES containers (tenant_id, container_id) ON DELETE RESTRICT,
            CONSTRAINT fk_triage_verdicts_raw_version_segment
                FOREIGN KEY (
                    tenant_id, container_id, raw_output_id, artifact_version_id, segment_id
                ) REFERENCES raw_triage_outputs (
                    tenant_id, container_id, raw_output_id, artifact_version_id, segment_id
                ) ON DELETE RESTRICT,
            CONSTRAINT ck_triage_verdicts_verdict_non_nil CHECK (
                verdict_id <> '00000000-0000-0000-0000-000000000000'::uuid
            ),
            CONSTRAINT ck_triage_verdicts_content_role_known CHECK (
                content_role IN ('CANON_CANDIDATE', 'REFERENCE', 'MIXED', 'UNKNOWN')
            ),
            CONSTRAINT ck_triage_verdicts_confidence_probability CHECK (
                confidence <> 'NaN'::double precision
                AND confidence >= 0.0 AND confidence <= 1.0
            ),
            CONSTRAINT ck_triage_verdicts_threshold_probability CHECK (
                policy_threshold <> 'NaN'::double precision
                AND policy_threshold >= 0.0 AND policy_threshold <= 1.0
            ),
            CONSTRAINT ck_triage_verdicts_evidence_exact CHECK (
                owcopilot_valid_triage_evidence(evidence_spans_jsonb)
            ),
            CONSTRAINT ck_triage_verdicts_mandatory_abstention CHECK (
                NOT (content_role = 'UNKNOWN' OR confidence < policy_threshold) OR abstain
            ),
            CONSTRAINT ck_triage_verdicts_abstain_reason_shape CHECK (
                abstain = (abstain_reason IS NOT NULL)
            ),
            CONSTRAINT ck_triage_verdicts_non_abstain_has_evidence CHECK (
                abstain OR jsonb_array_length(evidence_spans_jsonb) > 0
            ),
            CONSTRAINT ck_triage_verdicts_derivation_policy_version_nonempty CHECK (
                derivation_policy_version <> ''
            )
        );

        CREATE TABLE human_triage_decisions (
            tenant_id uuid NOT NULL,
            container_id uuid NOT NULL,
            decision_id uuid NOT NULL,
            run_id uuid NOT NULL,
            verdict_id uuid NOT NULL,
            segment_id uuid NOT NULL,
            artifact_version_id uuid NOT NULL,
            decision text NOT NULL,
            content_role text NOT NULL,
            decided_by uuid NOT NULL,
            rationale text NOT NULL,
            decided_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_human_triage_decisions
                PRIMARY KEY (tenant_id, container_id, decision_id),
            CONSTRAINT uq_human_triage_decisions_run_segment
                UNIQUE (tenant_id, container_id, run_id, segment_id),
            CONSTRAINT fk_human_triage_decisions_container
                FOREIGN KEY (tenant_id, container_id)
                REFERENCES containers (tenant_id, container_id) ON DELETE RESTRICT,
            CONSTRAINT fk_human_triage_decisions_run_version
                FOREIGN KEY (tenant_id, container_id, run_id, artifact_version_id)
                REFERENCES intake_runs (
                    tenant_id, container_id, run_id, artifact_version_id
                ) ON DELETE RESTRICT,
            CONSTRAINT fk_human_triage_decisions_verdict_segment_version
                FOREIGN KEY (
                    tenant_id, container_id, verdict_id, artifact_version_id, segment_id
                ) REFERENCES triage_verdicts (
                    tenant_id, container_id, verdict_id, artifact_version_id, segment_id
                ) ON DELETE RESTRICT,
            CONSTRAINT ck_human_triage_decisions_decision_non_nil CHECK (
                decision_id <> '00000000-0000-0000-0000-000000000000'::uuid
            ),
            CONSTRAINT ck_human_triage_decisions_decision_known CHECK (
                decision IN ('CONFIRM', 'OVERRIDE', 'REJECT')
            ),
            CONSTRAINT ck_human_triage_decisions_content_role_known CHECK (
                content_role IN ('CANON_CANDIDATE', 'REFERENCE', 'MIXED', 'UNKNOWN')
            ),
            CONSTRAINT ck_human_triage_decisions_rationale_nonempty CHECK (rationale <> ''),
            CONSTRAINT ck_human_triage_decisions_decided_by_non_nil CHECK (
                decided_by <> '00000000-0000-0000-0000-000000000000'::uuid
            )
        );

        CREATE TABLE rights_declarations (
            tenant_id uuid NOT NULL,
            container_id uuid NOT NULL,
            rights_declaration_id uuid NOT NULL,
            artifact_version_id uuid NOT NULL,
            rights_provenance text NOT NULL,
            declaration_text text NOT NULL,
            declared_by uuid NOT NULL,
            human_asserted boolean NOT NULL,
            declared_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_rights_declarations
                PRIMARY KEY (tenant_id, container_id, rights_declaration_id),
            CONSTRAINT uq_rights_declarations_artifact_version
                UNIQUE (tenant_id, container_id, artifact_version_id),
            CONSTRAINT fk_rights_declarations_container
                FOREIGN KEY (tenant_id, container_id)
                REFERENCES containers (tenant_id, container_id) ON DELETE RESTRICT,
            CONSTRAINT fk_rights_declarations_artifact_version
                FOREIGN KEY (tenant_id, container_id, artifact_version_id)
                REFERENCES artifact_versions (tenant_id, container_id, artifact_version_id)
                ON DELETE RESTRICT,
            CONSTRAINT ck_rights_declarations_provenance_known CHECK (
                rights_provenance IN ('OWNED', 'LICENSED', 'THIRD_PARTY', 'UNKNOWN')
            ),
            CONSTRAINT ck_rights_declarations_declaration_nonempty CHECK (
                declaration_text <> ''
            ),
            CONSTRAINT ck_rights_declarations_human_only CHECK (human_asserted = true),
            CONSTRAINT ck_rights_declarations_rights_declaration_non_nil CHECK (
                rights_declaration_id <> '00000000-0000-0000-0000-000000000000'::uuid
            ),
            CONSTRAINT ck_rights_declarations_declared_by_non_nil CHECK (
                declared_by <> '00000000-0000-0000-0000-000000000000'::uuid
            )
        );

        CREATE TABLE allowed_uses (
            tenant_id uuid NOT NULL,
            container_id uuid NOT NULL,
            allowed_use_id uuid NOT NULL,
            rights_declaration_id uuid NOT NULL,
            use_kind text NOT NULL,
            allowed boolean NOT NULL,
            declared_by uuid NOT NULL,
            declared_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_allowed_uses PRIMARY KEY (tenant_id, container_id, allowed_use_id),
            CONSTRAINT uq_allowed_uses_declaration_kind
                UNIQUE (tenant_id, container_id, rights_declaration_id, use_kind),
            CONSTRAINT fk_allowed_uses_container FOREIGN KEY (tenant_id, container_id)
                REFERENCES containers (tenant_id, container_id) ON DELETE RESTRICT,
            CONSTRAINT fk_allowed_uses_rights_declaration
                FOREIGN KEY (tenant_id, container_id, rights_declaration_id)
                REFERENCES rights_declarations (
                    tenant_id, container_id, rights_declaration_id
                ) ON DELETE RESTRICT,
            CONSTRAINT ck_allowed_uses_use_kind_known CHECK (
                use_kind IN ('ANALYSIS', 'RETRIEVAL', 'DRAFTING', 'TRAINING', 'PUBLICATION')
            ),
            CONSTRAINT ck_allowed_uses_allowed_use_non_nil CHECK (
                allowed_use_id <> '00000000-0000-0000-0000-000000000000'::uuid
            ),
            CONSTRAINT ck_allowed_uses_declared_by_non_nil CHECK (
                declared_by <> '00000000-0000-0000-0000-000000000000'::uuid
            )
        );

        CREATE TABLE lineage_anchors (
            tenant_id uuid NOT NULL,
            container_id uuid NOT NULL,
            lineage_anchor_id uuid NOT NULL,
            segment_id uuid NOT NULL,
            artifact_version_id uuid NOT NULL,
            lineage_kind text NOT NULL,
            target_ref text NOT NULL,
            created_by uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_lineage_anchors
                PRIMARY KEY (tenant_id, container_id, lineage_anchor_id),
            CONSTRAINT fk_lineage_anchors_container FOREIGN KEY (tenant_id, container_id)
                REFERENCES containers (tenant_id, container_id) ON DELETE RESTRICT,
            CONSTRAINT fk_lineage_anchors_segment_version
                FOREIGN KEY (tenant_id, container_id, segment_id, artifact_version_id)
                REFERENCES source_segments (
                    tenant_id, container_id, segment_id, artifact_version_id
                ) ON DELETE RESTRICT,
            CONSTRAINT ck_lineage_anchors_lineage_kind_known CHECK (
                lineage_kind IN ('SOURCE_EVIDENCE', 'INSPIRATION_LINEAGE')
            ),
            CONSTRAINT ck_lineage_anchors_target_ref_nonempty CHECK (target_ref <> ''),
            CONSTRAINT ck_lineage_anchors_lineage_anchor_non_nil CHECK (
                lineage_anchor_id <> '00000000-0000-0000-0000-000000000000'::uuid
            ),
            CONSTRAINT ck_lineage_anchors_created_by_non_nil CHECK (
                created_by <> '00000000-0000-0000-0000-000000000000'::uuid
            )
        );

        CREATE FUNCTION owcopilot_guard_source_artifact_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $source_artifact_guard$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.logical_deleted_at IS NOT NULL THEN
                    RAISE EXCEPTION 'source_artifacts must start active';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'source_artifacts permits logical delete only';
            END IF;
            IF (
                NEW.tenant_id, NEW.container_id, NEW.artifact_id, NEW.display_name,
                NEW.created_by, NEW.created_at
            ) IS DISTINCT FROM (
                OLD.tenant_id, OLD.container_id, OLD.artifact_id, OLD.display_name,
                OLD.created_by, OLD.created_at
            ) THEN
                RAISE EXCEPTION 'source_artifacts identity and audit fields are frozen';
            END IF;
            IF OLD.logical_deleted_at IS NOT NULL OR NEW.logical_deleted_at IS NULL THEN
                RAISE EXCEPTION 'source_artifacts logical delete is one-way NULL to timestamp';
            END IF;
            RETURN NEW;
        END
        $source_artifact_guard$;

        CREATE FUNCTION owcopilot_guard_artifact_version_insert()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $artifact_version_guard$
        DECLARE
            artifact_deleted_at timestamptz;
        BEGIN
            SELECT logical_deleted_at INTO artifact_deleted_at
              FROM source_artifacts
             WHERE tenant_id = NEW.tenant_id
               AND container_id = NEW.container_id
               AND artifact_id = NEW.artifact_id
             FOR KEY SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'artifact_versions requires an existing source artifact';
            END IF;
            IF artifact_deleted_at IS NOT NULL THEN
                RAISE EXCEPTION 'artifact_versions requires an active source artifact';
            END IF;
            RETURN NEW;
        END
        $artifact_version_guard$;

        CREATE FUNCTION owcopilot_guard_intake_run_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $intake_run_guard$
        DECLARE
            segment_count bigint;
            raw_count bigint;
            verdict_count bigint;
            missing_verdict_count bigint;
            first_ordinal integer;
            last_ordinal integer;
            artifact_extension text;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'UPLOADED' THEN
                    RAISE EXCEPTION 'intake_runs must start in UPLOADED';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'intake_runs cannot be deleted';
            END IF;
            IF (
                NEW.tenant_id, NEW.container_id, NEW.run_id, NEW.artifact_version_id,
                NEW.original_filename, NEW.created_by, NEW.started_at
            ) IS DISTINCT FROM (
                OLD.tenant_id, OLD.container_id, OLD.run_id, OLD.artifact_version_id,
                OLD.original_filename, OLD.created_by, OLD.started_at
            ) THEN
                RAISE EXCEPTION 'intake_runs identity and audit fields are frozen';
            END IF;
            IF NOT (
                (OLD.status = 'UPLOADED'
                    AND NEW.status IN (
                        'SEGMENTED', 'OCR_REQUIRED', 'PROCESSING_FAILED', 'REJECTED'
                    ))
                OR (OLD.status = 'PROCESSING_FAILED'
                    AND NEW.status IN (
                        'SEGMENTED', 'OCR_REQUIRED', 'PROCESSING_FAILED'
                    ))
                OR (OLD.status = 'SEGMENTED'
                    AND NEW.status IN ('AWAITING_HUMAN', 'REJECTED'))
            ) THEN
                RAISE EXCEPTION 'invalid intake_runs state transition % to %',
                    OLD.status, NEW.status;
            END IF;
            IF OLD.status IN ('UPLOADED', 'PROCESSING_FAILED')
               AND NEW.status = 'SEGMENTED' THEN
                SELECT count(*), min(ordinal), max(ordinal)
                  INTO segment_count, first_ordinal, last_ordinal
                  FROM source_segments
                 WHERE tenant_id = NEW.tenant_id
                   AND container_id = NEW.container_id
                   AND artifact_version_id = NEW.artifact_version_id;
                IF segment_count = 0 OR first_ordinal <> 0
                   OR last_ordinal <> segment_count - 1 THEN
                    RAISE EXCEPTION
                        'SEGMENTED requires a non-empty contiguous source segment set';
                END IF;
            END IF;
            IF OLD.status IN ('UPLOADED', 'PROCESSING_FAILED')
               AND NEW.status IN ('OCR_REQUIRED', 'PROCESSING_FAILED') THEN
                SELECT extension INTO artifact_extension
                  FROM artifact_versions
                 WHERE tenant_id = NEW.tenant_id
                   AND container_id = NEW.container_id
                   AND artifact_version_id = NEW.artifact_version_id
                 FOR KEY SHARE;
                SELECT count(*) INTO segment_count
                  FROM source_segments
                 WHERE tenant_id = NEW.tenant_id
                   AND container_id = NEW.container_id
                   AND artifact_version_id = NEW.artifact_version_id;
                IF artifact_extension IS DISTINCT FROM '.pdf' OR segment_count <> 0 THEN
                    RAISE EXCEPTION
                        'PDF handoff states require a segment-free PDF artifact version';
                END IF;
            END IF;
            IF OLD.status = 'SEGMENTED' AND NEW.status = 'AWAITING_HUMAN' THEN
                SELECT count(*) INTO segment_count
                  FROM source_segments
                 WHERE tenant_id = NEW.tenant_id
                   AND container_id = NEW.container_id
                   AND artifact_version_id = NEW.artifact_version_id;
                SELECT count(*) INTO raw_count
                  FROM raw_triage_outputs
                 WHERE tenant_id = NEW.tenant_id
                   AND container_id = NEW.container_id
                   AND run_id = NEW.run_id
                   AND artifact_version_id = NEW.artifact_version_id;
                SELECT count(v.verdict_id), count(*) FILTER (WHERE v.verdict_id IS NULL)
                  INTO verdict_count, missing_verdict_count
                  FROM raw_triage_outputs AS r
                  LEFT JOIN triage_verdicts AS v
                    ON v.tenant_id = r.tenant_id
                   AND v.container_id = r.container_id
                   AND v.raw_output_id = r.raw_output_id
                   AND v.artifact_version_id = r.artifact_version_id
                   AND v.segment_id = r.segment_id
                 WHERE r.tenant_id = NEW.tenant_id
                   AND r.container_id = NEW.container_id
                   AND r.run_id = NEW.run_id
                   AND r.artifact_version_id = NEW.artifact_version_id;
                IF segment_count = 0 OR raw_count <> segment_count
                   OR verdict_count <> segment_count OR missing_verdict_count <> 0 THEN
                    RAISE EXCEPTION
                        'AWAITING_HUMAN requires exactly one raw output and verdict per segment';
                END IF;
            END IF;
            RETURN NEW;
        END
        $intake_run_guard$;

        CREATE FUNCTION owcopilot_guard_source_segment_insert()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $source_segment_guard$
        DECLARE
            run_status text;
            artifact_deleted_at timestamptz;
            artifact_extension text;
        BEGIN
            SELECT ir.status, sa.logical_deleted_at, av.extension
              INTO run_status, artifact_deleted_at, artifact_extension
              FROM intake_runs AS ir
              JOIN artifact_versions AS av
                ON av.tenant_id = ir.tenant_id
               AND av.container_id = ir.container_id
               AND av.artifact_version_id = ir.artifact_version_id
              JOIN source_artifacts AS sa
                ON sa.tenant_id = av.tenant_id
               AND sa.container_id = av.container_id
               AND sa.artifact_id = av.artifact_id
             WHERE ir.tenant_id = NEW.tenant_id
               AND ir.container_id = NEW.container_id
               AND ir.artifact_version_id = NEW.artifact_version_id
             FOR KEY SHARE OF ir, sa;
            IF NOT FOUND OR run_status NOT IN ('UPLOADED', 'PROCESSING_FAILED') THEN
                RAISE EXCEPTION
                    'source_segments may only be inserted while the run is retryable';
            END IF;
            IF artifact_deleted_at IS NOT NULL THEN
                RAISE EXCEPTION 'source_segments requires an active source artifact';
            END IF;
            IF (artifact_extension = '.docx' AND NEW.locator_kind NOT IN (
                    'docx_paragraph', 'docx_table_cell'
                ))
               OR (artifact_extension = '.pdf' AND NEW.locator_kind <> 'pdf_text_line')
               OR (artifact_extension = '.xlsx' AND NEW.locator_kind <> 'xlsx_cell') THEN
                RAISE EXCEPTION 'source locator kind does not match artifact format';
            END IF;
            RETURN NEW;
        END
        $source_segment_guard$;

        CREATE FUNCTION owcopilot_guard_raw_triage_output_insert()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $raw_triage_output_guard$
        DECLARE
            run_status text;
            artifact_deleted_at timestamptz;
        BEGIN
            SELECT ir.status, sa.logical_deleted_at
              INTO run_status, artifact_deleted_at
              FROM intake_runs AS ir
              JOIN artifact_versions AS av
                ON av.tenant_id = ir.tenant_id
               AND av.container_id = ir.container_id
               AND av.artifact_version_id = ir.artifact_version_id
              JOIN source_artifacts AS sa
                ON sa.tenant_id = av.tenant_id
               AND sa.container_id = av.container_id
               AND sa.artifact_id = av.artifact_id
             WHERE ir.tenant_id = NEW.tenant_id
               AND ir.container_id = NEW.container_id
               AND ir.run_id = NEW.run_id
               AND ir.artifact_version_id = NEW.artifact_version_id
             FOR KEY SHARE OF ir, sa;
            IF NOT FOUND OR run_status <> 'SEGMENTED' THEN
                RAISE EXCEPTION
                    'raw_triage_outputs may only be inserted while the run is SEGMENTED';
            END IF;
            IF artifact_deleted_at IS NOT NULL THEN
                RAISE EXCEPTION 'raw_triage_outputs requires an active source artifact';
            END IF;
            RETURN NEW;
        END
        $raw_triage_output_guard$;

        CREATE FUNCTION owcopilot_guard_triage_verdict()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $triage_verdict_guard$
        DECLARE
            raw_text text;
            segment_text text;
            run_status text;
            artifact_deleted_at timestamptz;
            parsed_raw jsonb;
            invalid_raw boolean := false;
            evidence_span jsonb;
            start_offset integer;
            end_offset integer;
        BEGIN
            SELECT r.output_text, s.text_content, ir.status, sa.logical_deleted_at
              INTO raw_text, segment_text, run_status, artifact_deleted_at
              FROM raw_triage_outputs AS r
              JOIN intake_runs AS ir
                ON ir.tenant_id = r.tenant_id
               AND ir.container_id = r.container_id
               AND ir.run_id = r.run_id
               AND ir.artifact_version_id = r.artifact_version_id
              JOIN source_segments AS s
                ON s.tenant_id = r.tenant_id
               AND s.container_id = r.container_id
               AND s.segment_id = r.segment_id
               AND s.artifact_version_id = r.artifact_version_id
              JOIN artifact_versions AS av
                ON av.tenant_id = r.tenant_id
               AND av.container_id = r.container_id
               AND av.artifact_version_id = r.artifact_version_id
              JOIN source_artifacts AS sa
                ON sa.tenant_id = av.tenant_id
               AND sa.container_id = av.container_id
               AND sa.artifact_id = av.artifact_id
             WHERE r.tenant_id = NEW.tenant_id
               AND r.container_id = NEW.container_id
               AND r.raw_output_id = NEW.raw_output_id
               AND r.artifact_version_id = NEW.artifact_version_id
               AND r.segment_id = NEW.segment_id
             FOR KEY SHARE OF ir, sa;
            IF NOT FOUND OR run_status <> 'SEGMENTED' THEN
                RAISE EXCEPTION 'triage_verdicts may only be inserted while the run is SEGMENTED';
            END IF;
            IF artifact_deleted_at IS NOT NULL THEN
                RAISE EXCEPTION 'triage_verdicts requires an active source artifact';
            END IF;
            IF btrim(raw_text) = '' THEN
                invalid_raw := true;
            ELSE
                BEGIN
                    parsed_raw := raw_text::jsonb;
                    IF jsonb_typeof(parsed_raw) <> 'object' THEN
                        invalid_raw := true;
                    END IF;
                EXCEPTION
                    WHEN invalid_text_representation THEN
                        invalid_raw := true;
                END;
            END IF;
            IF invalid_raw AND NOT (NEW.content_role = 'UNKNOWN' AND NEW.abstain) THEN
                RAISE EXCEPTION 'empty or invalid raw triage output requires UNKNOWN abstention';
            END IF;
            FOR evidence_span IN
                SELECT value FROM jsonb_array_elements(NEW.evidence_spans_jsonb)
            LOOP
                start_offset := (evidence_span->>'start')::integer;
                end_offset := (evidence_span->>'end')::integer;
                IF end_offset > char_length(segment_text)
                   OR substring(
                        segment_text FROM start_offset + 1 FOR end_offset - start_offset
                   ) IS DISTINCT FROM evidence_span->>'quote' THEN
                    RAISE EXCEPTION
                        'triage verdict evidence must exactly match immutable segment text';
                END IF;
            END LOOP;
            RETURN NEW;
        END
        $triage_verdict_guard$;

        CREATE FUNCTION owcopilot_guard_rights_declaration()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $rights_declaration_guard$
        DECLARE
            artifact_deleted_at timestamptz;
        BEGIN
            SELECT sa.logical_deleted_at INTO artifact_deleted_at
              FROM artifact_versions AS av
              JOIN source_artifacts AS sa
                ON sa.tenant_id = av.tenant_id
               AND sa.container_id = av.container_id
               AND sa.artifact_id = av.artifact_id
             WHERE av.tenant_id = NEW.tenant_id
               AND av.container_id = NEW.container_id
               AND av.artifact_version_id = NEW.artifact_version_id
             FOR KEY SHARE OF sa;
            IF NOT FOUND OR artifact_deleted_at IS NOT NULL THEN
                RAISE EXCEPTION 'rights_declarations requires an active source artifact';
            END IF;
            RETURN NEW;
        END
        $rights_declaration_guard$;

        CREATE FUNCTION owcopilot_guard_allowed_use()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $allowed_use_guard$
        DECLARE
            declaration_actor uuid;
        BEGIN
            SELECT declared_by INTO declaration_actor
              FROM rights_declarations
             WHERE tenant_id = NEW.tenant_id
               AND container_id = NEW.container_id
               AND rights_declaration_id = NEW.rights_declaration_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'allowed_uses requires an existing rights declaration';
            END IF;
            IF NEW.declared_by IS DISTINCT FROM declaration_actor THEN
                RAISE EXCEPTION 'allowed_uses.declared_by must match rights declaration actor';
            END IF;
            RETURN NEW;
        END
        $allowed_use_guard$;

        CREATE FUNCTION owcopilot_assert_allowed_use_completeness()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $allowed_use_completeness$
        DECLARE
            use_count bigint;
            matching_actor_count bigint;
        BEGIN
            SELECT count(*), count(*) FILTER (WHERE declared_by = NEW.declared_by)
              INTO use_count, matching_actor_count
              FROM allowed_uses
             WHERE tenant_id = NEW.tenant_id
               AND container_id = NEW.container_id
               AND rights_declaration_id = NEW.rights_declaration_id;
            IF use_count <> 5 OR matching_actor_count <> 5 THEN
                RAISE EXCEPTION
                    'rights declaration requires exactly five allowed uses by the declaring actor';
            END IF;
            RETURN NULL;
        END
        $allowed_use_completeness$;

        CREATE FUNCTION owcopilot_guard_human_triage_decision()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $human_triage_decision_guard$
        DECLARE
            run_status text;
            derived_role text;
            artifact_deleted_at timestamptz;
            rights_count bigint;
            use_count bigint;
        BEGIN
            SELECT ir.status, v.content_role, sa.logical_deleted_at
              INTO run_status, derived_role, artifact_deleted_at
              FROM intake_runs AS ir
              JOIN triage_verdicts AS v
                ON v.tenant_id = ir.tenant_id
               AND v.container_id = ir.container_id
               AND v.artifact_version_id = ir.artifact_version_id
              JOIN artifact_versions AS av
                ON av.tenant_id = ir.tenant_id
               AND av.container_id = ir.container_id
               AND av.artifact_version_id = ir.artifact_version_id
              JOIN source_artifacts AS sa
                ON sa.tenant_id = av.tenant_id
               AND sa.container_id = av.container_id
               AND sa.artifact_id = av.artifact_id
             WHERE ir.tenant_id = NEW.tenant_id
               AND ir.container_id = NEW.container_id
               AND ir.run_id = NEW.run_id
               AND ir.artifact_version_id = NEW.artifact_version_id
               AND v.verdict_id = NEW.verdict_id
               AND v.segment_id = NEW.segment_id
             FOR KEY SHARE OF ir, sa;
            IF NOT FOUND OR run_status <> 'AWAITING_HUMAN' THEN
                RAISE EXCEPTION
                    'human_triage_decisions requires an AWAITING_HUMAN intake run';
            END IF;
            IF artifact_deleted_at IS NOT NULL THEN
                RAISE EXCEPTION 'human_triage_decisions requires an active source artifact';
            END IF;
            IF NEW.decision = 'CONFIRM' AND NEW.content_role IS DISTINCT FROM derived_role THEN
                RAISE EXCEPTION 'CONFIRM must retain the derived triage content role';
            END IF;
            SELECT count(rd.rights_declaration_id), count(au.allowed_use_id)
              INTO rights_count, use_count
              FROM rights_declarations AS rd
              LEFT JOIN allowed_uses AS au
                ON au.tenant_id = rd.tenant_id
               AND au.container_id = rd.container_id
               AND au.rights_declaration_id = rd.rights_declaration_id
               AND au.declared_by = rd.declared_by
             WHERE rd.tenant_id = NEW.tenant_id
               AND rd.container_id = NEW.container_id
               AND rd.artifact_version_id = NEW.artifact_version_id
             GROUP BY rd.rights_declaration_id;
            IF NOT FOUND OR rights_count <> 5 OR use_count <> 5 THEN
                RAISE EXCEPTION
                    'human_triage_decisions requires a complete five-kind rights declaration';
            END IF;
            RETURN NEW;
        END
        $human_triage_decision_guard$;

        CREATE FUNCTION owcopilot_assert_human_decision_coverage()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $human_decision_coverage$
        DECLARE
            segment_count bigint;
            decision_count bigint;
        BEGIN
            SELECT count(*) INTO segment_count
              FROM source_segments
             WHERE tenant_id = NEW.tenant_id
               AND container_id = NEW.container_id
               AND artifact_version_id = NEW.artifact_version_id;
            SELECT count(*) INTO decision_count
              FROM human_triage_decisions
             WHERE tenant_id = NEW.tenant_id
               AND container_id = NEW.container_id
               AND run_id = NEW.run_id
               AND artifact_version_id = NEW.artifact_version_id;
            IF segment_count = 0 OR decision_count <> segment_count THEN
                RAISE EXCEPTION 'human decisions must cover every source segment exactly once';
            END IF;
            RETURN NULL;
        END
        $human_decision_coverage$;

        CREATE FUNCTION owcopilot_guard_lineage_anchor()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $lineage_anchor_guard$
        DECLARE
            source_run_id uuid;
            run_status text;
            final_decision text;
            final_role text;
            artifact_deleted_at timestamptz;
            segment_count bigint;
            decision_count bigint;
        BEGIN
            SELECT ir.run_id, ir.status, d.decision, d.content_role, sa.logical_deleted_at
              INTO source_run_id, run_status, final_decision, final_role, artifact_deleted_at
              FROM source_segments AS s
              JOIN intake_runs AS ir
                ON ir.tenant_id = s.tenant_id
               AND ir.container_id = s.container_id
               AND ir.artifact_version_id = s.artifact_version_id
              JOIN artifact_versions AS av
                ON av.tenant_id = s.tenant_id
               AND av.container_id = s.container_id
               AND av.artifact_version_id = s.artifact_version_id
              JOIN source_artifacts AS sa
                ON sa.tenant_id = av.tenant_id
               AND sa.container_id = av.container_id
               AND sa.artifact_id = av.artifact_id
              JOIN human_triage_decisions AS d
                ON d.tenant_id = ir.tenant_id
               AND d.container_id = ir.container_id
               AND d.run_id = ir.run_id
               AND d.artifact_version_id = ir.artifact_version_id
               AND d.segment_id = s.segment_id
             WHERE s.tenant_id = NEW.tenant_id
               AND s.container_id = NEW.container_id
               AND s.segment_id = NEW.segment_id
               AND s.artifact_version_id = NEW.artifact_version_id
             FOR KEY SHARE OF ir, sa;
            IF NOT FOUND OR run_status <> 'AWAITING_HUMAN' THEN
                RAISE EXCEPTION 'lineage requires a completed human intake review';
            END IF;
            IF artifact_deleted_at IS NOT NULL THEN
                RAISE EXCEPTION 'lineage requires an active source artifact';
            END IF;
            SELECT count(*) INTO segment_count
              FROM source_segments
             WHERE tenant_id = NEW.tenant_id
               AND container_id = NEW.container_id
               AND artifact_version_id = NEW.artifact_version_id;
            SELECT count(*) INTO decision_count
              FROM human_triage_decisions
             WHERE tenant_id = NEW.tenant_id
               AND container_id = NEW.container_id
               AND run_id = source_run_id
               AND artifact_version_id = NEW.artifact_version_id;
            IF segment_count = 0 OR decision_count <> segment_count THEN
                RAISE EXCEPTION 'lineage requires complete human decisions for every segment';
            END IF;
            IF final_decision = 'REJECT' OR final_role = 'UNKNOWN' THEN
                RAISE EXCEPTION 'rejected or UNKNOWN human decisions cannot create lineage';
            END IF;
            IF NEW.lineage_kind = 'SOURCE_EVIDENCE'
               AND final_role <> 'CANON_CANDIDATE' THEN
                RAISE EXCEPTION
                    'SOURCE_EVIDENCE requires a final human CANON_CANDIDATE decision';
            END IF;
            IF NEW.lineage_kind = 'INSPIRATION_LINEAGE'
               AND final_role NOT IN ('REFERENCE', 'MIXED') THEN
                RAISE EXCEPTION
                    'INSPIRATION_LINEAGE requires a final human REFERENCE or MIXED decision';
            END IF;
            RETURN NEW;
        END
        $lineage_anchor_guard$;

        REVOKE ALL ON FUNCTION owcopilot_guard_source_artifact_mutation() FROM PUBLIC;
        REVOKE ALL ON FUNCTION owcopilot_guard_artifact_version_insert() FROM PUBLIC;
        REVOKE ALL ON FUNCTION owcopilot_guard_intake_run_mutation() FROM PUBLIC;
        REVOKE ALL ON FUNCTION owcopilot_guard_source_segment_insert() FROM PUBLIC;
        REVOKE ALL ON FUNCTION owcopilot_guard_raw_triage_output_insert() FROM PUBLIC;
        REVOKE ALL ON FUNCTION owcopilot_guard_triage_verdict() FROM PUBLIC;
        REVOKE ALL ON FUNCTION owcopilot_guard_rights_declaration() FROM PUBLIC;
        REVOKE ALL ON FUNCTION owcopilot_guard_allowed_use() FROM PUBLIC;
        REVOKE ALL ON FUNCTION owcopilot_assert_allowed_use_completeness() FROM PUBLIC;
        REVOKE ALL ON FUNCTION owcopilot_guard_human_triage_decision() FROM PUBLIC;
        REVOKE ALL ON FUNCTION owcopilot_assert_human_decision_coverage() FROM PUBLIC;
        REVOKE ALL ON FUNCTION owcopilot_guard_lineage_anchor() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION owcopilot_guard_source_artifact_mutation()
            TO owcopilot_app, owcopilot_migration;
        GRANT EXECUTE ON FUNCTION owcopilot_guard_artifact_version_insert()
            TO owcopilot_app, owcopilot_migration;
        GRANT EXECUTE ON FUNCTION owcopilot_guard_intake_run_mutation()
            TO owcopilot_app, owcopilot_migration;
        GRANT EXECUTE ON FUNCTION owcopilot_guard_source_segment_insert()
            TO owcopilot_app, owcopilot_migration;
        GRANT EXECUTE ON FUNCTION owcopilot_guard_raw_triage_output_insert()
            TO owcopilot_app, owcopilot_migration;
        GRANT EXECUTE ON FUNCTION owcopilot_guard_triage_verdict()
            TO owcopilot_app, owcopilot_migration;
        GRANT EXECUTE ON FUNCTION owcopilot_guard_rights_declaration()
            TO owcopilot_app, owcopilot_migration;
        GRANT EXECUTE ON FUNCTION owcopilot_guard_allowed_use()
            TO owcopilot_app, owcopilot_migration;
        GRANT EXECUTE ON FUNCTION owcopilot_assert_allowed_use_completeness()
            TO owcopilot_app, owcopilot_migration;
        GRANT EXECUTE ON FUNCTION owcopilot_guard_human_triage_decision()
            TO owcopilot_app, owcopilot_migration;
        GRANT EXECUTE ON FUNCTION owcopilot_assert_human_decision_coverage()
            TO owcopilot_app, owcopilot_migration;
        GRANT EXECUTE ON FUNCTION owcopilot_guard_lineage_anchor()
            TO owcopilot_app, owcopilot_migration;
        """
    )

    op.execute(
        "CREATE TRIGGER source_artifacts_logical_delete_guard "
        "BEFORE INSERT OR UPDATE OR DELETE ON source_artifacts FOR EACH ROW "
        "EXECUTE FUNCTION owcopilot_guard_source_artifact_mutation();"
    )
    op.execute(
        "CREATE TRIGGER artifact_versions_active_artifact_guard "
        "BEFORE INSERT ON artifact_versions FOR EACH ROW "
        "EXECUTE FUNCTION owcopilot_guard_artifact_version_insert();"
    )
    op.execute(
        "CREATE TRIGGER intake_runs_state_guard "
        "BEFORE INSERT OR UPDATE OR DELETE ON intake_runs FOR EACH ROW "
        "EXECUTE FUNCTION owcopilot_guard_intake_run_mutation();"
    )
    op.execute(
        "CREATE TRIGGER source_segments_stage_guard BEFORE INSERT ON source_segments "
        "FOR EACH ROW EXECUTE FUNCTION owcopilot_guard_source_segment_insert();"
    )
    op.execute(
        "CREATE TRIGGER raw_triage_outputs_stage_guard BEFORE INSERT ON raw_triage_outputs "
        "FOR EACH ROW EXECUTE FUNCTION owcopilot_guard_raw_triage_output_insert();"
    )
    op.execute(
        "CREATE TRIGGER triage_verdicts_raw_guard BEFORE INSERT ON triage_verdicts "
        "FOR EACH ROW EXECUTE FUNCTION owcopilot_guard_triage_verdict();"
    )
    op.execute(
        "CREATE TRIGGER rights_declarations_active_guard BEFORE INSERT ON rights_declarations "
        "FOR EACH ROW EXECUTE FUNCTION owcopilot_guard_rights_declaration();"
    )
    op.execute(
        "CREATE TRIGGER allowed_uses_actor_guard BEFORE INSERT ON allowed_uses "
        "FOR EACH ROW EXECUTE FUNCTION owcopilot_guard_allowed_use();"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER rights_declarations_complete_uses "
        "AFTER INSERT ON rights_declarations DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION owcopilot_assert_allowed_use_completeness();"
    )
    op.execute(
        "CREATE TRIGGER human_triage_decisions_submission_guard "
        "BEFORE INSERT ON human_triage_decisions FOR EACH ROW "
        "EXECUTE FUNCTION owcopilot_guard_human_triage_decision();"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER human_triage_decisions_complete "
        "AFTER INSERT ON human_triage_decisions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION owcopilot_assert_human_decision_coverage();"
    )
    op.execute(
        "CREATE TRIGGER lineage_anchors_governance_guard BEFORE INSERT ON lineage_anchors "
        "FOR EACH ROW EXECUTE FUNCTION owcopilot_guard_lineage_anchor();"
    )

    for table in _IMMUTABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION owcopilot_reject_immutable_mutation();"
        )

    scope_policy = f"tenant_id = {_TENANT} AND container_id = {_CONTAINER}"
    for table in _TABLES:
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
        REVOKE ALL ON TABLE source_artifacts, artifact_versions, intake_runs,
            source_segments, raw_triage_outputs, triage_verdicts, human_triage_decisions,
            rights_declarations, allowed_uses, lineage_anchors FROM PUBLIC;
        REVOKE ALL ON TABLE source_artifacts, artifact_versions, intake_runs,
            source_segments, raw_triage_outputs, triage_verdicts, human_triage_decisions,
            rights_declarations, allowed_uses, lineage_anchors FROM owcopilot_app;

        GRANT SELECT, INSERT, UPDATE ON TABLE source_artifacts, intake_runs TO owcopilot_app;
        GRANT SELECT, INSERT ON TABLE artifact_versions, source_segments,
            raw_triage_outputs, triage_verdicts, human_triage_decisions, rights_declarations,
            allowed_uses, lineage_anchors TO owcopilot_app;
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

        DROP TABLE lineage_anchors;
        DROP TABLE allowed_uses;
        DROP TABLE rights_declarations;
        DROP TABLE human_triage_decisions;
        DROP TABLE triage_verdicts;
        DROP TABLE raw_triage_outputs;
        DROP TABLE source_segments;
        DROP TABLE intake_runs;
        DROP TABLE artifact_versions;
        DROP TABLE source_artifacts;
        DROP FUNCTION owcopilot_guard_lineage_anchor();
        DROP FUNCTION owcopilot_assert_human_decision_coverage();
        DROP FUNCTION owcopilot_guard_human_triage_decision();
        DROP FUNCTION owcopilot_assert_allowed_use_completeness();
        DROP FUNCTION owcopilot_guard_allowed_use();
        DROP FUNCTION owcopilot_guard_rights_declaration();
        DROP FUNCTION owcopilot_guard_triage_verdict();
        DROP FUNCTION owcopilot_guard_raw_triage_output_insert();
        DROP FUNCTION owcopilot_guard_source_segment_insert();
        DROP FUNCTION owcopilot_guard_intake_run_mutation();
        DROP FUNCTION owcopilot_guard_artifact_version_insert();
        DROP FUNCTION owcopilot_guard_source_artifact_mutation();
        DROP FUNCTION owcopilot_valid_triage_evidence(jsonb);
        DROP FUNCTION owcopilot_valid_source_locator(text, jsonb);
        """
    )
