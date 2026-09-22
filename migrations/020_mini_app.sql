ALTER TABLE roles DROP CONSTRAINT IF EXISTS roles_name_check;
ALTER TABLE roles
    ADD CONSTRAINT roles_name_check
    CHECK (name IN (
        'superadmin', 'reviewer', 'federation_editor', 'city_coach',
        'coach_form', 'media_editor', 'player'
    ));

INSERT INTO roles(name, description)
VALUES ('player', 'Verified floorball player; no editorial or publication access')
ON CONFLICT (name) DO NOTHING;

ALTER TABLE players ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id);
CREATE UNIQUE INDEX IF NOT EXISTS players_user_id_unique_idx
    ON players(user_id) WHERE user_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS miniapp_mutations (
    request_id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id),
    session_id UUID NOT NULL REFERENCES conversation_sessions(id),
    action TEXT NOT NULL CHECK (action IN ('field_updated','session_started')),
    payload_hash TEXT NOT NULL CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    result_revision INTEGER NOT NULL CHECK (result_revision > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS miniapp_mutations_user_idx
    ON miniapp_mutations(user_id, created_at DESC);
