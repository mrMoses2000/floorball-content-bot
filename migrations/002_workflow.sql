CREATE TABLE IF NOT EXISTS media_assets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sha256 TEXT NOT NULL UNIQUE CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    original_filename TEXT NOT NULL CHECK (char_length(original_filename) <= 255),
    detected_mime TEXT NOT NULL CHECK (char_length(detected_mime) <= 120),
    byte_size BIGINT NOT NULL CHECK (byte_size > 0),
    width INTEGER,
    height INTEGER,
    uploader_id UUID NOT NULL REFERENCES users(id),
    original_path TEXT NOT NULL,
    derivative_path TEXT,
    caption_ru TEXT NOT NULL DEFAULT '' CHECK (char_length(caption_ru) <= 600),
    caption_kz TEXT NOT NULL DEFAULT '' CHECK (char_length(caption_kz) <= 600),
    caption_en TEXT NOT NULL DEFAULT '' CHECK (char_length(caption_en) <= 600),
    author TEXT NOT NULL DEFAULT '' CHECK (char_length(author) <= 180),
    taken_at DATE,
    moderation_status TEXT NOT NULL DEFAULT 'pending' CHECK (moderation_status IN ('pending','approved','rejected','quarantined')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ
);

ALTER TABLE city_content DROP CONSTRAINT IF EXISTS city_content_hero_media_id_fkey;
ALTER TABLE city_content ADD CONSTRAINT city_content_hero_media_id_fkey FOREIGN KEY (hero_media_id) REFERENCES media_assets(id);
ALTER TABLE leadership_profiles DROP CONSTRAINT IF EXISTS leadership_profiles_media_id_fkey;
ALTER TABLE leadership_profiles ADD CONSTRAINT leadership_profiles_media_id_fkey FOREIGN KEY (media_id) REFERENCES media_assets(id);

CREATE TABLE IF NOT EXISTS media_links (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    media_id UUID NOT NULL REFERENCES media_assets(id),
    entity_type TEXT NOT NULL CHECK (entity_type IN ('city','club','player','leadership','federation_section')),
    entity_id UUID NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('hero','gallery','portrait','illustration')),
    selected_for_publication BOOLEAN NOT NULL DEFAULT FALSE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (media_id, entity_type, entity_id, purpose)
);

CREATE TABLE IF NOT EXISTS consents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subject_type TEXT NOT NULL CHECK (subject_type IN ('player','media','contact','leadership')),
    subject_id UUID NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('name_bio','portrait','contact','media_publication')),
    status TEXT NOT NULL CHECK (status IN ('pending','granted','withdrawn','rejected')),
    minor BOOLEAN NOT NULL DEFAULT FALSE,
    guardian_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
    evidence_private TEXT NOT NULL DEFAULT '',
    legal_text_version TEXT NOT NULL DEFAULT 'pending-legal-approval',
    granted_by UUID REFERENCES users(id),
    reviewed_by UUID REFERENCES users(id),
    valid_from TIMESTAMPTZ,
    valid_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (NOT minor OR status <> 'granted' OR guardian_confirmed)
);
CREATE INDEX IF NOT EXISTS consents_subject_idx ON consents(subject_type, subject_id, scope, status);

