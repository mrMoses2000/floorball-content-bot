ALTER TABLE roles DROP CONSTRAINT IF EXISTS roles_name_check;
ALTER TABLE roles
    ADD CONSTRAINT roles_name_check
    CHECK (name IN (
        'superadmin', 'reviewer', 'federation_editor', 'city_coach', 'coach_form', 'media_editor'
    ));

INSERT INTO roles(name, description)
VALUES ('coach_form', 'Coach questionnaire only; no content or publication access')
ON CONFLICT (name) DO NOTHING;

ALTER TABLE conversation_sessions DROP CONSTRAINT IF EXISTS conversation_sessions_workflow_check;
ALTER TABLE conversation_sessions
    ADD CONSTRAINT conversation_sessions_workflow_check
    CHECK (workflow IN ('city', 'federation', 'review', 'publish', 'user_admin', 'coach_form'));
