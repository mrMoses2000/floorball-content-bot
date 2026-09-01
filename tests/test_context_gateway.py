from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from floorball_bot.context_gateway import (
    AgentContextGateway,
    AgentMode,
    CityIdentity,
    LocalizedValue,
    assert_agent_context_safe,
    context_schema_catalog,
    redact_agent_value,
)
from floorball_bot.db import create_pool, run_migrations
from floorball_bot.domain import Actor, Role
from floorball_bot.errors import AuthorizationError


def _actor(*roles: Role, scopes=()) -> Actor:
    return Actor(
        user_id=uuid4(),
        telegram_id=100,
        roles=frozenset(roles),
        city_scopes=frozenset(scopes),
    )


def test_context_redactor_removes_nested_identifiers_and_contacts():
    value = {
        "statement": "Развивать флорбол",
        "phone": "+77000000000",
        "nested": {
            "telegramId": 123,
            "contactName": "Private person",
            "profileId": str(uuid4()),
            "sourceUrl": "https://example.test/source",
        },
        "unlabeled": "private@example.test / +7 (700) 000-00-00",
    }

    redacted = redact_agent_value(value)

    assert redacted == {
        "statement": "Развивать флорбол",
        "nested": {"sourceUrl": "https://example.test/source"},
        "unlabeled": "[redacted-contact] / [redacted-contact]",
    }
    assert_agent_context_safe(redacted)


def test_context_catalog_maps_only_canonical_sources_to_site_contract():
    catalog = context_schema_catalog()
    payload = catalog.model_dump(mode="json")

    assert {mapping["mode"] for mapping in payload["mappings"]} == {
        "trainer",
        "strategy",
        "history",
        "leadership",
        "news",
    }
    assert any(
        "cities[].clubs_list[]" in path
        for mapping in payload["mappings"]
        for path in mapping["site_paths"]
    )
    assert any(
        field["db_field"] == "cities.players_estimate/coaches_estimate/clubs_estimate"
        and field["site_field"] == "cities[].players/coaches/clubs"
        for mapping in payload["mappings"]
        for field in mapping["fields"]
    )
    assert "phone_private" not in json.dumps(payload)
    assert "email_private" not in json.dumps(payload)


def test_context_models_reject_unexpected_private_fields():
    with pytest.raises(ValidationError):
        CityIdentity.model_validate(
            {
                "slug": "almaty",
                "name": LocalizedValue(ru="Алматы"),
                "locative": LocalizedValue(),
                "revision": 1,
                "updated_at": "2026-08-25T00:00:00Z",
                "city_id": str(uuid4()),
            }
        )


@pytest.fixture
async def context_pg_pool():
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not configured")
    pool = await create_pool(dsn)
    await run_migrations(pool, Path(__file__).parents[1] / "migrations")
    await pool.execute(
        "TRUNCATE users, cities, jobs, outbox_events, processed_updates "
        "RESTART IDENTITY CASCADE"
    )
    yield pool
    await pool.close()


