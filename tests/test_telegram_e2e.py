import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from aiogram.types import CallbackQuery, Chat, Contact, Message, Update, User

from floorball_bot.db import create_pool, run_migrations
from floorball_bot.dialogue.repository import DialogueSpecRepository
from floorball_bot.telegram import TelegramIngress
from floorball_bot.workflow import canonical_hash

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


def callback_update(update_id: int, sender_id: int, data: str) -> Update:
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=f"callback-{update_id}",
            from_user=User(id=sender_id, is_bot=False, first_name="Reviewer"),
            chat_instance="test-review",
            data=data,
        ),
    )


def contact_update(
    update_id: int, sender_id: int, *, contact_user_id: int, phone: str
) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime.now(UTC),
            chat=Chat(id=sender_id, type="private"),
            from_user=User(id=sender_id, is_bot=False, first_name="Applicant"),
            contact=Contact(
                phone_number=phone,
                first_name="Applicant",
                user_id=contact_user_id,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_new_city_deep_link_keeps_applicant_isolated_until_superadmin_initialization(pg_pool):
    sender_id = uuid4().int % 1_000_000_000
    ingress = TelegramIngress(FakeBot(), pg_pool)

    assert await ingress.accept(message_update(8_000, sender_id, "/start new_city"))
    assert await pg_pool.fetchval(
        "SELECT count(*) FROM telegram_start_intents WHERE telegram_id=$1 AND status='pending'",
        sender_id,
    ) == 1
    assert await pg_pool.fetchval("SELECT count(*) FROM users") == 0

    assert await ingress.accept(
        contact_update(
            8_001,
            sender_id,
            contact_user_id=sender_id,
            phone="+77012345678",
        )
    )
    answers = [
        "Новый тренер",
        "тренер",
        "Конаев",
        "Қонаев",
        "Алматинская область",
        "Есть инициативная группа",
        "Начинаем регулярные тренировки.",
        "да",
        "да",
        "Тұрақты жаттығуларды бастаймыз.",
        "Инициативная группа создана в 2026 году.",
        "Бастамашыл топ 2026 жылы құрылды.",
        "https://floorball.kz/contacts",
        "/skip",
        "/skip",
        "/skip",
    ]
    for offset, answer in enumerate(answers, start=2):
        assert await ingress.accept(message_update(8_000 + offset, sender_id, answer))
    assert await ingress.accept(message_update(8_100, sender_id, "/submit"))

    application = await pg_pool.fetchrow(
        """
        SELECT a.status, a.slug_candidate, p.telegram_id
        FROM city_applications a JOIN city_applicants p ON p.id=a.applicant_id
        WHERE p.telegram_id=$1
        """,
        sender_id,
    )
    assert application["status"] == "submitted"
    assert application["slug_candidate"] == "konaev"
    assert await pg_pool.fetchval("SELECT count(*) FROM users") == 0
    assert await pg_pool.fetchval("SELECT count(*) FROM agent_context_snapshots") == 0

    assert await ingress.accept(message_update(8_101, sender_id, "/profile"))
    last_reply = await pg_pool.fetchval(
        "SELECT payload->>'text' FROM outbox_events ORDER BY created_at DESC LIMIT 1"
    )
    assert "Доступны команды" in last_reply


async def _seed_submitted_trainer_draft(pg_pool, *, reviewer_telegram_id: int):
    coach_telegram_id = uuid4().int % 1_000_000_000
    coach_id = await pg_pool.fetchval(
        """
        INSERT INTO users(phone_e164, display_name, telegram_id)
        VALUES ($1,'Тренер',$2) RETURNING id
        """,
        f"+77{uuid4().int % 10**9:09d}",
        coach_telegram_id,
    )
    reviewer_id = await pg_pool.fetchval(
        """
        INSERT INTO users(phone_e164, display_name, telegram_id)
        VALUES ($1,'Редактор',$2) RETURNING id
        """,
        f"+77{uuid4().int % 10**9:09d}",
        reviewer_telegram_id,
    )
    await pg_pool.execute(
        "INSERT INTO user_roles(user_id, role_name) VALUES ($1,'reviewer')", reviewer_id
    )
    loaded = DialogueSpecRepository().load("trainer")
    session_id = await pg_pool.fetchval(
        """
        INSERT INTO conversation_sessions(
            user_id, workflow, status, definition_version, definition_hash
        ) VALUES ($1,'trainer','completed',$2,$3) RETURNING id
        """,
        coach_id,
        loaded.spec.version,
        loaded.sha256,
    )
    content = {
        "dialogue_mode": "trainer",
        "definition_version": loaded.spec.version,
        "definition_hash": loaded.sha256,
        "context_hash": "",
        "fields": {
            "respondent": {
                "name": "Тренер",
                "role": "тренер",
                "phone": "+77000000000",
                "email": "coach@example.kz",
            },
            "city": {
                "name": "Алматы",
                "status": "есть регулярные тренировки",
                "summary": "Описание",
                "history": "История",
            },
            "metrics": {
                "players_total": 12,
                "coaches_total": 2,
                "clubs_total": 1,
                "data_confidence": 4,
            },
            "media": {"permission": "нет", "minors_permission": "нет"},
            "accuracy_confirmed": True,
            "publication_permission": True,
        },
    }
    draft_id = await pg_pool.fetchval(
        """
        INSERT INTO drafts(
            session_id, entity_type, status, current_revision, created_by, updated_by
        ) VALUES ($1,'city','submitted',1,$2,$2) RETURNING id
        """,
        session_id,
        coach_id,
    )
    await pg_pool.execute(
        """
        INSERT INTO draft_revisions(draft_id, revision, content, content_hash, created_by)
        VALUES ($1,1,$2::jsonb,$3,$4)
        """,
        draft_id,
        content,
        canonical_hash(content),
        coach_id,
    )
    await pg_pool.execute(
        """
        INSERT INTO conversation_memory(session_id, structured_memory)
        VALUES ($1,$2::jsonb)
        """,
        session_id,
        {"fields": content["fields"]},
    )
    return draft_id, coach_telegram_id


@pytest.mark.asyncio
async def test_unknown_user_is_denied_and_duplicate_update_is_idempotent(pg_pool):
    ingress = TelegramIngress(FakeBot(), pg_pool)
    update = message_update(1001, 555001, "/help")

    assert await ingress.accept(update)
    assert not await ingress.accept(update)
    events = await pg_pool.fetch("SELECT payload FROM outbox_events")

    assert len(events) == 1
    assert "Доступ запрещён" in events[0]["payload"]["text"]
    processed = await pg_pool.fetchrow(
        "SELECT status, completed_at FROM processed_updates WHERE update_id=$1",
        update.update_id,
    )
    assert processed["status"] == "completed"
    assert processed["completed_at"] is not None


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


@pytest.mark.asyncio
async def test_reviewer_can_open_inbox_review_and_approve_exact_revision(pg_pool):
    reviewer_telegram_id = 555099
    draft_id, _coach_telegram_id = await _seed_submitted_trainer_draft(
        pg_pool, reviewer_telegram_id=reviewer_telegram_id
    )
    ingress = TelegramIngress(FakeBot(), pg_pool)

    assert await ingress.accept(message_update(1090, reviewer_telegram_id, "/review"))
    inbox = await pg_pool.fetchval(
        """
        SELECT payload FROM outbox_events
        WHERE payload->>'chat_id'=$1 ORDER BY created_at DESC LIMIT 1
        """,
        str(reviewer_telegram_id),
    )
    assert "Алматы" in inbox["text"]
    start_callback = inbox["reply_markup"]["inline_keyboard"][0][0]["callback_data"]

    assert await ingress.accept(callback_update(1091, reviewer_telegram_id, start_callback))
    assert (
        await pg_pool.fetchval("SELECT status FROM drafts WHERE id=$1", draft_id)
        == "under_review"
    )
    review = await pg_pool.fetchval(
        """
        SELECT payload FROM outbox_events
        WHERE payload->>'chat_id'=$1 ORDER BY created_at DESC LIMIT 1
        """,
        str(reviewer_telegram_id),
    )
    assert "Игроков: 12" in review["text"]
    assert "context_hash" not in review["text"]
    buttons = review["reply_markup"]["inline_keyboard"][0]
    approve_callback = next(
        button["callback_data"] for button in buttons if "Одобрить" in button["text"]
    )

    assert await ingress.accept(callback_update(1092, reviewer_telegram_id, approve_callback))
    draft = await pg_pool.fetchrow(
        "SELECT status, approved_revision FROM drafts WHERE id=$1", draft_id
    )
    assert dict(draft) == {"status": "approved", "approved_revision": 1}
    job = await pg_pool.fetchrow("SELECT kind, payload FROM jobs WHERE kind='apply_projection'")
    assert job["kind"] == "apply_projection"
    assert job["payload"]["draft_id"] == str(draft_id)
    assert job["payload"]["actor_id"]


@pytest.mark.asyncio
async def test_reviewer_requests_changes_and_resumes_author_session(pg_pool):
    reviewer_telegram_id = 555100
    draft_id, coach_telegram_id = await _seed_submitted_trainer_draft(
        pg_pool, reviewer_telegram_id=reviewer_telegram_id
    )
    ingress = TelegramIngress(FakeBot(), pg_pool)

    await ingress.accept(message_update(1100, reviewer_telegram_id, "/review"))
    inbox = await pg_pool.fetchval(
        """
        SELECT payload FROM outbox_events WHERE payload->>'chat_id'=$1
        ORDER BY created_at DESC LIMIT 1
        """,
        str(reviewer_telegram_id),
    )
    await ingress.accept(
        callback_update(
            1101,
            reviewer_telegram_id,
            inbox["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
        )
    )
    review = await pg_pool.fetchval(
        """
        SELECT payload FROM outbox_events WHERE payload->>'chat_id'=$1
        ORDER BY created_at DESC LIMIT 1
        """,
        str(reviewer_telegram_id),
    )
    changes_callback = next(
        button["callback_data"]
        for button in review["reply_markup"]["inline_keyboard"][0]
        if "изменения" in button["text"]
    )

    await ingress.accept(callback_update(1102, reviewer_telegram_id, changes_callback))

    draft = await pg_pool.fetchrow(
        """
        SELECT d.status, d.approved_revision, s.status AS session_status, s.current_step
        FROM drafts d JOIN conversation_sessions s ON s.id=d.session_id WHERE d.id=$1
        """,
        draft_id,
    )
    assert dict(draft) == {
        "status": "changes_requested",
        "approved_revision": None,
        "session_status": "active",
        "current_step": "changes_requested",
    }
    author_notice = await pg_pool.fetchval(
        """
        SELECT payload FROM outbox_events WHERE payload->>'chat_id'=$1
        ORDER BY created_at DESC LIMIT 1
        """,
        str(coach_telegram_id),
    )
    assert "/resume" in author_notice["text"]
    assert await pg_pool.fetchval("SELECT count(*) FROM jobs WHERE kind='apply_projection'") == 0


@pytest.mark.asyncio
async def test_resubmission_appends_revision_to_same_draft(pg_pool):
    reviewer_telegram_id = 555101
    draft_id, coach_telegram_id = await _seed_submitted_trainer_draft(
        pg_pool, reviewer_telegram_id=reviewer_telegram_id
    )
    await pg_pool.execute(
        "UPDATE drafts SET status='changes_requested' WHERE id=$1", draft_id
    )
    await pg_pool.execute(
        """
        UPDATE conversation_sessions SET status='active', current_step='changes_requested'
        WHERE id=(SELECT session_id FROM drafts WHERE id=$1)
        """,
        draft_id,
    )
    await pg_pool.execute(
        """
        UPDATE conversation_memory
        SET structured_memory=jsonb_set(
            structured_memory,
            '{fields,city,summary}',
            to_jsonb('Исправленное описание'::text)
        )
        WHERE session_id=(SELECT session_id FROM drafts WHERE id=$1)
        """,
        draft_id,
    )
    stale_callback_id = await pg_pool.fetchval(
        """
        INSERT INTO callback_actions(
            actor_id, action, target_id, nonce_hash, expires_at
        )
        SELECT created_by, 'review_approve', id, repeat('a',64), now()+interval '1 hour'
        FROM drafts WHERE id=$1 RETURNING id
        """,
        draft_id,
    )
    ingress = TelegramIngress(FakeBot(), pg_pool)

    assert await ingress.accept(message_update(1110, coach_telegram_id, "/submit"))

    draft = await pg_pool.fetchrow(
        "SELECT status, current_revision, approved_revision FROM drafts WHERE id=$1", draft_id
    )
    assert dict(draft) == {
        "status": "submitted",
        "current_revision": 2,
        "approved_revision": None,
    }
    assert await pg_pool.fetchval(
        "SELECT count(*) FROM drafts WHERE session_id=(SELECT session_id FROM drafts WHERE id=$1)",
        draft_id,
    ) == 1
    revisions = await pg_pool.fetch(
        "SELECT revision, content FROM draft_revisions WHERE draft_id=$1 ORDER BY revision",
        draft_id,
    )
    assert [row["revision"] for row in revisions] == [1, 2]
    assert revisions[1]["content"]["fields"]["city"]["summary"] == "Исправленное описание"
    assert await pg_pool.fetchval(
        "SELECT consumed_at IS NOT NULL FROM callback_actions WHERE id=$1", stale_callback_id
    )
