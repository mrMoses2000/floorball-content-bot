CREATE TABLE IF NOT EXISTS session_media_attachments (
    session_id UUID NOT NULL REFERENCES conversation_sessions(id),
    field_path TEXT NOT NULL CHECK (char_length(field_path) BETWEEN 1 AND 160),
    media_id UUID NOT NULL REFERENCES media_assets(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, field_path, media_id)
);
CREATE INDEX IF NOT EXISTS session_media_attachments_media_idx
    ON session_media_attachments(media_id);

ALTER TABLE miniapp_mutations DROP CONSTRAINT miniapp_mutations_action_check;
ALTER TABLE miniapp_mutations ADD CONSTRAINT miniapp_mutations_action_check
 CHECK (action IN ('field_updated','session_started','media_queued'));
