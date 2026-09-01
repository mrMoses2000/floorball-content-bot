ALTER TABLE cities ADD COLUMN IF NOT EXISTS region_aliases TEXT[] NOT NULL DEFAULT '{}';

CREATE TABLE IF NOT EXISTS telegram_start_intents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_id BIGINT NOT NULL,
    update_id BIGINT NOT NULL UNIQUE,
    start_parameter TEXT NOT NULL CHECK (start_parameter IN ('new_city')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','contact_verified','completed','cancelled','expired')),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + interval '24 hours',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS telegram_start_intents_pending_idx
    ON telegram_start_intents(telegram_id, created_at DESC)
    WHERE status='pending';

CREATE TABLE IF NOT EXISTS city_applicants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_id BIGINT NOT NULL UNIQUE,
    phone_e164 TEXT NOT NULL UNIQUE CHECK (phone_e164 ~ '^\+[1-9][0-9]{7,14}$'),
    display_name TEXT NOT NULL DEFAULT '' CHECK (char_length(display_name) <= 180),
    contact_verified_at TIMESTAMPTZ NOT NULL,
    initialized_user_id UUID UNIQUE REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS city_applicant_contact_attempts (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL,
    contact_user_id BIGINT,
    succeeded BOOLEAN NOT NULL,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS city_applicant_contact_attempts_rate_idx
    ON city_applicant_contact_attempts(telegram_id, attempted_at DESC);

CREATE TABLE IF NOT EXISTS city_applications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    applicant_id UUID NOT NULL REFERENCES city_applicants(id),
    status TEXT NOT NULL DEFAULT 'collecting'
        CHECK (status IN (
            'collecting','submitted','under_review','changes_requested','verified',
            'rejected','initialized','cancelled'
        )),
    spec_version TEXT NOT NULL CHECK (char_length(spec_version) BETWEEN 1 AND 40),
    spec_hash TEXT NOT NULL CHECK (spec_hash ~ '^[0-9a-f]{64}$'),
    fields JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(fields)='object'),
    skipped TEXT[] NOT NULL DEFAULT '{}',
    current_step TEXT NOT NULL DEFAULT '' CHECK (char_length(current_step) <= 160),
    slug_candidate TEXT NOT NULL DEFAULT '' CHECK (slug_candidate='' OR slug_candidate ~ '^[a-z0-9-]{1,80}$'),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    submitted_at TIMESTAMPTZ,
    verified_by UUID REFERENCES users(id),
    verified_at TIMESTAMPTZ,
    initialized_city_id UUID UNIQUE REFERENCES cities(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS city_applications_one_open_idx
    ON city_applications(applicant_id)
    WHERE status IN ('collecting','submitted','under_review','changes_requested','verified');
CREATE INDEX IF NOT EXISTS city_applications_review_idx
    ON city_applications(status, submitted_at, created_at);

CREATE TABLE IF NOT EXISTS city_application_events (
    id BIGSERIAL PRIMARY KEY,
    application_id UUID NOT NULL REFERENCES city_applications(id),
    applicant_id UUID REFERENCES city_applicants(id),
    actor_id UUID REFERENCES users(id),
    action TEXT NOT NULL CHECK (action IN (
        'created','answer','submitted','review_started','changes_requested',
        'verified','rejected','initialized','cancelled'
    )),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata)='object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((applicant_id IS NULL) <> (actor_id IS NULL))
);
CREATE INDEX IF NOT EXISTS city_application_events_rate_idx
    ON city_application_events(applicant_id, action, created_at DESC);

CREATE TABLE IF NOT EXISTS city_slug_reservations (
    slug TEXT PRIMARY KEY CHECK (slug ~ '^[a-z0-9-]{1,80}$'),
    application_id UUID NOT NULL UNIQUE REFERENCES city_applications(id),
    reserved_by UUID NOT NULL REFERENCES users(id),
    reserved_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
