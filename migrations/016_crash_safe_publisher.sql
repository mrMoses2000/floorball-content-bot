ALTER TABLE publication_jobs DROP CONSTRAINT IF EXISTS publication_jobs_status_check;
ALTER TABLE publication_jobs ADD CONSTRAINT publication_jobs_status_check CHECK (
    status IN (
        'requested','building','preview_ready','confirming','confirmed','pushing',
        'remote_verified','published','failed','cancelled'
    )
);

ALTER TABLE publication_jobs
    ADD COLUMN IF NOT EXISTS base_static_commit TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS expected_main_commit TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS expected_static_commit TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS change_class TEXT NOT NULL DEFAULT 'content',
    ADD COLUMN IF NOT EXISTS change_manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS publish_lease_owner TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS publish_lease_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS confirmation_chat_id BIGINT,
    ADD COLUMN IF NOT EXISTS push_started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS remote_verified_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_reconciled_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS reconciliation_error TEXT NOT NULL DEFAULT '';

ALTER TABLE publication_jobs
    DROP CONSTRAINT IF EXISTS publication_jobs_base_static_commit_check,
    DROP CONSTRAINT IF EXISTS publication_jobs_expected_main_commit_check,
    DROP CONSTRAINT IF EXISTS publication_jobs_expected_static_commit_check,
    DROP CONSTRAINT IF EXISTS publication_jobs_change_class_check,
    DROP CONSTRAINT IF EXISTS publication_jobs_change_manifest_check,
    DROP CONSTRAINT IF EXISTS publication_jobs_publish_lease_owner_check,
    DROP CONSTRAINT IF EXISTS publication_jobs_reconciliation_error_check;
ALTER TABLE publication_jobs
    ADD CONSTRAINT publication_jobs_base_static_commit_check CHECK (
        base_static_commit='' OR base_static_commit ~ '^[0-9a-f]{40,64}$'
    ),
    ADD CONSTRAINT publication_jobs_expected_main_commit_check CHECK (
        expected_main_commit='' OR expected_main_commit ~ '^[0-9a-f]{40,64}$'
    ),
    ADD CONSTRAINT publication_jobs_expected_static_commit_check CHECK (
        expected_static_commit='' OR expected_static_commit ~ '^[0-9a-f]{40,64}$'
    ),
    ADD CONSTRAINT publication_jobs_change_class_check CHECK (
        change_class IN ('content','code_template')
    ),
    ADD CONSTRAINT publication_jobs_change_manifest_check CHECK (
        jsonb_typeof(change_manifest)='object'
    ),
    ADD CONSTRAINT publication_jobs_publish_lease_owner_check CHECK (
        char_length(publish_lease_owner) <= 200
    ),
    ADD CONSTRAINT publication_jobs_reconciliation_error_check CHECK (
        char_length(reconciliation_error) <= 4000
    );

CREATE INDEX IF NOT EXISTS publication_jobs_reconcile_idx
    ON publication_jobs(status, publish_lease_expires_at, updated_at)
    WHERE status IN ('confirming','pushing','remote_verified');

ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK (kind IN (
    'transcribe', 'extract', 'media', 'reply', 'reconcile', 'publish_preview',
    'publish_confirm', 'publish_reconcile', 'readiness_scan', 'apply_projection',
    'contact_delivery'
));
