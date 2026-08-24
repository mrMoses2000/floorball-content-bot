import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from aiogram.types import Chat, Contact, Message, Update, User

from floorball_bot.db import create_pool, run_migrations
from floorball_bot.telegram import TelegramIngress

pytestmark = pytest.mark.postgres


class FakeBot:
    pass


@pytest.fixture
async def pg_pool():
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not configured")
    pool = await create_pool(dsn)
    await run_migrations(pool, Path(__file__).parents[1] / "migrations")
    await pool.execute(
        "TRUNCATE users, cities, jobs, outbox_events, processed_updates RESTART IDENTITY CASCADE"
    )
    yield pool
    await pool.close()


def message_update(update_id: int, sender_id: int, text: str) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime.now(UTC),
            chat=Chat(id=sender_id, type="private"),
            from_user=User(id=sender_id, is_bot=False, first_name="Test"),
            text=text,
        ),
    )


@pytest.mark.asyncio
async def test_unknown_user_is_denied_and_duplicate_update_is_idempotent(pg_pool):
    ingress = TelegramIngress(FakeBot(), pg_pool)
    update = message_update(1001, 555001, "/help")

    assert await ingress.accept(update)
    assert not await ingress.accept(update)
    events = await pg_pool.fetch("SELECT payload FROM outbox_events")

    assert len(events) == 1
    assert "Доступ запрещён" in events[0]["payload"]["text"]


@pytest.mark.asyncio
async def test_foreign_contact_is_rejected(pg_pool):
    phone = "+77011234567"
    await pg_pool.execute("INSERT INTO users(phone_e164, display_name) VALUES ($1,'Coach')", phone)
    sender_id = 555002
    update = Update(
        update_id=1002,
        message=Message(
            message_id=1002,
            date=datetime.now(UTC),
            chat=Chat(id=sender_id, type="private"),
            from_user=User(id=sender_id, is_bot=False, first_name="Test"),
            contact=Contact(phone_number=phone, first_name="Other", user_id=sender_id + 1),
        ),
    )
    ingress = TelegramIngress(FakeBot(), pg_pool)

    assert await ingress.accept(update)
    payload = await pg_pool.fetchval("SELECT payload FROM outbox_events")
    assert "не совпал" in payload["text"]
    bound_id = await pg_pool.fetchval("SELECT telegram_id FROM users WHERE phone_e164=$1", phone)
    assert bound_id is None


@pytest.mark.asyncio
async def test_authorized_coach_profile_and_superadmin_command_denial(pg_pool):
    sender_id = 555003
    user_id = await pg_pool.fetchval(
        """
        INSERT INTO users(phone_e164, display_name, telegram_id)
        VALUES ('+77011234568','Coach',$1) RETURNING id
        """,
        sender_id,
    )
    city_id = await pg_pool.fetchval(
        """
        INSERT INTO cities(slug, name_ru, name_kz, name_en)
        VALUES ($1,'Алматы','Алматы','Almaty') RETURNING id
        """,
        f"city-{uuid4().hex[:8]}",
    )
    await pg_pool.execute(
        "INSERT INTO user_roles VALUES ($1,'city_coach',NULL,now(),NULL)", user_id
    )
    await pg_pool.execute(
        "INSERT INTO user_city_scopes VALUES ($1,$2,NULL,now(),NULL)", user_id, city_id
    )
    ingress = TelegramIngress(FakeBot(), pg_pool)

    await ingress.accept(message_update(1003, sender_id, "/profile"))
    await ingress.accept(message_update(1004, sender_id, "/publish"))
    events = await pg_pool.fetch("SELECT payload FROM outbox_events ORDER BY created_at")

    assert "city_coach" in events[0]["payload"]["text"]
    assert "Назначенных городов: 1" in events[0]["payload"]["text"]
    assert "Недостаточно прав" in events[1]["payload"]["text"]
