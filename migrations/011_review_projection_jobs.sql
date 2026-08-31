ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK (kind IN (
    'transcribe', 'extract', 'media', 'reply', 'reconcile', 'publish_preview',
    'publish_confirm', 'readiness_scan', 'apply_projection'
));
