import os
from pathlib import Path
from uuid import uuid4

import pytest

from floorball_bot.city_applications import (
    CityApplicationRejected,
    bind_city_applicant,
    get_or_create_city_application,
    initialize_city_application,
    load_city_proposal_spec,
    record_start_intent,
    verify_city_application,
)
from floorball_bot.db import create_pool, run_migrations
from floorball_bot.domain import Actor, Role
from floorball_bot.errors import AuthorizationError

pytestmark = pytest.mark.postgres


def complete_fields(city_ru="Кызылорда", city_kz="Қызылорда"):
    return {
        "applicant_name": "Новый тренер",
        "applicant_role": "тренер",
        "city_name_ru": city_ru,
        "city_name_kz": city_kz,
        "region": "Кызылординская область",
        "current_status": "Есть инициативная группа",
        "summary_ru": "Начинаем регулярные тренировки.",
        "accuracy_confirmed": True,
        "publication_permission": True,
        "summary_kz": "Тұрақты жаттығуларды бастаймыз.",
        "history_ru": "Инициативная группа создана в 2026 году.",
        "history_kz": "Бастамашыл топ 2026 жылы құрылды.",
        "source_url": "https://floorball.kz/contacts",
        "region_aliases": "Kyzylorda, Qyzylorda",
    }


@pytest.fixture
async def pg_pool():
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not configured")
    pool = await create_pool(dsn)
    await run_migrations(pool, Path(__file__).parents[1] / "migrations")
    database = await pool.fetchval("SELECT current_database()")
    if not database.endswith("_test"):
        await pool.close()
        raise RuntimeError(f"refusing destructive fixture database: {database}")
    await pool.execute(
        "TRUNCATE coach_onboarding_contact_attempts, city_applicant_contact_attempts, "
        "telegram_start_intents, "
        "city_applications, city_applicants, users, cities RESTART IDENTITY CASCADE"
    )
    yield pool
    await pool.close()


async def seed_superadmin(pool):
    user_id = await pool.fetchval(
        "INSERT INTO users(phone_e164,display_name) VALUES ($1,'Admin') RETURNING id",
        f"+77{uuid4().int % 10**9:09d}",
    )
    await pool.execute(
        "INSERT INTO user_roles(user_id,role_name) VALUES ($1,'superadmin')", user_id
    )
    return Actor(
        user_id=user_id,
        telegram_id=99,
        roles=frozenset({Role.SUPERADMIN}),
        city_scopes=frozenset(),
    )


@pytest.mark.asyncio
async def test_verified_application_initializes_inactive_city_and_exact_scope_idempotently(pg_pool):
    actor = await seed_superadmin(pg_pool)
    sender = uuid4().int % 1_000_000_000
    async with pg_pool.acquire() as connection, connection.transaction():
        await record_start_intent(connection, telegram_id=sender, update_id=1001)
        applicant_id = await bind_city_applicant(
            connection,
            sender_id=sender,
            contact_user_id=sender,
            raw_phone="+77012345678",
            display_name="Applicant",
        )
        application = await get_or_create_city_application(
            connection, applicant_id=applicant_id
        )
        loaded = load_city_proposal_spec()
        await connection.execute(
            """
            UPDATE city_applications SET status='submitted', fields=$2::jsonb,
                spec_version=$3, spec_hash=$4 WHERE id=$1
            """,
            application["id"],
            complete_fields(),
            loaded.spec.version,
            loaded.sha256,
        )
        slug = await verify_city_application(
            connection, application_id=application["id"], actor=actor
        )
    assert slug == "kyzylorda"
    assert await pg_pool.fetchval("SELECT count(*) FROM users") == 1

    dry_run = await initialize_city_application(
        pg_pool, application_id=application["id"], actor=actor, apply=False
    )
    assert not dry_run.applied and dry_run.city_id is None
    assert await pg_pool.fetchval("SELECT count(*) FROM cities") == 0

    applied = await initialize_city_application(
        pg_pool, application_id=application["id"], actor=actor, apply=True
    )
    repeated = await initialize_city_application(
        pg_pool, application_id=application["id"], actor=actor, apply=True
    )
    city = await pg_pool.fetchrow("SELECT * FROM cities WHERE id=$1", applied.city_id)
    assert applied.applied and repeated.already_applied
    assert city["active"] is False
    assert city["region_aliases"] == ["Kyzylorda", "Qyzylorda"]
    assert await pg_pool.fetchval(
        "SELECT count(*) FROM user_roles WHERE user_id=$1 AND role_name='city_coach'",
        applied.user_id,
    ) == 1
    assert await pg_pool.fetchval(
        "SELECT count(*) FROM user_city_scopes WHERE user_id=$1 AND revoked_at IS NULL",
        applied.user_id,
    ) == 1


@pytest.mark.asyncio
async def test_contact_verification_is_self_only_and_rate_limited(pg_pool):
    sender = uuid4().int % 1_000_000_000
    async with pg_pool.acquire() as connection, connection.transaction():
        await record_start_intent(connection, telegram_id=sender, update_id=2001)
        for _ in range(5):
            with pytest.raises(ValueError):
                await bind_city_applicant(
                    connection,
                    sender_id=sender,
                    contact_user_id=sender + 1,
                    raw_phone="+77000000000",
                    display_name="Wrong",
                )
        with pytest.raises(AuthorizationError, match="too many"):
            await bind_city_applicant(
                connection,
                sender_id=sender,
                contact_user_id=sender,
                raw_phone="+77000000000",
                display_name="Applicant",
            )


@pytest.mark.asyncio
async def test_duplicate_city_blocks_verification_without_partial_initialization(pg_pool):
    actor = await seed_superadmin(pg_pool)
    await pg_pool.execute(
        "INSERT INTO cities(slug,name_ru,name_kz) VALUES ('kyzylorda','Кызылорда','Қызылорда')"
    )
    applicant_id = await pg_pool.fetchval(
        """
        INSERT INTO city_applicants(telegram_id,phone_e164,contact_verified_at)
        VALUES ($1,$2,now()) RETURNING id
        """,
        uuid4().int % 1_000_000_000,
        f"+77{uuid4().int % 10**9:09d}",
    )
    loaded = load_city_proposal_spec()
    application_id = await pg_pool.fetchval(
        """
        INSERT INTO city_applications(
            applicant_id,status,spec_version,spec_hash,fields,slug_candidate
        ) VALUES ($1,'submitted',$2,$3,$4::jsonb,'kyzylorda') RETURNING id
        """,
        applicant_id,
        loaded.spec.version,
        loaded.sha256,
        complete_fields(),
    )
    async with pg_pool.acquire() as connection, connection.transaction():
        with pytest.raises(CityApplicationRejected, match="already exists"):
            await verify_city_application(
                connection, application_id=application_id, actor=actor
            )
    assert await pg_pool.fetchval(
        "SELECT status FROM city_applications WHERE id=$1", application_id
    ) == "submitted"
