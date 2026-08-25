ALTER TABLE cities
    ADD COLUMN IF NOT EXISTS hero_url TEXT NOT NULL DEFAULT '/assets/heroes/clubs.png',
    ADD COLUMN IF NOT EXISTS players_estimate INTEGER,
    ADD COLUMN IF NOT EXISTS coaches_estimate INTEGER,
    ADD COLUMN IF NOT EXISTS clubs_estimate INTEGER,
    ADD COLUMN IF NOT EXISTS data_status TEXT NOT NULL DEFAULT 'approved-city-registry',
    ADD COLUMN IF NOT EXISTS public_updated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS public_sort_order INTEGER NOT NULL DEFAULT 0;

ALTER TABLE cities DROP CONSTRAINT IF EXISTS cities_public_contract_check;
ALTER TABLE cities ADD CONSTRAINT cities_public_contract_check CHECK (
    char_length(hero_url) <= 1000
    AND (players_estimate IS NULL OR players_estimate BETWEEN 0 AND 1000000)
    AND (coaches_estimate IS NULL OR coaches_estimate BETWEEN 0 AND 1000000)
    AND (clubs_estimate IS NULL OR clubs_estimate BETWEEN 0 AND 1000000)
    AND data_status IN ('approved-city-registry', 'verified-coach-data')
);

ALTER TABLE clubs ADD COLUMN IF NOT EXISTS source_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS clubs_city_source_key_uq
    ON clubs(city_id, source_key) WHERE source_key IS NOT NULL;

ALTER TABLE training_schedules ADD COLUMN IF NOT EXISTS source_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS schedules_city_source_key_uq
    ON training_schedules(city_id, source_key) WHERE source_key IS NOT NULL;

ALTER TABLE players
    ADD COLUMN IF NOT EXISTS source_key TEXT,
    ADD COLUMN IF NOT EXISTS photo_url TEXT NOT NULL DEFAULT '';
CREATE UNIQUE INDEX IF NOT EXISTS players_source_key_uq
    ON players(source_key) WHERE source_key IS NOT NULL;

ALTER TABLE player_city_memberships ADD COLUMN IF NOT EXISTS source_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS memberships_source_key_uq
    ON player_city_memberships(source_key) WHERE source_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS city_gallery_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    city_id UUID NOT NULL REFERENCES cities(id),
    source_key TEXT NOT NULL,
    public_id TEXT NOT NULL CHECK (char_length(public_id) BETWEEN 1 AND 120),
    src TEXT NOT NULL CHECK (char_length(src) BETWEEN 1 AND 1000),
    thumbnail TEXT NOT NULL CHECK (char_length(thumbnail) BETWEEN 1 AND 1000),
    width INTEGER CHECK (width IS NULL OR width > 0),
    height INTEGER CHECK (height IS NULL OR height > 0),
    alt_ru TEXT NOT NULL DEFAULT '' CHECK (char_length(alt_ru) <= 300),
    alt_kz TEXT NOT NULL DEFAULT '' CHECK (char_length(alt_kz) <= 300),
    alt_en TEXT NOT NULL DEFAULT '' CHECK (char_length(alt_en) <= 300),
    caption_ru TEXT NOT NULL DEFAULT '' CHECK (char_length(caption_ru) <= 600),
    caption_kz TEXT NOT NULL DEFAULT '' CHECK (char_length(caption_kz) <= 600),
    caption_en TEXT NOT NULL DEFAULT '' CHECK (char_length(caption_en) <= 600),
    author TEXT NOT NULL DEFAULT '' CHECK (char_length(author) <= 180),
    taken_at TEXT NOT NULL DEFAULT '' CHECK (char_length(taken_at) <= 40),
    selected_for_publication BOOLEAN NOT NULL DEFAULT TRUE,
    approved_for_publication BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    UNIQUE (city_id, source_key)
);

ALTER TABLE federation_sections
    ADD COLUMN IF NOT EXISTS public_content JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE federation_sections DROP CONSTRAINT IF EXISTS federation_sections_public_content_check;
ALTER TABLE federation_sections ADD CONSTRAINT federation_sections_public_content_check
    CHECK (jsonb_typeof(public_content) = 'object' OR jsonb_typeof(public_content) = 'array');

ALTER TABLE leadership_profiles
    ADD COLUMN IF NOT EXISTS source_key TEXT,
    ADD COLUMN IF NOT EXISTS photo_url TEXT NOT NULL DEFAULT '';
CREATE UNIQUE INDEX IF NOT EXISTS leadership_profiles_source_key_uq
    ON leadership_profiles(source_key) WHERE source_key IS NOT NULL;
