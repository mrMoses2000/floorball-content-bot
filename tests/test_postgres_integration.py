import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from floorball_bot.db import create_pool, run_migrations, transaction
from floorball_bot.dialogue.patches import (
    ExtractedDialoguePatch,
    ExtractedFieldPatch,
    canonical_context,
)
from floorball_bot.dialogue.repository import DialogueSpecRepository
from floorball_bot.domain import Actor, DraftStatus, ExtractedCityPatch, Role
from floorball_bot.providers.codex import FakeExtractor
from floorball_bot.providers.transcription import FakeTranscriber
from floorball_bot.queue import (
    accept_update,
    claim_job,
    enqueue_job,
    fail_job,
)
from floorball_bot.worker import Worker
from floorball_bot.workflow import add_revision, transition_draft

pytestmark = pytest.mark.postgres


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


@pytest.mark.asyncio
async def test_migrations_are_repeatable(pg_pool):
    migrations_dir = Path(__file__).parents[1] / "migrations"
    assert await run_migrations(pg_pool, migrations_dir) == []
    count = await pg_pool.fetchval("SELECT count(*) FROM schema_migrations")
    assert count == len(list(migrations_dir.glob("[0-9][0-9][0-9]_*.sql")))


@pytest.mark.asyncio
async def test_duplicate_update_and_job_idempotency(pg_pool):
    update_id = 9_000_000 + uuid4().int % 1_000_000
    async with transaction(pg_pool) as connection:
        assert await accept_update(connection, update_id)
        assert not await accept_update(connection, update_id)
        first = await enqueue_job(
            connection,
            kind="extract",
            payload={"text": "Алматы"},
            idempotency_key=f"integration:{uuid4()}",
        )
        assert first is not None
        duplicate = await enqueue_job(
            connection,
            kind="extract",
            payload={"text": "Другое"},
            idempotency_key=(
                await connection.fetchval("SELECT idempotency_key FROM jobs WHERE id=$1", first)
            ),
        )
        assert duplicate is None


@pytest.mark.asyncio
async def test_job_claim_and_retry(pg_pool):
    key = f"integration:{uuid4()}"
    async with transaction(pg_pool) as connection:
        job_id = await enqueue_job(
            connection,
            kind="extract",
            payload={"text": "Қазақша мәтін"},
            idempotency_key=key,
            max_attempts=2,
        )
    claimed = await claim_job(pg_pool, worker_id="test-worker", lease_seconds=1)
    assert claimed is not None
    assert claimed.id == job_id
    assert claimed.attempts == 1
    await fail_job(pg_pool, claimed, "temporary", retryable=True)
    status = await pg_pool.fetchval("SELECT status FROM jobs WHERE id=$1", job_id)
    assert status == "retry"


@pytest.mark.asyncio
async def test_revision_idempotency_and_review_transition(pg_pool):
    phone = f"+77{uuid4().int % 10**9:09d}"
    user_id = await pg_pool.fetchval(
        """
        INSERT INTO users(phone_e164, display_name, telegram_id)
        VALUES ($1,'Coach',$2) RETURNING id
        """,
        phone,
        uuid4().int % 1_000_000_000,
    )
    session_id = await pg_pool.fetchval(
        "INSERT INTO conversation_sessions(user_id, workflow) VALUES ($1,'city') RETURNING id",
        user_id,
    )
    draft_id = await pg_pool.fetchval(
        """
        INSERT INTO drafts(session_id, entity_type, created_by, updated_by)
        VALUES ($1,'city',$2,$2) RETURNING id
        """,
        session_id,
        user_id,
    )
    actor = Actor(
        user_id=user_id,
        telegram_id=1,
        roles=frozenset({Role.CITY_COACH}),
        city_scopes=frozenset(),
    )
    content = {"slug": "almaty", "descKz": "Қазақша мәтін"}
    async with transaction(pg_pool) as connection:
        first = await add_revision(connection, draft_id=draft_id, actor=actor, content=content)
        second = await add_revision(connection, draft_id=draft_id, actor=actor, content=content)
    assert first == second == 2
    assert (
        await pg_pool.fetchval("SELECT count(*) FROM draft_revisions WHERE draft_id=$1", draft_id)
        == 1
    )

    async with transaction(pg_pool) as connection:
        await transition_draft(
            connection,
            draft_id=draft_id,
            actor=actor,
            target=DraftStatus.READY_FOR_USER_REVIEW,
        )
        await transition_draft(
            connection,
            draft_id=draft_id,
            actor=actor,
            target=DraftStatus.SUBMITTED,
        )
        reviewer = actor.model_copy(update={"roles": frozenset({Role.REVIEWER})})
        await transition_draft(
            connection,
            draft_id=draft_id,
            actor=reviewer,
            target=DraftStatus.UNDER_REVIEW,
        )
        await transition_draft(
            connection,
            draft_id=draft_id,
            actor=reviewer,
            target=DraftStatus.APPROVED,
        )
    draft = await pg_pool.fetchrow(
        "SELECT status, current_revision, approved_revision FROM drafts WHERE id=$1", draft_id
    )
    assert dict(draft) == {
        "status": "approved",
        "current_revision": 2,
        "approved_revision": 2,
    }


