ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK (kind IN (
    'transcribe', 'extract', 'media', 'reply', 'reconcile', 'publish_preview',
    'publish_confirm', 'readiness_scan'
));

CREATE TABLE IF NOT EXISTS notification_subscriptions (
    user_id UUID NOT NULL REFERENCES users(id),
    event_type TEXT NOT NULL CHECK (event_type IN ('content_ready', 'publication_status')),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, event_type)
);

CREATE TABLE IF NOT EXISTS content_readiness (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type TEXT NOT NULL CHECK (entity_type IN ('city', 'federation')),
    entity_key TEXT NOT NULL CHECK (char_length(entity_key) BETWEEN 1 AND 120),
    content_hash TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    ready BOOLEAN NOT NULL,
    missing JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(missing) = 'array'),
    snapshot_draft_id UUID REFERENCES drafts(id),
    notified_hash TEXT NOT NULL DEFAULT ''
        CHECK (notified_hash = '' OR notified_hash ~ '^[0-9a-f]{64}$'),
    checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    notified_at TIMESTAMPTZ,
    UNIQUE (entity_type, entity_key)
);

CREATE UNIQUE INDEX IF NOT EXISTS publication_jobs_active_revision_uq
    ON publication_jobs(draft_id, revision)
    WHERE status NOT IN ('failed', 'cancelled');

CREATE INDEX IF NOT EXISTS content_readiness_pending_notification_idx
    ON content_readiness(ready, checked_at)
    WHERE ready=TRUE AND notified_hash='';

CREATE TABLE IF NOT EXISTS readiness_notifications (
    readiness_id UUID NOT NULL REFERENCES content_readiness(id),
    user_id UUID NOT NULL REFERENCES users(id),
    content_hash TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (readiness_id, user_id, content_hash)
);
