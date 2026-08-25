import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from aiogram.types import Chat, Contact, Message, Update, User

from floorball_bot.db import create_pool, run_migrations
from floorball_bot.dialogue.repository import DialogueSpecRepository
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
    await ingress.accept(message_update(1012, sender_id, "Алматы"))
    events = await pg_pool.fetch("SELECT payload FROM outbox_events ORDER BY created_at")

    assert "city_coach" in events[0]["payload"]["text"]
    assert "Назначенных городов: 1" in events[0]["payload"]["text"]
    assert "Недостаточно прав" in events[1]["payload"]["text"]
    assert "Сначала выберите" in events[2]["payload"]["text"]
    assert await pg_pool.fetchval("SELECT count(*) FROM jobs WHERE kind='extract'") == 0


@pytest.mark.asyncio
async def test_kazakh_transcript_confirmation_queues_extraction(pg_pool):
    sender_id = 555004
    user_id = await pg_pool.fetchval(
        """
        INSERT INTO users(phone_e164, display_name, telegram_id, preferred_language)
        VALUES ('+77011234569','KZ Coach',$1,'kz') RETURNING id
        """,
        sender_id,
    )
    await pg_pool.execute(
        "INSERT INTO user_roles(user_id, role_name) VALUES ($1,'coach_form')", user_id
    )
    loaded = DialogueSpecRepository().load("trainer")
    session_id = await pg_pool.fetchval(
        """
        INSERT INTO conversation_sessions(
            user_id, workflow, definition_version, definition_hash
        ) VALUES ($1,'trainer',$2,$3) RETURNING id
        """,
        user_id,
        loaded.spec.version,
        loaded.sha256,
    )
    await pg_pool.execute("INSERT INTO conversation_memory(session_id) VALUES ($1)", session_id)
    transcript_id = await pg_pool.fetchval(
        """
        INSERT INTO messages(
            session_id, user_id, telegram_chat_id, direction, message_type,
            original_text, normalized_text, source_language, transcript_confirmed
        ) VALUES ($1,$2,$3,'inbound','voice','','Қазақша мәтін','kz',FALSE)
        RETURNING id
        """,
        session_id,
        user_id,
        sender_id,
    )
    ingress = TelegramIngress(FakeBot(), pg_pool)

    assert await ingress.accept(message_update(1005, sender_id, "Растаймын"))

    transcript = await pg_pool.fetchrow(
        "SELECT normalized_text, transcript_confirmed FROM messages WHERE id=$1",
        transcript_id,
    )
    job = await pg_pool.fetchrow("SELECT kind, payload FROM jobs WHERE kind='extract'")
    assert transcript["normalized_text"] == "Қазақша мәтін"
    assert transcript["transcript_confirmed"] is True
    assert job["payload"]["text"] == "Қазақша мәтін"
    assert job["payload"]["mode"] == "trainer"
    assert job["payload"]["session_id"] == str(session_id)


@pytest.mark.asyncio
async def test_coach_form_role_starts_only_pinned_trainer_dialogue(pg_pool):
    sender_id = 555005
    user_id = await pg_pool.fetchval(
        """
        INSERT INTO users(phone_e164, display_name, telegram_id)
        VALUES ('+77011234570','Form Coach',$1) RETURNING id
        """,
        sender_id,
    )
    await pg_pool.execute(
        "INSERT INTO user_roles(user_id, role_name) VALUES ($1,'coach_form')", user_id
    )
    ingress = TelegramIngress(FakeBot(), pg_pool)

    await ingress.accept(message_update(1006, sender_id, "/city"))
    await ingress.accept(message_update(1007, sender_id, "/coach-form"))
    await ingress.accept(message_update(1008, sender_id, "Тестовый тренер"))
    await ingress.accept(message_update(1009, sender_id, "/resume"))

    job = await pg_pool.fetchrow("SELECT payload FROM jobs WHERE kind='extract'")
    assert job["payload"]["mode"] == "trainer"
    assert job["payload"]["session_id"]
    session = await pg_pool.fetchrow(
        """
        SELECT workflow, status, definition_version, definition_hash
        FROM conversation_sessions WHERE user_id=$1
        """,
        user_id,
    )
    assert session["workflow"] == "trainer"
    assert session["status"] == "active"
    assert session["definition_version"] == "2026-08-25"
    assert len(session["definition_hash"]) == 64
    denied = await pg_pool.fetchval(
        "SELECT payload FROM outbox_events ORDER BY created_at LIMIT 1"
    )
    assert "анкета тренера" in denied["text"]