async def _seed_city_context(pool):
    city_id = await pool.fetchval(
        """
        INSERT INTO cities(
            slug, name_ru, name_kz, name_en, locative_ru, locative_kz, locative_en,
            region, longitude, latitude, players_estimate, coaches_estimate, clubs_estimate,
            data_status
        ) VALUES (
            'almaty', 'Алматы', 'Алматы', 'Almaty', 'Алматы', 'Алматыда', 'Almaty',
            'Алматы', 76.95, 43.25, 42, 3, 2, 'verified-coach-data'
        ) RETURNING id
        """
    )
    await pool.execute(
        """
        INSERT INTO city_content(
            city_id, description_ru, description_kz, history_ru, sources
        ) VALUES (
            $1, 'Описание. Внутренний телефон +7 (700) 000-00-00',
            'Сипаттама', 'История', '[{"url":"https://example.test"}]'
        )
        """,
        city_id,
    )
    club_id = await pool.fetchval(
        """
        INSERT INTO clubs(
            city_id, name, age_groups, notes, contact_name,
            contact_phone_private, contact_is_public
        ) VALUES ($1, 'Алматы Флорбол', '["U-13"]', 'Детская секция',
                  'Private Coach', '+77000000000', FALSE)
        RETURNING id
        """,
        city_id,
    )
    await pool.execute(
        """
        INSERT INTO training_schedules(city_id, club_id, day, time_text, venue, address)
        VALUES ($1,$2,'monday','18:00','Спортзал','ул. Тестовая')
        """,
        city_id,
        club_id,
    )
    await pool.execute(
        "INSERT INTO coaches(city_id, club_id, name_ru) VALUES ($1,$2,'Тренер')",
        city_id,
        club_id,
    )
    player_id = await pool.fetchval(
        """
        INSERT INTO players(
            name_ru, bio_ru, selected_for_publication, approved_for_publication
        ) VALUES ('Игрок','Описание',TRUE,TRUE) RETURNING id
        """
    )
    await pool.execute(
        """
        INSERT INTO player_city_memberships(player_id, city_id, club_id, valid_from)
        VALUES ($1,$2,$3,current_date)
        """,
        player_id,
        city_id,
        club_id,
    )
    return city_id


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_trainer_context_is_scoped_typed_and_private_data_free(context_pg_pool):
    city_id = await _seed_city_context(context_pg_pool)
    gateway = AgentContextGateway(context_pg_pool)

    snapshot = await gateway.snapshot(
        actor=_actor(Role.CITY_COACH, scopes=[city_id]),
        mode=AgentMode.TRAINER,
        city_slug="almaty",
    )

    assert snapshot.access == "full"
    assert snapshot.narrative is not None
    assert snapshot.narrative.description.ru == "Описание. Внутренний телефон [redacted-contact]"
    assert snapshot.site_metrics.players == 42
    assert snapshot.site_metrics.coaches == 3
    assert snapshot.site_metrics.clubs == 2
    assert snapshot.records.registered_clubs == 1
    assert snapshot.records.registered_coaches == 1
    assert snapshot.records.registered_players == 1
    assert snapshot.clubs[0].public_contact_ready is False
    assert snapshot.schedules[0].venue == "Спортзал"
    serialized = json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False)
    assert "+77000000000" not in serialized
    assert "Private Coach" not in serialized
    assert str(city_id) not in serialized


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_trainer_context_rejects_unscoped_city_coach(context_pg_pool):
    await _seed_city_context(context_pg_pool)
    gateway = AgentContextGateway(context_pg_pool)

    with pytest.raises(AuthorizationError):
        await gateway.snapshot(
            actor=_actor(Role.CITY_COACH),
            mode=AgentMode.TRAINER,
            city_slug="almaty",
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_city_directory_contains_only_public_labels_and_honors_scope(context_pg_pool):
    city_id = await _seed_city_context(context_pg_pool)
    await context_pg_pool.execute(
        """
        INSERT INTO cities(slug, name_ru, name_kz, name_en)
        VALUES ('astana','Астана','Астана','Astana')
        """
    )
    gateway = AgentContextGateway(context_pg_pool)

    scoped = await gateway.snapshot(
        actor=_actor(Role.CITY_COACH, scopes=[city_id]), mode=AgentMode.TRAINER
    )
    public = await gateway.snapshot(actor=_actor(Role.COACH_FORM), mode=AgentMode.TRAINER)

    assert [city.slug for city in scoped.cities] == ["almaty"]
    assert {city.slug for city in public.cities} == {"almaty", "astana"}
    serialized = json.dumps(public.model_dump(mode="json"), ensure_ascii=False)
    assert str(city_id) not in serialized
    assert "region" not in serialized
    assert "longitude" not in serialized


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_coach_form_gets_coverage_without_internal_city_content(context_pg_pool):
    await _seed_city_context(context_pg_pool)
    snapshot = await AgentContextGateway(context_pg_pool).snapshot(
        actor=_actor(Role.COACH_FORM),
        mode=AgentMode.TRAINER,
        city_slug="almaty",
    )

    assert snapshot.access == "coverage_only"
    assert snapshot.narrative is None
    assert snapshot.clubs == []
    assert snapshot.schedules == []
    assert snapshot.records.registered_clubs == 1
    assert "description_ru" in snapshot.coverage.available_fields


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_federation_context_redacts_json_and_leadership_contacts(context_pg_pool):
    await context_pg_pool.execute(
        """
        INSERT INTO federation_sections(section_key, content_ru, content_kz, public_content)
        VALUES (
            'mission',
            '{"statement":"Развивать флорбол","phone":"+77000000000",'
                '"nested":{"contactName":"Private"}}',
            '{"statement":"Флорболды дамыту"}',
            '{"statementRu":"Развивать флорбол","email":"private@example.test"}'
        )
        """
    )
    await context_pg_pool.execute(
        """
        INSERT INTO leadership_profiles(
            name_ru, name_kz, role_ru, role_kz, email_private, phone_private,
            contacts_are_public
        ) VALUES (
            'Руководитель', 'Басшы', 'Президент', 'Президент',
            'private@example.test', '+77000000000', TRUE
        )
        """
    )
    gateway = AgentContextGateway(context_pg_pool)
    actor = _actor(Role.FEDERATION_EDITOR)

    strategy = await gateway.snapshot(actor=actor, mode=AgentMode.STRATEGY)
    leadership = await gateway.snapshot(actor=actor, mode=AgentMode.LEADERSHIP)

    assert strategy.sections[0].content["ru"] == {"statement": "Развивать флорбол"}
    assert strategy.sections[0].public_content == {"statementRu": "Развивать флорбол"}
    assert leadership.profiles[0].public_contact_ready is True
    serialized = json.dumps(
        {
            "strategy": strategy.model_dump(mode="json"),
            "leadership": leadership.model_dump(mode="json"),
        },
        ensure_ascii=False,
    )
    assert "+77000000000" not in serialized
    assert "private@example.test" not in serialized


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_federation_mode_rejects_coach_form_role(context_pg_pool):
    with pytest.raises(AuthorizationError):
        await AgentContextGateway(context_pg_pool).snapshot(
            actor=_actor(Role.COACH_FORM), mode=AgentMode.HISTORY
        )
