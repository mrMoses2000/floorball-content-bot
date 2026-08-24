CREATE TABLE IF NOT EXISTS schema_migrations (
    filename TEXT PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    phone_e164 TEXT NOT NULL UNIQUE CHECK (phone_e164 ~ '^\+[1-9][0-9]{7,14}$'),
    display_name TEXT NOT NULL CHECK (char_length(display_name) BETWEEN 1 AND 180),
    telegram_id BIGINT UNIQUE,
    telegram_bound_at TIMESTAMPTZ,
    preferred_language TEXT NOT NULL DEFAULT 'ru' CHECK (preferred_language IN ('ru','kz')),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS roles (
    name TEXT PRIMARY KEY CHECK (name IN ('superadmin','reviewer','federation_editor','city_coach','media_editor')),
    description TEXT NOT NULL DEFAULT ''
);
INSERT INTO roles(name, description) VALUES
    ('superadmin','Full administration and publication'),
    ('reviewer','Content review and approval'),
    ('federation_editor','Federation sections editor'),
    ('city_coach','Assigned city editor'),
    ('media_editor','Media moderation')
ON CONFLICT (name) DO NOTHING;

CREATE TABLE IF NOT EXISTS user_roles (
    user_id UUID NOT NULL REFERENCES users(id),
    role_name TEXT NOT NULL REFERENCES roles(name),
    granted_by UUID REFERENCES users(id),
    granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ,
    PRIMARY KEY (user_id, role_name)
);

CREATE TABLE IF NOT EXISTS cities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug TEXT NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9-]{1,80}$'),
    name_ru TEXT NOT NULL DEFAULT '' CHECK (char_length(name_ru) <= 120),
    name_kz TEXT NOT NULL DEFAULT '' CHECK (char_length(name_kz) <= 120),
    name_en TEXT NOT NULL DEFAULT '' CHECK (char_length(name_en) <= 120),
    locative_ru TEXT NOT NULL DEFAULT '' CHECK (char_length(locative_ru) <= 120),
    locative_kz TEXT NOT NULL DEFAULT '' CHECK (char_length(locative_kz) <= 120),
    locative_en TEXT NOT NULL DEFAULT '' CHECK (char_length(locative_en) <= 120),
    region TEXT NOT NULL DEFAULT '' CHECK (char_length(region) <= 180),
    longitude DOUBLE PRECISION,
    latitude DOUBLE PRECISION,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by UUID REFERENCES users(id),
    updated_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    CHECK ((longitude IS NULL AND latitude IS NULL) OR (longitude BETWEEN -180 AND 180 AND latitude BETWEEN -90 AND 90))
);

CREATE TABLE IF NOT EXISTS user_city_scopes (
    user_id UUID NOT NULL REFERENCES users(id),
    city_id UUID NOT NULL REFERENCES cities(id),
    granted_by UUID REFERENCES users(id),
    granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ,
    PRIMARY KEY (user_id, city_id)
);

