-- Privileged accounts can be provisioned from a verified Telegram identity when
-- their phone number is not known. Contact based binding still requires a phone.
ALTER TABLE users ALTER COLUMN phone_e164 DROP NOT NULL;
ALTER TABLE users ADD CONSTRAINT users_contact_identity_check
    CHECK (phone_e164 IS NOT NULL OR telegram_id IS NOT NULL);
