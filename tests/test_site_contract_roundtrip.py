import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from floorball_bot.db import create_pool, run_migrations
from floorball_bot.domain import CityPayload, FederationPayload
from floorball_bot.exporters import project_city_payload, project_federation_payload
from floorball_bot.importers import (
    apply_city_import,
    apply_federation_import,
    read_city_bundle,
    read_federation_bundle,
)

pytestmark = pytest.mark.postgres

GENERATED_AT = datetime(2026, 8, 25, tzinfo=UTC)
GENERATED_AT_TEXT = "2026-08-25T00:00:00.000Z"


@pytest.fixture
async def pg_pool():
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not configured")
    pool = await create_pool(dsn)
    await run_migrations(pool, Path(__file__).parents[1] / "migrations")
    await pool.execute(
        """
        TRUNCATE users, cities, federation_sections, leadership_profiles,
                 import_snapshots RESTART IDENTITY CASCADE
        """
    )
    yield pool
    await pool.close()


def city_bundle() -> dict:
    return {
        "ok": True,
        "version": 1,
        "generatedAt": GENERATED_AT_TEXT,
        "cities": [
            {
                "slug": "almaty",
                "nameRu": "Алматы",
                "nameKz": "Алматы",
                "nameEn": "Almaty",
                "locativeRu": "Алматы",
                "locativeKz": "Алматыда",
                "locativeEn": "Almaty",
                "region": "Алматы",
                "hero": "/assets/heroes/clubs.png",
                "geoCoords": [76.95, 43.25],
                "players": 42,
                "coaches": 3,
                "clubs": 1,
                "clubs_list": [
                    {
                        "name": "Almaty Floorball",
                        "ageGroups": ["U-13", "взрослые"],
                        "notes": "Регулярные тренировки",
                        "contactName": "Тренер",
                        "contactPhone": "+7 700 000 00 00",
                    }
                ],
                "schedule": [
                    {
                        "day": "tuesday",
                        "time": "19:00",
                        "venue": "Спортзал",
                        "address": "ул. Абая, 1",
                        "group": "Взрослые",
                    }
                ],
                "players_list": [
                    {
                        "id": "player-1",
                        "photo": "https://images.example.kz/player-1.webp",
                        "nameRu": "Арман Тестов",
                        "nameKz": "Арман Тестов",
                        "nameEn": "Arman Testov",
                        "positionRu": "Нападающий",
                        "positionKz": "Шабуылшы",
                        "positionEn": "Forward",
                        "bioRu": "Подтверждённый профиль.",
                        "bioKz": "Расталған профиль.",
                        "bioEn": "Verified profile.",
                    }
                ],
                "gallery": [
                    {
                        "id": "almaty-1",
                        "src": "https://images.example.kz/almaty-1.webp",
                        "thumbnail": "https://images.example.kz/almaty-1-thumb.webp",
                        "width": 1600,
                        "height": 900,
                        "altRu": "Тренировка",
                        "altKz": "Жаттығу",
                        "altEn": "Training",
                        "captionRu": "Командная тренировка",
                        "captionKz": "Командалық жаттығу",
                        "captionEn": "Team training",
                        "author": "Федерация",
                        "takenAt": "2026-08-20",
                    }
                ],
                "dataStatus": "verified-coach-data",
                "updatedAt": GENERATED_AT_TEXT,
                "descRu": "Проверенное описание.",
                "descKz": "Расталған сипаттама.",
                "descEn": "Verified description.",
                "historyRu": "Проверенная история.",
                "historyKz": "Расталған тарих.",
                "historyEn": "Verified history.",
            }
        ],
    }


