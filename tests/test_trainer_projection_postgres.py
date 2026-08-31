import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest

from floorball_bot.db import create_pool, run_migrations
from floorball_bot.dialogue.repository import DialogueSpecRepository
from floorball_bot.domain import Actor, ExtractedCityPatch, Role
from floorball_bot.errors import AuthorizationError
from floorball_bot.projection.apply import ProjectionRejected, apply_approved_trainer_draft
from floorball_bot.providers.codex import FakeExtractor
from floorball_bot.providers.transcription import FakeTranscriber
from floorball_bot.queue import claim_job, enqueue_job
from floorball_bot.worker import Worker
from floorball_bot.workflow import canonical_hash

pytestmark = pytest.mark.postgres


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
        "TRUNCATE users, cities, jobs, outbox_events, processed_updates "
        "RESTART IDENTITY CASCADE"
    )
    yield pool
    await pool.close()


def _fields(city_name: str = "Алматы") -> dict:
    return {
        "respondent": {
            "name": "Тестовый тренер",
            "role": "тренер",
            "phone": "+77000000000",
            "email": "coach@example.kz",
        },
        "city": {
            "name": city_name,
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


async def _seed_case(pg_pool, *, status: str = "approved", city_name: str = "Алматы"):
    telegram_id = uuid4().int % 1_000_000_000
    actor_id = await pg_pool.fetchval(
        """
        INSERT INTO users(phone_e164, display_name, telegram_id)
        VALUES ($1,'Администратор',$2) RETURNING id
        """,
        f"+77{uuid4().int % 10**9:09d}",
        telegram_id,
    )
    await pg_pool.execute(
        "INSERT INTO user_roles(user_id, role_name) VALUES ($1,'superadmin')", actor_id
    )
    city_id = await pg_pool.fetchval(
        """
        INSERT INTO cities(slug, name_ru, name_kz, name_en, created_by, updated_by)
        VALUES ('almaty','Алматы','Алматы','Almaty',$1,$1) RETURNING id
        """,
        actor_id,
    )
    loaded = DialogueSpecRepository().load("trainer")
    context_hash = "a" * 64
    session_id = await pg_pool.fetchval(
        """
        INSERT INTO conversation_sessions(
            user_id, city_id, workflow, definition_version, definition_hash, context_hash
        ) VALUES ($1,$2,'trainer',$3,$4,$5) RETURNING id
        """,
        actor_id,
        city_id,
        loaded.spec.version,
        loaded.sha256,
        context_hash,
    )
    draft_id = await pg_pool.fetchval(
        """
        INSERT INTO drafts(
            session_id, entity_type, city_id, status, current_revision,
            approved_revision, created_by, updated_by
        ) VALUES ($1,'city',$2,$3,1,$4,$5,$5) RETURNING id
        """,
        session_id,
        city_id,
        status,
        1 if status == "approved" else None,
        actor_id,
    )
    content = {
        "dialogue_mode": "trainer",
        "definition_version": loaded.spec.version,
        "definition_hash": loaded.sha256,
        "context_hash": context_hash,
        "fields": _fields(city_name),
    }
    await pg_pool.execute(
        """
        INSERT INTO draft_revisions(draft_id, revision, content, content_hash, created_by)
        VALUES ($1,1,$2::jsonb,$3,$4)
        """,
        draft_id,
        content,
        canonical_hash(content),
        actor_id,
    )
    actor = Actor(
        user_id=actor_id,
        telegram_id=telegram_id,
        roles=frozenset({Role.SUPERADMIN}),
        city_scopes=frozenset(),
    )
    return actor, draft_id, city_id


@pytest.mark.asyncio
async def test_only_approved_revision_can_change_canonical_city(pg_pool):
    actor, draft_id, city_id = await _seed_case(pg_pool, status="submitted")

    with pytest.raises(ProjectionRejected, match="approved"):
        await apply_approved_trainer_draft(pg_pool, draft_id=draft_id, actor=actor)

    city = await pg_pool.fetchrow(
        "SELECT players_estimate, revision FROM cities WHERE id=$1", city_id
    )
    assert dict(city) == {"players_estimate": None, "revision": 1}
    assert await pg_pool.fetchval("SELECT count(*) FROM clubs") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("UPDATE drafts SET current_revision=2 WHERE id=$1", "stale"),
        (
            "UPDATE conversation_sessions SET context_hash=repeat('b',64) "
            "WHERE id=(SELECT session_id FROM drafts WHERE id=$1)",
            "context hash changed",
        ),
        (
            "UPDATE conversation_sessions SET definition_hash=repeat('b',64) "
            "WHERE id=(SELECT session_id FROM drafts WHERE id=$1)",
            "definition hash changed",
        ),
    ],
)
async def test_stale_pinned_inputs_are_rejected_without_writes(pg_pool, mutation, message):
    actor, draft_id, city_id = await _seed_case(pg_pool)
    await pg_pool.execute(mutation, draft_id)

    with pytest.raises(ProjectionRejected, match=message):
        await apply_approved_trainer_draft(pg_pool, draft_id=draft_id, actor=actor)

    assert await pg_pool.fetchval("SELECT revision FROM cities WHERE id=$1", city_id) == 1
    assert await pg_pool.fetchval("SELECT count(*) FROM city_content") == 0
    assert await pg_pool.fetchval("SELECT count(*) FROM canonical_projection_applications") == 0


