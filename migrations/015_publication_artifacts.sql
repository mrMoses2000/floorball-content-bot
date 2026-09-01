ALTER TABLE publication_jobs
    ADD COLUMN IF NOT EXISTS screenshot_manifest_hash TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS artifacts_invalidated_at TIMESTAMPTZ;

ALTER TABLE publication_jobs
    DROP CONSTRAINT IF EXISTS publication_jobs_screenshot_manifest_hash_check;
ALTER TABLE publication_jobs
    ADD CONSTRAINT publication_jobs_screenshot_manifest_hash_check CHECK (
        screenshot_manifest_hash='' OR screenshot_manifest_hash ~ '^[0-9a-f]{64}$'
    );

CREATE TABLE IF NOT EXISTS publication_artifacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    publication_id UUID NOT NULL REFERENCES publication_jobs(id),
    route TEXT NOT NULL CHECK (route LIKE '/%' AND char_length(route) <= 500),
    language TEXT NOT NULL CHECK (language IN ('ru','kz','en')),
    viewport TEXT NOT NULL CHECK (viewport IN ('desktop-1280x720','mobile-390x844')),
    path TEXT NOT NULL CHECK (char_length(path) <= 2000),
    sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    width INTEGER NOT NULL CHECK (width IN (1280,390)),
    height INTEGER NOT NULL CHECK (height IN (720,844)),
    revision_hash TEXT NOT NULL CHECK (revision_hash ~ '^[0-9a-f]{64}$'),
    manifest_hash TEXT NOT NULL CHECK (manifest_hash ~ '^[0-9a-f]{64}$'),
    valid BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    retain_until TIMESTAMPTZ NOT NULL DEFAULT now() + interval '7 days',
    UNIQUE (publication_id, route, language, viewport)
);
CREATE INDEX IF NOT EXISTS publication_artifacts_retention_idx
    ON publication_artifacts(valid, retain_until);

ALTER TABLE callback_actions
    ADD COLUMN IF NOT EXISTS revision_hash TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS manifest_hash TEXT NOT NULL DEFAULT '';

ALTER TABLE callback_actions
    DROP CONSTRAINT IF EXISTS callback_actions_revision_hash_check,
    DROP CONSTRAINT IF EXISTS callback_actions_manifest_hash_check;
ALTER TABLE callback_actions
    ADD CONSTRAINT callback_actions_revision_hash_check CHECK (
        revision_hash='' OR revision_hash ~ '^[0-9a-f]{64}$'
    ),
    ADD CONSTRAINT callback_actions_manifest_hash_check CHECK (
        manifest_hash='' OR manifest_hash ~ '^[0-9a-f]{64}$'
    );

ALTER TABLE outbox_events DROP CONSTRAINT IF EXISTS outbox_events_event_type_check;
ALTER TABLE outbox_events ADD CONSTRAINT outbox_events_event_type_check CHECK (
    event_type IN ('telegram_message','telegram_edit','telegram_media_group','audit_notification')
);
