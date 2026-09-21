ALTER TABLE telegram_start_intents
    DROP CONSTRAINT IF EXISTS telegram_start_intents_start_parameter_check;
ALTER TABLE telegram_start_intents
    ADD CONSTRAINT telegram_start_intents_start_parameter_check
    CHECK (start_parameter IN ('new_city','coach'));

CREATE TABLE IF NOT EXISTS coach_onboarding_contact_attempts (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL,
    contact_user_id BIGINT,
    succeeded BOOLEAN NOT NULL,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS coach_onboarding_contact_attempts_rate_idx
    ON coach_onboarding_contact_attempts(telegram_id, attempted_at DESC);
