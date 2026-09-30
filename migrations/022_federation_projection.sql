CREATE TABLE IF NOT EXISTS federation_projection_applications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    draft_id UUID NOT NULL REFERENCES drafts(id),
    revision INTEGER NOT NULL CHECK (revision > 0),
    workflow TEXT NOT NULL CHECK (workflow IN ('strategy','history','leadership')),
    content_hash TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    before_hash TEXT NOT NULL CHECK (before_hash ~ '^[0-9a-f]{64}$'),
    after_hash TEXT NOT NULL CHECK (after_hash ~ '^[0-9a-f]{64}$'),
    applied_by UUID NOT NULL REFERENCES users(id),
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (draft_id, revision)
);