CREATE TABLE IF NOT EXISTS conversation_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    city_id UUID REFERENCES cities(id),
    workflow TEXT NOT NULL CHECK (workflow IN ('city','federation','review','publish','user_admin')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','completed','cancelled')),
    current_step TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1,
    last_activity_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID REFERENCES conversation_sessions(id),
    user_id UUID REFERENCES users(id),
    telegram_chat_id BIGINT NOT NULL,
    telegram_message_id BIGINT,
    telegram_update_id BIGINT,
    direction TEXT NOT NULL CHECK (direction IN ('inbound','outbound')),
    message_type TEXT NOT NULL CHECK (message_type IN ('text','voice','audio','photo','document','contact','callback','system')),
    source_language TEXT NOT NULL DEFAULT 'unknown' CHECK (source_language IN ('ru','kz','en','unknown')),
    original_text TEXT NOT NULL DEFAULT '',
    normalized_text TEXT NOT NULL DEFAULT '',
    translation_text TEXT NOT NULL DEFAULT '',
    translation_status TEXT NOT NULL DEFAULT 'source' CHECK (translation_status IN ('source','machine_draft','reviewed','rejected')),
    translation_method TEXT NOT NULL DEFAULT '',
    translation_reviewer UUID REFERENCES users(id),
    media_id UUID REFERENCES media_assets(id),
    edited_from_message_id UUID REFERENCES messages(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS conversation_memory (
    session_id UUID PRIMARY KEY REFERENCES conversation_sessions(id),
    summary TEXT NOT NULL DEFAULT '' CHECK (char_length(summary) <= 8000),
    structured_memory JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(structured_memory) = 'object'),
    revision INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS drafts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES conversation_sessions(id),
    entity_type TEXT NOT NULL CHECK (entity_type IN ('city','player','federation','leadership','partner')),
    entity_id UUID,
    city_id UUID REFERENCES cities(id),
    status TEXT NOT NULL DEFAULT 'collecting' CHECK (status IN ('collecting','ready_for_user_review','submitted','under_review','changes_requested','approved','publishing','published','rejected','cancelled','publish_failed','revoked')),
    current_revision INTEGER NOT NULL DEFAULT 1 CHECK (current_revision > 0),
    approved_revision INTEGER,
    created_by UUID NOT NULL REFERENCES users(id),
    updated_by UUID NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS draft_revisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    draft_id UUID NOT NULL REFERENCES drafts(id),
    revision INTEGER NOT NULL CHECK (revision > 0),
    content JSONB NOT NULL CHECK (jsonb_typeof(content) = 'object'),
    content_hash TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    source_message_id UUID REFERENCES messages(id),
    created_by UUID NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (draft_id, revision),
    UNIQUE (draft_id, content_hash)
);

CREATE TABLE IF NOT EXISTS approval_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    draft_id UUID NOT NULL REFERENCES drafts(id),
    revision INTEGER NOT NULL,
    actor_id UUID NOT NULL REFERENCES users(id),
    action TEXT NOT NULL CHECK (action IN ('user_confirmed','submitted','review_started','changes_requested','approved','rejected','revoked')),
    reason TEXT NOT NULL DEFAULT '' CHECK (char_length(reason) <= 2000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS processed_updates (
    update_id BIGINT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'accepted' CHECK (status IN ('accepted','processing','completed','failed')),
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    error_class TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind TEXT NOT NULL CHECK (kind IN ('transcribe','extract','media','reply','reconcile','publish_preview')),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','retry','succeeded','dead')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 20),
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_at TIMESTAMPTZ,
    locked_by TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    last_error TEXT NOT NULL DEFAULT '' CHECK (char_length(last_error) <= 4000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS jobs_claim_idx ON jobs(status, available_at, created_at);

CREATE TABLE IF NOT EXISTS outbox_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type TEXT NOT NULL CHECK (event_type IN ('telegram_message','telegram_edit','audit_notification')),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sending','sent','retry','dead')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_at TIMESTAMPTZ,
    locked_by TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    external_id TEXT,
    last_error TEXT NOT NULL DEFAULT '' CHECK (char_length(last_error) <= 2000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS outbox_claim_idx ON outbox_events(status, available_at, created_at);

CREATE TABLE IF NOT EXISTS publication_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    draft_id UUID NOT NULL REFERENCES drafts(id),
    revision INTEGER NOT NULL,
    revision_hash TEXT NOT NULL CHECK (revision_hash ~ '^[0-9a-f]{64}$'),
    status TEXT NOT NULL DEFAULT 'requested' CHECK (status IN ('requested','building','preview_ready','confirmed','pushing','published','failed','cancelled')),
    base_commit TEXT NOT NULL DEFAULT '',
    preview_nonce_hash TEXT NOT NULL DEFAULT '',
    preview_expires_at TIMESTAMPTZ,
    main_commit TEXT,
    static_commit TEXT,
    diff_summary TEXT NOT NULL DEFAULT '',
    check_output TEXT NOT NULL DEFAULT '',
    requested_by UUID NOT NULL REFERENCES users(id),
    confirmed_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS publication_files (
    publication_id UUID NOT NULL REFERENCES publication_jobs(id),
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    byte_size BIGINT NOT NULL CHECK (byte_size >= 0),
    PRIMARY KEY (publication_id, path)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    actor_id UUID REFERENCES users(id),
    action TEXT NOT NULL CHECK (char_length(action) <= 120),
    entity_type TEXT NOT NULL CHECK (char_length(entity_type) <= 80),
    entity_id UUID,
    correlation_id UUID NOT NULL DEFAULT gen_random_uuid(),
    before_hash TEXT,
    after_hash TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_entity_idx ON audit_log(entity_type, entity_id, created_at DESC);

