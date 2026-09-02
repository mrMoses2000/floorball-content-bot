ALTER TABLE messages ADD COLUMN IF NOT EXISTS telegram_media_group_id TEXT NOT NULL DEFAULT ''
    CHECK (char_length(telegram_media_group_id) <= 128);

CREATE TABLE IF NOT EXISTS news_session_media (
    session_id UUID NOT NULL REFERENCES conversation_sessions(id) ON DELETE CASCADE,
    media_id UUID NOT NULL REFERENCES media_assets(id),
    message_id UUID REFERENCES messages(id) ON DELETE SET NULL,
    telegram_message_id BIGINT NOT NULL,
    telegram_media_group_id TEXT NOT NULL DEFAULT ''
        CHECK (char_length(telegram_media_group_id) <= 128),
    caption TEXT NOT NULL DEFAULT '' CHECK (char_length(caption) <= 600),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, media_id)
);

CREATE INDEX IF NOT EXISTS news_session_media_order_idx
    ON news_session_media(session_id, telegram_message_id, media_id);

CREATE TABLE IF NOT EXISTS news_media_items (
    news_id UUID NOT NULL REFERENCES news_items(id) ON DELETE CASCADE,
    media_id UUID NOT NULL REFERENCES media_assets(id),
    alt_ru TEXT NOT NULL CHECK (char_length(alt_ru) BETWEEN 1 AND 300),
    alt_kz TEXT NOT NULL CHECK (char_length(alt_kz) BETWEEN 1 AND 300),
    alt_en TEXT NOT NULL DEFAULT '' CHECK (char_length(alt_en) <= 300),
    sort_order INTEGER NOT NULL DEFAULT 0 CHECK (sort_order >= 0),
    selected_for_publication BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (news_id, media_id)
);

CREATE INDEX IF NOT EXISTS news_media_items_public_idx
    ON news_media_items(news_id, sort_order, media_id)
    WHERE selected_for_publication=TRUE;
