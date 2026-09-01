CREATE TABLE IF NOT EXISTS contact_requests (
    id UUID PRIMARY KEY,
    contract_version INTEGER NOT NULL CHECK (contract_version = 1),
    payload_hash TEXT NOT NULL CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    locale TEXT NOT NULL CHECK (locale IN ('ru','kz','en')),
    name TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 160),
    reply_to TEXT NOT NULL CHECK (char_length(reply_to) BETWEEN 3 AND 254),
    subject TEXT NOT NULL CHECK (subject IN (
        'training','tournaments','club','partnership','other'
    )),
    message TEXT NOT NULL CHECK (char_length(message) BETWEEN 3 AND 5000),
    recipient TEXT NOT NULL DEFAULT 'Knff@gmail.com' CHECK (recipient = 'Knff@gmail.com'),
    client_fingerprint TEXT NOT NULL CHECK (client_fingerprint ~ '^[0-9a-f]{64}$'),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending','sending','retry','sent','dead'
    )),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_at TIMESTAMPTZ,
    locked_by TEXT,
    last_error TEXT NOT NULL DEFAULT '' CHECK (char_length(last_error) <= 2000),
    external_id TEXT NOT NULL DEFAULT '' CHECK (char_length(external_id) <= 500),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS contact_requests_rate_idx
    ON contact_requests(client_fingerprint, created_at DESC);

CREATE INDEX IF NOT EXISTS contact_requests_delivery_idx
    ON contact_requests(status, available_at, created_at)
    WHERE status IN ('pending','retry');

ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK (kind IN (
    'transcribe', 'extract', 'media', 'reply', 'reconcile', 'publish_preview',
    'publish_confirm', 'readiness_scan', 'apply_projection', 'contact_delivery'
));
