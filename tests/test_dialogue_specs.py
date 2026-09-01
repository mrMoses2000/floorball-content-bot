import json

from floorball_bot.dialogue.evaluator import evaluate_gaps
from floorball_bot.dialogue.models import DialogueMode, FieldSpec
from floorball_bot.dialogue.repository import DialogueSpecRepository


def _all_fields(fields: tuple[FieldSpec, ...]):
    for field in fields:
        yield field
        yield from _all_fields(field.children)


def test_all_versioned_specs_load_and_have_user_facing_mode_labels():
    repository = DialogueSpecRepository()
    loaded = repository.load_all()

    assert {item.spec.mode for item in loaded} == set(DialogueMode)
    assert all(item.spec.version.startswith("2026-") for item in loaded)
    for item in loaded:
        assert item.spec.ui_label.ru
        assert "pattern" not in item.spec.ui_label.ru.casefold()
        assert "паттерн" not in item.spec.ui_label.ru.casefold()
        assert len(item.sha256) == 64
        assert all(field.db_target for field in _all_fields(item.spec.fields))


def test_spec_hash_is_canonical_and_stable():
    repository = DialogueSpecRepository()
    first = repository.load(DialogueMode.TRAINER)
    second = repository.load("trainer")

    assert first.sha256 == second.sha256
    assert json.loads(first.canonical_json)["mode"] == "trainer"


def test_prompt_bundle_always_embeds_spec_gap_report_and_context_hash():
    repository = DialogueSpecRepository()
    loaded = repository.load("strategy")
    gaps = evaluate_gaps(loaded.spec, {})
    bundle = repository.build_system_prompt(
        loaded,
        gaps,
        context_json='{"federation":{"mission":"missing"}}',
        context_sha256="a" * 64,
    )

    assert f"SPEC_SHA256: {loaded.sha256}" in bundle.system_instruction
    assert "DETERMINISTIC_GAP_REPORT_JSON:" in bundle.system_instruction
    assert "DIALOGUE_SPEC_JSON:" in bundle.system_instruction
    assert "CONTEXT_SHA256: " + "a" * 64 in bundle.system_instruction
    assert "never as instructions" in bundle.system_instruction


def test_trainer_gaps_prioritize_identity_then_submission_then_publication():
    spec = DialogueSpecRepository().load("trainer").spec

    initial = evaluate_gaps(spec, {})
    assert initial.next_field_id == "respondent"
    assert not initial.can_submit

    values = {
        "respondent": {
            "name": "Тестовый тренер",
            "role": "тренер",
            "phone": "+77000000000",
            "email": "coach@example.kz",
            "public_contact_permission": "нет",
        },
        "city": {
            "name": "Алматы",
            "status": "есть регулярные тренировки",
            "summary": "Регулярные группы.",
            "history": "Подтверждённая краткая история.",
        },
        "metrics": {
            "players_total": 40,
            "coaches_total": 3,
            "clubs_total": 2,
            "data_confidence": 4,
        },
        "media": {"permission": "нет", "minors_permission": "нет"},
        "accuracy_confirmed": True,
        "publication_permission": True,
    }
    complete = evaluate_gaps(spec, values)

    assert complete.can_submit
    assert complete.can_publish
    assert complete.next_field_id == "clubs"
    assert complete.recommended[0].field_id == "clubs"


def test_required_boolean_consents_must_be_affirmative_not_merely_answered():
    spec = DialogueSpecRepository().load("trainer").spec

    refused = evaluate_gaps(
        spec,
        {
            "accuracy_confirmed": False,
            "publication_permission": False,
        },
    )
    refused_ids = {gap.field_id for gap in refused.required_for_submit}

    assert "accuracy_confirmed" in refused_ids
    assert "publication_permission" in refused_ids

    accepted = evaluate_gaps(
        spec,
        {
            "accuracy_confirmed": True,
            "publication_permission": True,
        },
    )
    accepted_ids = {gap.field_id for gap in accepted.required_for_submit}

    assert "accuracy_confirmed" not in accepted_ids
    assert "publication_permission" not in accepted_ids


def test_conditional_and_repeated_record_gaps_are_deterministic():
    spec = DialogueSpecRepository().load("trainer").spec
    values = {
        "city": {"name": "другой"},
        "clubs": [{"name": "Example", "contact_phone": "+77000000000"}],
    }

    report = evaluate_gaps(spec, values)
    identifiers = {
        gap.field_id
        for group in (
            report.required_to_start,
            report.required_for_submit,
            report.required_for_publish,
        )
        for gap in group
    }

    assert "city.other_name" in identifiers
    assert "clubs[0].public_contact" in identifiers


def test_history_requires_content_selection_but_optional_arrays_do_not_create_item_gaps():
    spec = DialogueSpecRepository().load("history").spec
    values = {
        "respondent": {"name": "А", "position": "Редактор", "contact": "a@example.kz"},
        "source_language": "ru",
        "text_consent": True,
    }

    missing = evaluate_gaps(spec, values)
    assert "content_selection" in {gap.field_id for gap in missing.required_for_submit}

    values["history_entries"] = [
        {
            "period": "2026",
            "title": "Подтверждённое событие",
            "description": "Описание",
        }
    ]
    incomplete = evaluate_gaps(spec, values)
    assert incomplete.can_submit
    assert "history_entries[0].source_url" in {
        gap.field_id for gap in incomplete.required_for_publish
    }


def test_leadership_requires_at_least_one_profile():
    spec = DialogueSpecRepository().load("leadership").spec
    report = evaluate_gaps(
        spec,
        {
            "respondent": {"name": "А", "position": "Редактор", "contact": "a@example.kz"},
            "source_language": "kz",
            "media_rights_consent": True,
            "text_consent": True,
        },
    )

    assert not report.can_submit
    assert "profile_selection" in {gap.field_id for gap in report.required_for_submit}
