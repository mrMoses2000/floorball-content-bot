CREATE TABLE IF NOT EXISTS runtime_heartbeats (
    component TEXT PRIMARY KEY CHECK (component IN ('telegram_ingress', 'worker')),
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object')
);

CREATE INDEX IF NOT EXISTS runtime_heartbeats_observed_idx
    ON runtime_heartbeats(observed_at);