@pytest.mark.asyncio
async def test_voice_transcript_requires_confirmation_before_extraction(pg_pool, tmp_path):
    user_id = await pg_pool.fetchval(
        """
        INSERT INTO users(phone_e164, display_name, telegram_id)
        VALUES ('+77011234999','Voice Coach',9988) RETURNING id
        """
    )
    message_id = await pg_pool.fetchval(
        """
        INSERT INTO messages(
            user_id, telegram_chat_id, direction, message_type, original_text
        ) VALUES ($1,9988,'inbound','voice','') RETURNING id
        """,
        user_id,
    )
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"fake")
    async with transaction(pg_pool) as connection:
        job_id = await enqueue_job(
            connection,
            kind="transcribe",
            payload={"path": str(audio), "chat_id": 9988, "language": "kz"},
            idempotency_key=f"voice:{uuid4()}",
        )
    job = await claim_job(pg_pool, worker_id="voice-worker")
    assert job is not None and job.id == job_id
    worker = Worker(
        pg_pool,
        extractor=FakeExtractor(ExtractedCityPatch(source_language="kz")),
        transcriber=FakeTranscriber("Қазақша мәтін", "kz"),
    )

    await worker.process(job)

    message = await pg_pool.fetchrow(
        "SELECT normalized_text, transcript_confirmed FROM messages WHERE id=$1", message_id
    )
    assert message["normalized_text"] == "Қазақша мәтін"
    assert message["transcript_confirmed"] is False
    assert await pg_pool.fetchval("SELECT count(*) FROM jobs WHERE kind='extract'") == 0
    reply = await pg_pool.fetchval("SELECT payload FROM outbox_events")
    assert "Подтвердите текст" in reply["text"]


@pytest.mark.asyncio
async def test_dialogue_worker_uses_pinned_spec_and_safe_db_snapshot(pg_pool):
    loaded = DialogueSpecRepository().load("trainer")
    user_id = await pg_pool.fetchval(
        """
        INSERT INTO users(phone_e164, display_name, telegram_id)
        VALUES ('+77011234888','Dialogue Coach',8877) RETURNING id
        """
    )
    await pg_pool.execute(
        "INSERT INTO user_roles(user_id, role_name) VALUES ($1,'coach_form')", user_id
    )
    await pg_pool.execute(
        """
        INSERT INTO cities(slug, name_ru, name_kz, name_en)
        VALUES ('almaty','Алматы','Алматы','Almaty')
        """
    )
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
    await pg_pool.execute(
        "INSERT INTO conversation_memory(session_id) VALUES ($1)", session_id
    )

    class EchoExtractor:
        async def extract(
            self, text, output_model, *, mode=None, context=None, known_fields=None
        ):
            _, context_hash = canonical_context(context)
            return ExtractedDialoguePatch(
                mode=mode,
                spec_sha256=loaded.sha256,
                context_sha256=context_hash,
                fields=[
                    ExtractedFieldPatch(
                        field_id="respondent",
                        value_json=json.dumps(
                            {"name": "Тестовый тренер", "role": "тренер"},
                            ensure_ascii=False,
                        ),
                    )
                ],
            )

    async with transaction(pg_pool) as connection:
        await enqueue_job(
            connection,
            kind="extract",
            payload={
                "text": "Я тестовый тренер",
                "chat_id": 8877,
                "user_id": str(user_id),
                "session_id": str(session_id),
                "mode": "trainer",
            },
            idempotency_key=f"dialogue:{uuid4()}",
        )
    job = await claim_job(pg_pool, worker_id="dialogue-worker")
    worker = Worker(
        pg_pool,
        extractor=EchoExtractor(),
        transcriber=FakeTranscriber("", "ru"),
    )

    await worker.process(job)

    memory = await pg_pool.fetchval(
        "SELECT structured_memory FROM conversation_memory WHERE session_id=$1", session_id
    )
    assert memory["fields"]["respondent"]["name"] == "Тестовый тренер"
    assert await pg_pool.fetchval(
        "SELECT count(*) FROM agent_context_snapshots WHERE session_id=$1", session_id
    ) == 1
    stored = await pg_pool.fetchval(
        "SELECT context FROM agent_context_snapshots WHERE session_id=$1", session_id
    )
    serialized = json.dumps(stored)
    assert "+77011234888" not in serialized
    assert "telegram_id" not in serialized
    reply = await pg_pool.fetchval(
        "SELECT payload FROM outbox_events WHERE idempotency_key LIKE 'dialogue-extract-result:%'"
    )
    assert "О каком городе" in reply["text"]
