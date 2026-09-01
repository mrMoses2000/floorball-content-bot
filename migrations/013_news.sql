ALTER TABLE conversation_sessions DROP CONSTRAINT IF EXISTS conversation_sessions_workflow_check;
ALTER TABLE conversation_sessions ADD CONSTRAINT conversation_sessions_workflow_check CHECK (
    workflow IN (
        'city', 'federation', 'review', 'publish', 'user_admin', 'coach_form',
        'trainer', 'strategy', 'history', 'leadership', 'news'
    )
);

ALTER TABLE agent_context_snapshots DROP CONSTRAINT IF EXISTS agent_context_snapshots_mode_check;
ALTER TABLE agent_context_snapshots ADD CONSTRAINT agent_context_snapshots_mode_check CHECK (
    mode IN ('trainer','strategy','history','leadership','news')
);

ALTER TABLE drafts DROP CONSTRAINT IF EXISTS drafts_entity_type_check;
ALTER TABLE drafts ADD CONSTRAINT drafts_entity_type_check CHECK (
    entity_type IN ('city','player','federation','leadership','partner','news')
);

ALTER TABLE media_links DROP CONSTRAINT IF EXISTS media_links_entity_type_check;
ALTER TABLE media_links ADD CONSTRAINT media_links_entity_type_check CHECK (
    entity_type IN ('city','club','player','leadership','federation_section','news')
);

ALTER TABLE content_readiness DROP CONSTRAINT IF EXISTS content_readiness_entity_type_check;
ALTER TABLE content_readiness ADD CONSTRAINT content_readiness_entity_type_check CHECK (
    entity_type IN ('city','federation','news')
);

CREATE TABLE IF NOT EXISTS news_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug TEXT NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9-]{1,120}$'),
    scope TEXT NOT NULL CHECK (scope IN ('national','city')),
    city_id UUID REFERENCES cities(id),
    title_ru TEXT NOT NULL CHECK (char_length(title_ru) BETWEEN 1 AND 240),
    title_kz TEXT NOT NULL CHECK (char_length(title_kz) BETWEEN 1 AND 240),
    title_en TEXT NOT NULL DEFAULT '' CHECK (char_length(title_en) <= 240),
    excerpt_ru TEXT NOT NULL CHECK (char_length(excerpt_ru) BETWEEN 1 AND 600),
    excerpt_kz TEXT NOT NULL CHECK (char_length(excerpt_kz) BETWEEN 1 AND 600),
    excerpt_en TEXT NOT NULL DEFAULT '' CHECK (char_length(excerpt_en) <= 600),
    body_ru JSONB NOT NULL CHECK (jsonb_typeof(body_ru)='array'),
    body_kz JSONB NOT NULL CHECK (jsonb_typeof(body_kz)='array'),
    body_en JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(body_en)='array'),
    sources JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(sources)='array'),
    image_url TEXT NOT NULL DEFAULT '' CHECK (char_length(image_url) <= 1000),
    image_alt_ru TEXT NOT NULL DEFAULT '' CHECK (char_length(image_alt_ru) <= 300),
    image_alt_kz TEXT NOT NULL DEFAULT '' CHECK (char_length(image_alt_kz) <= 300),
    image_alt_en TEXT NOT NULL DEFAULT '' CHECK (char_length(image_alt_en) <= 300),
    media_rights_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
    video_url TEXT NOT NULL DEFAULT '' CHECK (char_length(video_url) <= 1000),
    status TEXT NOT NULL DEFAULT 'approved' CHECK (status IN ('approved','archived')),
    published_at TIMESTAMPTZ NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_by UUID REFERENCES users(id),
    updated_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    CHECK ((scope='national' AND city_id IS NULL) OR (scope='city' AND city_id IS NOT NULL)),
    CHECK (image_url='' OR (
        media_rights_confirmed=TRUE AND image_alt_ru<>'' AND image_alt_kz<>''
    ))
);

CREATE INDEX IF NOT EXISTS news_items_public_idx
    ON news_items(status, published_at DESC, slug) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS news_items_city_idx
    ON news_items(city_id, published_at DESC) WHERE deleted_at IS NULL AND city_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS news_projection_applications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    draft_id UUID NOT NULL REFERENCES drafts(id),
    revision INTEGER NOT NULL CHECK (revision > 0),
    news_id UUID NOT NULL REFERENCES news_items(id),
    content_hash TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    applied_by UUID NOT NULL REFERENCES users(id),
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (draft_id, revision)
);
