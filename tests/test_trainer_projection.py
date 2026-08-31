from floorball_bot.projection.trainer import CityDirectoryEntry, plan_trainer_projection


def _complete_fields():
    return {
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
            "summary": "Регулярные тренировки для детей и взрослых.",
            "history": "Подтверждённая история городского флорбола.",
        },
        "metrics": {
            "players_total": 40,
            "coaches_total": 3,
            "clubs_total": 1,
            "data_confidence": 4,
        },
        "clubs": [
            {
                "name": "Тестовый клуб",
                "contact_name": "Контактное лицо",
                "contact_phone": "+77001112233",
                "public_contact": "нет",
                "age_groups": "U13, U18",
                "notes": "Тестовая заметка",
            }
        ],
        "schedule": [
            {
                "day": "monday",
                "time": "18:00",
                "venue": "Спортивный зал",
                "address": "Тестовый адрес",
                "group": "U13",
                "public_permission": "да",
            }
        ],
        "media": {"permission": "нет", "minors_permission": "нет"},
        "accuracy_confirmed": True,
        "publication_permission": True,
    }


def _directory():
    return (
        CityDirectoryEntry(
            slug="almaty",
            name_ru="Алматы",
            name_kz="Алматы",
            name_en="Almaty",
        ),
    )


def test_projection_plan_maps_existing_city_without_exposing_private_contacts():
    plan = plan_trainer_projection(
        _complete_fields(),
        preferred_language="ru",
        city_directory=_directory(),
    )

    assert plan.ready
    assert plan.city_slug == "almaty"
    assert plan.description_ru == "Регулярные тренировки для детей и взрослых."
    assert plan.description_kz is None
    assert plan.players_estimate == 40
    assert plan.clubs[0].contact_phone_private == "+77001112233"
    assert not plan.clubs[0].contact_is_public

    public = plan.public_preview()
    assert public["slug"] == "almaty"
    assert public["clubs_list"][0]["contactPhone"] == ""
    assert "+77001112233" not in str(public)


def test_projection_plan_blocks_refused_required_consents():
    fields = _complete_fields()
    fields["accuracy_confirmed"] = False
    fields["publication_permission"] = False

    plan = plan_trainer_projection(
        fields,
        preferred_language="ru",
        city_directory=_directory(),
    )

    assert not plan.ready
    assert "accuracy_confirmed" in plan.blockers
    assert "publication_permission" in plan.blockers


def test_projection_plan_routes_unknown_city_to_application_instead_of_inventing_slug():
    fields = _complete_fields()
    fields["city"] = {
        **fields["city"],
        "name": "другой",
        "other_name": "Конаев",
    }

    plan = plan_trainer_projection(
        fields,
        preferred_language="ru",
        city_directory=_directory(),
    )

    assert not plan.ready
    assert plan.city_slug is None
    assert "new_city_application_required" in plan.blockers
    assert plan.proposed_city_name == "Конаев"


def test_projection_plan_excludes_schedule_without_public_permission():
    fields = _complete_fields()
    fields["schedule"][0]["public_permission"] = "нет"

    plan = plan_trainer_projection(
        fields,
        preferred_language="kz",
        city_directory=_directory(),
    )

    assert plan.ready
    assert plan.description_kz == "Регулярные тренировки для детей и взрослых."
    assert plan.description_ru is None
    assert plan.schedules == ()
    assert "schedule[0].not_public" in plan.warnings

