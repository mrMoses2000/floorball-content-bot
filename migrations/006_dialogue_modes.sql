ALTER TABLE conversation_sessions
    DROP CONSTRAINT IF EXISTS conversation_sessions_workflow_check;

ALTER TABLE conversation_sessions
    ADD CONSTRAINT conversation_sessions_workflow_check
    CHECK (workflow IN (
        'city', 'federation', 'review', 'publish', 'user_admin', 'coach_form',
        'trainer', 'strategy', 'history', 'leadership'
    ));

ALTER TABLE conversation_sessions
    ADD COLUMN IF NOT EXISTS definition_version TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS definition_hash TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS context_hash TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS subject_key TEXT NOT NULL DEFAULT '';

ALTER TABLE conversation_sessions
    DROP CONSTRAINT IF EXISTS conversation_sessions_definition_hash_check,
    DROP CONSTRAINT IF EXISTS conversation_sessions_context_hash_check,
    DROP CONSTRAINT IF EXISTS conversation_sessions_definition_version_check,
    DROP CONSTRAINT IF EXISTS conversation_sessions_subject_key_check;

ALTER TABLE conversation_sessions
    ADD CONSTRAINT conversation_sessions_definition_hash_check
        CHECK (definition_hash = '' OR definition_hash ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT conversation_sessions_context_hash_check
        CHECK (context_hash = '' OR context_hash ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT conversation_sessions_definition_version_check
        CHECK (char_length(definition_version) <= 40),
    ADD CONSTRAINT conversation_sessions_subject_key_check
        CHECK (char_length(subject_key) <= 160);

CREATE INDEX IF NOT EXISTS conversation_sessions_active_mode_idx
    ON conversation_sessions(user_id, workflow, subject_key, last_activity_at DESC)
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS agent_context_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES conversation_sessions(id),
    mode TEXT NOT NULL CHECK (mode IN ('trainer','strategy','history','leadership')),
    definition_hash TEXT NOT NULL CHECK (definition_hash ~ '^[0-9a-f]{64}$'),
    context_hash TEXT NOT NULL CHECK (context_hash ~ '^[0-9a-f]{64}$'),
    context JSONB NOT NULL CHECK (jsonb_typeof(context) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, context_hash)
);

CREATE INDEX IF NOT EXISTS agent_context_snapshots_session_idx
    ON agent_context_snapshots(session_id, created_at DESC);
