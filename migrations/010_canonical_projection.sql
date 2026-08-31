CREATE TABLE IF NOT EXISTS canonical_projection_applications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    draft_id UUID NOT NULL REFERENCES drafts(id),
    revision INTEGER NOT NULL CHECK (revision > 0),
    projection_kind TEXT NOT NULL CHECK (projection_kind IN ('trainer_city')),
    content_hash TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    before_hash TEXT NOT NULL CHECK (before_hash ~ '^[0-9a-f]{64}$'),
    after_hash TEXT NOT NULL CHECK (after_hash ~ '^[0-9a-f]{64}$'),
    city_id UUID NOT NULL REFERENCES cities(id),
    applied_by UUID NOT NULL REFERENCES users(id),
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (draft_id, revision, projection_kind)
);

CREATE INDEX IF NOT EXISTS canonical_projection_applications_city_idx
    ON canonical_projection_applications(city_id, applied_at DESC);