def federation_bundle() -> dict:
    return {
        "ok": True,
        "version": 1,
        "generatedAt": GENERATED_AT_TEXT,
        "federation": {
            "mission": {
                "statementRu": "Развивать флорбол.",
                "statementKz": "Флорболды дамыту.",
                "visionRu": "Устойчивое сообщество.",
                "visionKz": "Тұрақты қауымдастық.",
                "values": [
                    {
                        "id": "value-1",
                        "titleRu": "Открытость",
                        "titleKz": "Ашықтық",
                        "descriptionRu": "Понятные решения.",
                        "descriptionKz": "Түсінікті шешімдер.",
                    }
                ],
                "goals": [
                    {
                        "id": "goal-1",
                        "titleRu": "Регионы",
                        "titleKz": "Өңірлер",
                        "descriptionRu": "Поддержать клубы.",
                        "descriptionKz": "Клубтарды қолдау.",
                        "kpiRu": "7 городов",
                        "kpiKz": "7 қала",
                        "ownerRu": "Правление",
                        "ownerKz": "Басқарма",
                        "deadlineRu": "2028",
                        "deadlineKz": "2028",
                    }
                ],
            },
            "history": [
                {
                    "id": "history-1",
                    "yearRu": "2020",
                    "yearKz": "2020",
                    "titleRu": "Событие",
                    "titleKz": "Оқиға",
                    "descriptionRu": "Подтверждённый факт.",
                    "descriptionKz": "Расталған дерек.",
                    "sourceUrl": "https://example.kz/history",
                }
            ],
            "achievements": [],
            "roadmap": [
                {
                    "id": "phase-1",
                    "phaseRu": "Этап 1",
                    "phaseKz": "1 кезең",
                    "labelRu": "В работе",
                    "labelKz": "Жұмыста",
                    "itemsRu": "Подготовить клубы",
                    "itemsKz": "Клубтарды дайындау",
                    "done": False,
                }
            ],
            "leadership": [
                {
                    "id": "president",
                    "nameRu": "Тестовый руководитель",
                    "nameKz": "Сынақ басшысы",
                    "roleRu": "Президент",
                    "roleKz": "Президент",
                    "bioRu": "Проверенная биография.",
                    "bioKz": "Расталған өмірбаян.",
                    "focusRu": "Развитие регионов.",
                    "focusKz": "Өңірлерді дамыту.",
                    "photo": "https://images.example.kz/president.webp",
                    "email": "president@example.kz",
                    "phone": "+7 700 000 00 00",
                }
            ],
        },
    }


@pytest.mark.asyncio
async def test_city_bundle_round_trips_through_normalized_database(pg_pool, tmp_path):
    source = tmp_path / "city-content.json"
    source.write_text(json.dumps(city_bundle(), ensure_ascii=False), encoding="utf-8")
    records = read_city_bundle(source)

    async with pg_pool.acquire() as connection, connection.transaction():
        assert await apply_city_import(connection, records) == 1
    projected = await project_city_payload(pg_pool, generated_at=GENERATED_AT)
    assert projected == CityPayload.model_validate(city_bundle())

    async with pg_pool.acquire() as connection, connection.transaction():
        assert await apply_city_import(connection, records) == 0
    assert await pg_pool.fetchval("SELECT count(*) FROM clubs") == 1
    assert await pg_pool.fetchval("SELECT count(*) FROM training_schedules") == 1
    assert await pg_pool.fetchval("SELECT count(*) FROM players") == 1
    assert await pg_pool.fetchval("SELECT count(*) FROM city_gallery_items") == 1


@pytest.mark.asyncio
async def test_new_city_bundle_revision_withdraws_removed_nested_records(pg_pool, tmp_path):
    source = tmp_path / "city-content.json"
    first = city_bundle()
    source.write_text(json.dumps(first, ensure_ascii=False), encoding="utf-8")
    async with pg_pool.acquire() as connection, connection.transaction():
        await apply_city_import(connection, read_city_bundle(source))

    second = city_bundle()
    city = second["cities"][0]
    city["players"] = None
    city["clubs_list"] = []
    city["schedule"] = []
    city["players_list"] = []
    city["gallery"] = []
    source.write_text(json.dumps(second, ensure_ascii=False), encoding="utf-8")
    async with pg_pool.acquire() as connection, connection.transaction():
        assert await apply_city_import(connection, read_city_bundle(source)) == 1

    projected = await project_city_payload(pg_pool, generated_at=GENERATED_AT)
    assert projected.cities[0].players is None
    assert projected.cities[0].clubs_list == []
    assert projected.cities[0].schedule == []
    assert projected.cities[0].players_list == []
    assert projected.cities[0].gallery == []
    assert await pg_pool.fetchval(
        "SELECT count(*) FROM clubs WHERE deleted_at IS NULL"
    ) == 0
    assert await pg_pool.fetchval(
        "SELECT count(*) FROM city_gallery_items WHERE deleted_at IS NULL"
    ) == 0


