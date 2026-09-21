CREATE TABLE IF NOT EXISTS official_document_requirements (
    code TEXT PRIMARY KEY CHECK (code ~ '^[a-z][a-z0-9_]{1,79}$'),
    title_ru TEXT NOT NULL CHECK (char_length(title_ru) BETWEEN 1 AND 240),
    title_kz TEXT NOT NULL CHECK (char_length(title_kz) BETWEEN 1 AND 240),
    required BOOLEAN NOT NULL DEFAULT TRUE,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO official_document_requirements(
    code, title_ru, title_kz, required, sort_order
) VALUES
    ('federation_charter', 'Устав федерации', 'Федерация жарғысы', TRUE, 10),
    ('federation_regulations', 'Положение о федерации', 'Федерация туралы ереже', TRUE, 20),
    ('game_rules', 'Правила игры и судейства', 'Ойын және төрешілік ережелері', TRUE, 30),
    ('competition_regulations', 'Регламент соревнований', 'Жарыстар регламенті', TRUE, 40),
    ('venue_equipment_requirements', 'Требования к площадке и инвентарю', 'Алаң мен құрал-жабдық талаптары', TRUE, 50),
    ('player_registration', 'Порядок регистрации игроков', 'Ойыншыларды тіркеу тәртібі', TRUE, 60),
    ('disciplinary_regulations', 'Дисциплинарное положение', 'Тәртіптік ереже', TRUE, 70),
    ('other', 'Другой официальный документ', 'Басқа ресми құжат', FALSE, 1000)
ON CONFLICT (code) DO UPDATE SET
    title_ru=EXCLUDED.title_ru,
    title_kz=EXCLUDED.title_kz,
    required=EXCLUDED.required,
    sort_order=EXCLUDED.sort_order,
    updated_at=now();

CREATE TABLE IF NOT EXISTS official_documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    requirement_code TEXT NOT NULL REFERENCES official_document_requirements(code),
    title_ru TEXT NOT NULL CHECK (char_length(title_ru) BETWEEN 1 AND 240),
    title_kz TEXT NOT NULL DEFAULT '' CHECK (char_length(title_kz) <= 240),
    document_number TEXT NOT NULL DEFAULT '' CHECK (char_length(document_number) <= 180),
    issued_on DATE,
    valid_until DATE,
    original_filename TEXT NOT NULL CHECK (char_length(original_filename) BETWEEN 1 AND 255),
    detected_mime TEXT NOT NULL DEFAULT 'application/pdf'
        CHECK (detected_mime='application/pdf'),
    byte_size BIGINT NOT NULL CHECK (byte_size BETWEEN 1 AND 20971520),
    sha256 TEXT NOT NULL UNIQUE CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    original_path TEXT NOT NULL CHECK (char_length(original_path) BETWEEN 1 AND 2000),
    publication_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL DEFAULT 'metadata_pending' CHECK (
        status IN ('metadata_pending','received','verified','rejected','superseded')
    ),
    uploaded_by UUID NOT NULL REFERENCES users(id),
    reviewed_by UUID REFERENCES users(id),
    reviewed_at TIMESTAMPTZ,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (valid_until IS NULL OR issued_on IS NULL OR valid_until >= issued_on)
);
CREATE INDEX IF NOT EXISTS official_documents_requirement_idx
    ON official_documents(requirement_code, status, valid_until, created_at DESC);

CREATE TABLE IF NOT EXISTS official_document_upload_sessions (
    user_id UUID PRIMARY KEY REFERENCES users(id),
    requirement_code TEXT NOT NULL REFERENCES official_document_requirements(code),
    document_id UUID REFERENCES official_documents(id),
    current_step TEXT NOT NULL DEFAULT 'awaiting_file' CHECK (
        current_step IN (
            'awaiting_file','document_number','issued_on','valid_until',
            'publication_permission','completed','cancelled'
        )
    ),
    status TEXT NOT NULL DEFAULT 'active' CHECK (
        status IN ('active','completed','cancelled')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS official_document_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID REFERENCES official_documents(id),
    requirement_code TEXT NOT NULL REFERENCES official_document_requirements(code),
    actor_id UUID REFERENCES users(id),
    action TEXT NOT NULL CHECK (
        action IN (
            'upload_started','file_received','metadata_updated','received',
            'verified','rejected','superseded','reminder_sent'
        )
    ),
    details JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(details)='object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS official_document_events_requirement_idx
    ON official_document_events(requirement_code, created_at DESC);