CREATE TABLE IF NOT EXISTS clubs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    city_id UUID NOT NULL REFERENCES cities(id),
    name TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 180),
    age_groups JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(age_groups) = 'array'),
    notes TEXT NOT NULL DEFAULT '' CHECK (char_length(notes) <= 600),
    contact_name TEXT NOT NULL DEFAULT '' CHECK (char_length(contact_name) <= 180),
    contact_phone_private TEXT NOT NULL DEFAULT '' CHECK (char_length(contact_phone_private) <= 80),
    contact_is_public BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','inactive')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by UUID REFERENCES users(id),
    updated_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS coaches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id),
    club_id UUID REFERENCES clubs(id),
    city_id UUID NOT NULL REFERENCES cities(id),
    name_ru TEXT NOT NULL DEFAULT '' CHECK (char_length(name_ru) <= 160),
    name_kz TEXT NOT NULL DEFAULT '' CHECK (char_length(name_kz) <= 160),
    bio_ru TEXT NOT NULL DEFAULT '' CHECK (char_length(bio_ru) <= 900),
    bio_kz TEXT NOT NULL DEFAULT '' CHECK (char_length(bio_kz) <= 900),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','inactive')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS training_schedules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    city_id UUID NOT NULL REFERENCES cities(id),
    club_id UUID REFERENCES clubs(id),
    day TEXT NOT NULL CHECK (day IN ('monday','tuesday','wednesday','thursday','friday','saturday','sunday')),
    time_text TEXT NOT NULL CHECK (char_length(time_text) BETWEEN 1 AND 80),
    venue TEXT NOT NULL CHECK (char_length(venue) BETWEEN 1 AND 240),
    address TEXT NOT NULL DEFAULT '' CHECK (char_length(address) <= 300),
    group_name TEXT NOT NULL DEFAULT '' CHECK (char_length(group_name) <= 180),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by UUID REFERENCES users(id),
    updated_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS players (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name_ru TEXT NOT NULL DEFAULT '' CHECK (char_length(name_ru) <= 120),
    name_kz TEXT NOT NULL DEFAULT '' CHECK (char_length(name_kz) <= 120),
    name_en TEXT NOT NULL DEFAULT '' CHECK (char_length(name_en) <= 120),
    position_ru TEXT NOT NULL DEFAULT '' CHECK (char_length(position_ru) <= 120),
    position_kz TEXT NOT NULL DEFAULT '' CHECK (char_length(position_kz) <= 120),
    position_en TEXT NOT NULL DEFAULT '' CHECK (char_length(position_en) <= 120),
    bio_ru TEXT NOT NULL DEFAULT '' CHECK (char_length(bio_ru) <= 900),
    bio_kz TEXT NOT NULL DEFAULT '' CHECK (char_length(bio_kz) <= 900),
    bio_en TEXT NOT NULL DEFAULT '' CHECK (char_length(bio_en) <= 900),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','inactive')),
    selected_for_publication BOOLEAN NOT NULL DEFAULT FALSE,
    approved_for_publication BOOLEAN NOT NULL DEFAULT FALSE,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by UUID REFERENCES users(id),
    updated_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS player_city_memberships (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    player_id UUID NOT NULL REFERENCES players(id),
    city_id UUID NOT NULL REFERENCES cities(id),
    club_id UUID REFERENCES clubs(id),
    valid_from DATE NOT NULL,
    valid_to DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);

CREATE TABLE IF NOT EXISTS city_content (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    city_id UUID NOT NULL UNIQUE REFERENCES cities(id),
    description_ru TEXT NOT NULL DEFAULT '' CHECK (char_length(description_ru) <= 1200),
    description_kz TEXT NOT NULL DEFAULT '' CHECK (char_length(description_kz) <= 1200),
    description_en TEXT NOT NULL DEFAULT '' CHECK (char_length(description_en) <= 1200),
    history_ru TEXT NOT NULL DEFAULT '' CHECK (char_length(history_ru) <= 2400),
    history_kz TEXT NOT NULL DEFAULT '' CHECK (char_length(history_kz) <= 2400),
    history_en TEXT NOT NULL DEFAULT '' CHECK (char_length(history_en) <= 2400),
    sources JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(sources) = 'array'),
    hero_media_id UUID,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by UUID REFERENCES users(id),
    updated_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS federation_sections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    section_key TEXT NOT NULL CHECK (section_key IN ('mission','vision','values','goals','history','achievements','roadmap','partnerships','youth','women','coaching','refereeing','competitions','international','infrastructure')),
    item_key TEXT NOT NULL DEFAULT 'main',
    content_ru JSONB NOT NULL DEFAULT '{}'::jsonb,
    content_kz JSONB NOT NULL DEFAULT '{}'::jsonb,
    content_en JSONB NOT NULL DEFAULT '{}'::jsonb,
    sources JSONB NOT NULL DEFAULT '[]'::jsonb,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by UUID REFERENCES users(id),
    updated_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    UNIQUE (section_key, item_key)
);

CREATE TABLE IF NOT EXISTS leadership_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name_ru TEXT NOT NULL DEFAULT '' CHECK (char_length(name_ru) <= 160),
    name_kz TEXT NOT NULL DEFAULT '' CHECK (char_length(name_kz) <= 160),
    role_ru TEXT NOT NULL DEFAULT '' CHECK (char_length(role_ru) <= 160),
    role_kz TEXT NOT NULL DEFAULT '' CHECK (char_length(role_kz) <= 160),
    bio_ru TEXT NOT NULL DEFAULT '' CHECK (char_length(bio_ru) <= 1200),
    bio_kz TEXT NOT NULL DEFAULT '' CHECK (char_length(bio_kz) <= 1200),
    focus_ru TEXT NOT NULL DEFAULT '' CHECK (char_length(focus_ru) <= 500),
    focus_kz TEXT NOT NULL DEFAULT '' CHECK (char_length(focus_kz) <= 500),
    email_private TEXT NOT NULL DEFAULT '',
    phone_private TEXT NOT NULL DEFAULT '',
    contacts_are_public BOOLEAN NOT NULL DEFAULT FALSE,
    media_id UUID,
    sort_order INTEGER NOT NULL DEFAULT 0,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by UUID REFERENCES users(id),
    updated_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ
);