@pytest.mark.asyncio
async def test_private_club_contact_is_not_projected(pg_pool, tmp_path):
    source = tmp_path / "city-content.json"
    source.write_text(json.dumps(city_bundle(), ensure_ascii=False), encoding="utf-8")
    async with pg_pool.acquire() as connection, connection.transaction():
        await apply_city_import(connection, read_city_bundle(source))
        city_id = await connection.fetchval("SELECT id FROM cities WHERE slug='almaty'")
        club_id = await connection.fetchval(
            """
            INSERT INTO clubs(
                city_id, name, contact_name, contact_phone_private, contact_is_public
            ) VALUES ($1,'Private club','Private person','+7 secret',TRUE)
            RETURNING id
            """,
            city_id,
        )

    projected = await project_city_payload(pg_pool, generated_at=GENERATED_AT)
    private = next(item for item in projected.cities[0].clubs_list if item.name == "Private club")
    assert private.contactName == ""
    assert private.contactPhone == ""

    await pg_pool.execute(
        """
        INSERT INTO consents(subject_type, subject_id, scope, status)
        VALUES ('contact',$1,'contact','granted')
        """,
        club_id,
    )
    projected = await project_city_payload(pg_pool, generated_at=GENERATED_AT)
    allowed = next(item for item in projected.cities[0].clubs_list if item.name == "Private club")
    assert allowed.contactName == "Private person"
    assert allowed.contactPhone == "+7 secret"


@pytest.mark.asyncio
async def test_federation_bundle_round_trips_through_normalized_database(pg_pool, tmp_path):
    source = tmp_path / "federation-content.json"
    source.write_text(json.dumps(federation_bundle(), ensure_ascii=False), encoding="utf-8")
    records = read_federation_bundle(source)

    async with pg_pool.acquire() as connection, connection.transaction():
        assert await apply_federation_import(connection, records) == 5
    assert await project_federation_payload(
        pg_pool, generated_at=GENERATED_AT
    ) == FederationPayload.model_validate(federation_bundle())

    async with pg_pool.acquire() as connection, connection.transaction():
        assert await apply_federation_import(connection, records) == 0
    assert await pg_pool.fetchval("SELECT count(*) FROM federation_sections") == 4
    assert await pg_pool.fetchval("SELECT count(*) FROM leadership_profiles") == 1


@pytest.mark.asyncio
async def test_federation_revision_withdraws_removed_leadership(pg_pool, tmp_path):
    source = tmp_path / "federation-content.json"
    first = federation_bundle()
    source.write_text(json.dumps(first, ensure_ascii=False), encoding="utf-8")
    async with pg_pool.acquire() as connection, connection.transaction():
        await apply_federation_import(connection, read_federation_bundle(source))

    second = federation_bundle()
    second["federation"]["leadership"] = []
    source.write_text(json.dumps(second, ensure_ascii=False), encoding="utf-8")
    async with pg_pool.acquire() as connection, connection.transaction():
        assert await apply_federation_import(connection, read_federation_bundle(source)) == 1

    projected = await project_federation_payload(pg_pool, generated_at=GENERATED_AT)
    assert projected.federation.leadership == []
    assert await pg_pool.fetchval(
        "SELECT count(*) FROM leadership_profiles WHERE active=TRUE"
    ) == 0