@pytest.mark.asyncio
async def test_superadmin_applies_public_fields_but_keeps_contact_private(pg_pool):
    actor, draft_id, city_id = await _seed_case(pg_pool)

    result = await apply_approved_trainer_draft(pg_pool, draft_id=draft_id, actor=actor)

    assert result.applied
    assert result.city_id == city_id
    assert result.revision == 1
    city = await pg_pool.fetchrow(
        """
        SELECT players_estimate, coaches_estimate, clubs_estimate, data_status, revision
        FROM cities WHERE id=$1
        """,
        city_id,
    )
    assert dict(city) == {
        "players_estimate": 40,
        "coaches_estimate": 3,
        "clubs_estimate": 1,
        "data_status": "verified-coach-data",
        "revision": 2,
    }
    content = await pg_pool.fetchrow(
        "SELECT description_ru, history_ru FROM city_content WHERE city_id=$1", city_id
    )
    assert dict(content) == {
        "description_ru": "Регулярные тренировки для детей и взрослых.",
        "history_ru": "Подтверждённая история городского флорбола.",
    }
    club = await pg_pool.fetchrow(
        """
        SELECT contact_phone_private, contact_is_public, source_key
        FROM clubs WHERE city_id=$1
        """,
        city_id,
    )
    assert club["contact_phone_private"] == "+77001112233"
    assert not club["contact_is_public"]
    assert club["source_key"] == f"draft:{draft_id}:club:0"
    assert await pg_pool.fetchval(
        "SELECT count(*) FROM training_schedules WHERE city_id=$1", city_id
    ) == 1


@pytest.mark.asyncio
async def test_projection_is_idempotent_for_same_approved_revision(pg_pool):
    actor, draft_id, city_id = await _seed_case(pg_pool)

    first = await apply_approved_trainer_draft(pg_pool, draft_id=draft_id, actor=actor)
    second = await apply_approved_trainer_draft(pg_pool, draft_id=draft_id, actor=actor)

    assert first.applied
    assert not second.applied
    assert second.application_id == first.application_id
    assert await pg_pool.fetchval("SELECT revision FROM cities WHERE id=$1", city_id) == 2
    assert await pg_pool.fetchval("SELECT count(*) FROM clubs WHERE city_id=$1", city_id) == 1
    assert await pg_pool.fetchval(
        "SELECT count(*) FROM canonical_projection_applications WHERE draft_id=$1", draft_id
    ) == 1


@pytest.mark.asyncio
async def test_concurrent_projection_applies_revision_once(pg_pool):
    actor, draft_id, city_id = await _seed_case(pg_pool)

    results = await asyncio.gather(
        apply_approved_trainer_draft(pg_pool, draft_id=draft_id, actor=actor),
        apply_approved_trainer_draft(pg_pool, draft_id=draft_id, actor=actor),
    )

    assert sorted(result.applied for result in results) == [False, True]
    assert results[0].application_id == results[1].application_id
    assert await pg_pool.fetchval("SELECT revision FROM cities WHERE id=$1", city_id) == 2
    assert await pg_pool.fetchval(
        "SELECT count(*) FROM canonical_projection_applications WHERE draft_id=$1", draft_id
    ) == 1


@pytest.mark.asyncio
async def test_projection_rejects_wrong_role_and_new_city_without_partial_writes(pg_pool):
    actor, draft_id, city_id = await _seed_case(pg_pool, city_name="другой")
    unauthorized = actor.model_copy(update={"roles": frozenset({Role.CITY_COACH})})

    with pytest.raises(AuthorizationError):
        await apply_approved_trainer_draft(
            pg_pool, draft_id=draft_id, actor=unauthorized
        )
    with pytest.raises(ProjectionRejected, match="new_city_application_required"):
        await apply_approved_trainer_draft(pg_pool, draft_id=draft_id, actor=actor)

    assert await pg_pool.fetchval("SELECT revision FROM cities WHERE id=$1", city_id) == 1
    assert await pg_pool.fetchval("SELECT count(*) FROM city_content") == 0
    assert await pg_pool.fetchval("SELECT count(*) FROM clubs") == 0


@pytest.mark.asyncio
async def test_worker_applies_approved_projection_and_schedules_readiness_scan(pg_pool):
    actor, draft_id, city_id = await _seed_case(pg_pool)
    async with pg_pool.acquire() as connection, connection.transaction():
        job_id = await enqueue_job(
            connection,
            kind="apply_projection",
            payload={
                "draft_id": str(draft_id),
                "actor_id": str(actor.user_id),
                "chat_id": actor.telegram_id,
            },
            idempotency_key=f"test-apply:{draft_id}",
        )
    job = await claim_job(pg_pool, worker_id="projection-test")
    assert job is not None and job.id == job_id
    worker = Worker(
        pg_pool,
        extractor=FakeExtractor(ExtractedCityPatch(source_language="ru")),
        transcriber=FakeTranscriber(),
    )

    await worker.process(job)

    assert await pg_pool.fetchval("SELECT status FROM jobs WHERE id=$1", job_id) == "succeeded"
    assert await pg_pool.fetchval(
        "SELECT count(*) FROM canonical_projection_applications WHERE draft_id=$1", draft_id
    ) == 1
    assert await pg_pool.fetchval("SELECT players_estimate FROM cities WHERE id=$1", city_id) == 40
    assert await pg_pool.fetchval(
        "SELECT count(*) FROM jobs WHERE kind='readiness_scan' AND status='pending'"
    ) == 1
    notice = await pg_pool.fetchval(
        "SELECT payload FROM outbox_events WHERE payload->>'chat_id'=$1",
        str(actor.telegram_id),
    )
    assert "основную БД" in notice["text"]
    assert "preview" in notice["text"]
