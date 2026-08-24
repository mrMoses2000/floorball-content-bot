CREATE TABLE IF NOT EXISTS import_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source TEXT NOT NULL CHECK (source IN ('google_forms_import','federation_bundle_import')),
    entity_type TEXT NOT NULL CHECK (entity_type IN ('city','federation_section')),
    entity_key TEXT NOT NULL CHECK (char_length(entity_key) <= 120),
    content_hash TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    content JSONB NOT NULL,
    imported_by UUID REFERENCES users(id),
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, entity_type, entity_key, content_hash)
);
CREATE INDEX IF NOT EXISTS import_snapshots_latest_idx
    ON import_snapshots(source, entity_type, entity_key, imported_at DESC);

