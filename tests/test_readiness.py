from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiogram.types import CallbackQuery, Update, User

from floorball_bot.db import create_pool, run_migrations
from floorball_bot.domain import PublicCity, PublicFederation
from floorball_bot.publisher import PublicationPreview
from floorball_bot.queue import claim_job
from floorball_bot.readiness import city_missing, federation_missing, scan_readiness
from floorball_bot.telegram import TelegramIngress
from floorball_bot.worker import Worker


def test_city_readiness_distinguishes_complete_site_data():
    city = PublicCity(
        slug="almaty",
        nameRu="Алматы",
        nameKz="Алматы",
        nameEn="Almaty",
        players=0,
        coaches=0,
        clubs=0,
        dataStatus="verified-coach-data",
        updatedAt="2026-08-26T00:00:00.000Z",
        descRu="Описание",
        descKz="Сипаттама",
        historyRu="История",
        historyKz="Тарих",
    )

    assert city_missing(city) == ()
    assert "descKz" in city_missing(city.model_copy(update={"descKz": ""}))


def test_federation_readiness_requires_mission_history_and_leadership():
    missing = federation_missing(PublicFederation())

    assert "mission.statementRu" in missing
    assert "history" in missing
    assert "leadership" in missing


@pytest.fixture
async def readiness_pool():
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


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_ready_city_notifies_once_and_callback_queues_preview(readiness_pool):
    telegram_id = 778899
    admin_id = await readiness_pool.fetchval(
        """
        INSERT INTO users(phone_e164, display_name, telegram_id)
        VALUES ('+77070000001','Publishing admin',$1) RETURNING id
        """,
        telegram_id,
    )
    await readiness_pool.execute(
        "INSERT INTO user_roles(user_id, role_name) VALUES ($1,'superadmin')", admin_id
    )
    await readiness_pool.execute(
        """
        INSERT INTO notification_subscriptions(user_id, event_type)
        VALUES ($1,'content_ready'), ($1,'publication_status')
        """,
        admin_id,
    )
    city_id = await readiness_pool.fetchval(
        """
        INSERT INTO cities(
            slug, name_ru, name_kz, name_en, players_estimate, coaches_estimate,
            clubs_estimate, data_status, public_updated_at
        ) VALUES (
            'almaty','Алматы','Алматы','Almaty',0,0,0,
            'verified-coach-data',$1
        ) RETURNING id
        """,
        datetime(2026, 8, 26, tzinfo=UTC),
    )
    await readiness_pool.execute(
        """
        INSERT INTO city_content(
            city_id, description_ru, description_kz, history_ru, history_kz
        ) VALUES ($1,'Описание','Сипаттама','История','Тарих')
        """,
        city_id,
    )

    first = await scan_readiness(readiness_pool)
    city_result = next(item for item in first if item.entity_key == "almaty")

    assert city_result.ready is True
    assert city_result.notifications_created == 1
    assert city_result.draft_id is not None
    event = await readiness_pool.fetchval(
        "SELECT payload FROM outbox_events WHERE idempotency_key LIKE 'content-ready:%'"
    )
    assert "Данные готовы: город almaty" in event["text"]
    callback_data = event["reply_markup"]["inline_keyboard"][0][0]["callback_data"]

    second = await scan_readiness(readiness_pool)
    repeated = next(item for item in second if item.entity_key == "almaty")
    assert repeated.notifications_created == 0
    assert await readiness_pool.fetchval(
        "SELECT count(*) FROM outbox_events WHERE idempotency_key LIKE 'content-ready:%'"
    ) == 1

    ingress = TelegramIngress(object(), readiness_pool)
    update = Update(
        update_id=889900,
        callback_query=CallbackQuery(
            id="callback-1",
            from_user=User(
                id=telegram_id,
                is_bot=False,
                first_name="Publishing",
            ),
            chat_instance="test-instance",
            data=callback_data,
        ),
    )

    assert await ingress.accept(update)
    draft = await readiness_pool.fetchrow(
        "SELECT status, approved_revision FROM drafts WHERE id=$1", city_result.draft_id
    )
    assert dict(draft) == {"status": "approved", "approved_revision": 1}
    job = await readiness_pool.fetchrow(
        "SELECT kind, payload FROM jobs WHERE kind='publish_preview'"
    )
    assert job["kind"] == "publish_preview"
    assert job["payload"]["draft_id"] == str(city_result.draft_id)


class FakePublisher:
    def __init__(self, pool, worktree_root: Path) -> None:
        self.pool = pool
        self.worktree_root = worktree_root

    async def build_preview(self, publication_id, payload):
        row = await self.pool.fetchrow(
            "SELECT base_commit, revision_hash FROM publication_jobs WHERE id=$1",
            publication_id,
        )
        await self.pool.execute(
            """
            UPDATE publication_jobs SET status='preview_ready',
                base_commit=$2, preview_nonce_hash=repeat('0',64),
                preview_expires_at=now()+interval '30 minutes',
                screenshot_manifest_hash=$3, artifacts_invalidated_at=NULL
            WHERE id=$1
            """,
            publication_id,
            "a" * 40,
            "e" * 64,
        )
        return PublicationPreview(
            publication_id=publication_id,
            base_commit="a" * 40,
            revision_hash=row["revision_hash"],
            nonce="preview-nonce",
            diff_summary="city-content.json | 2 +",
            worktree=self.worktree_root / str(publication_id),
            screenshot_manifest_hash="e" * 64,
            artifacts=(),
        )

    async def confirm_and_push(self, publication_id, nonce, actor_id, manifest_hash):
        assert nonce == "preview-nonce"
        assert manifest_hash == "e" * 64
        await self.pool.execute(
            """
            UPDATE publication_jobs SET status='published', confirmed_by=$2,
                main_commit=$3, static_commit=$4 WHERE id=$1
            """,
            publication_id,
            actor_id,
            "b" * 40,
            "c" * 40,
        )
        return "b" * 40, "c" * 40

    async def cleanup(self, _worktree):
        return None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_worker_requires_second_button_before_commit_and_reports_deploy(
    readiness_pool, tmp_path
):
    telegram_id = 778899
    admin_id = await readiness_pool.fetchval(
        """
        INSERT INTO users(phone_e164, display_name, telegram_id)
        VALUES ('+77070000001','Publishing admin',$1) RETURNING id
        """,
        telegram_id,
    )
    await readiness_pool.execute(
        "INSERT INTO user_roles(user_id, role_name) VALUES ($1,'superadmin')", admin_id
    )
    await readiness_pool.execute(
        """
        INSERT INTO notification_subscriptions(user_id, event_type)
        VALUES ($1,'publication_status')
        """,
        admin_id,
    )
    session_id = await readiness_pool.fetchval(
        """
        INSERT INTO conversation_sessions(user_id, workflow, status)
        VALUES ($1,'publish','completed') RETURNING id
        """,
        admin_id,
    )
    draft_id = await readiness_pool.fetchval(
        """
        INSERT INTO drafts(
            session_id, entity_type, status, current_revision, approved_revision,
            created_by, updated_by
        ) VALUES ($1,'city','approved',1,1,$2,$2) RETURNING id
        """,
        session_id,
        admin_id,
    )
    content = {"ok": True, "version": 1, "generatedAt": "stable", "cities": []}
    from floorball_bot.workflow import canonical_hash

    await readiness_pool.execute(
        """
        INSERT INTO draft_revisions(draft_id, revision, content, content_hash, created_by)
        VALUES ($1,1,$2::jsonb,$3,$4)
        """,
        draft_id,
        content,
        canonical_hash(content),
        admin_id,
    )
    await readiness_pool.execute(
        """
        INSERT INTO jobs(kind, payload, idempotency_key)
        VALUES ('publish_preview',$1::jsonb,'preview-worker-test')
        """,
        {
            "draft_id": str(draft_id),
            "actor_id": str(admin_id),
            "chat_id": telegram_id,
        },
    )
    publisher = FakePublisher(readiness_pool, tmp_path)
    worker = Worker(
        readiness_pool,
        extractor=object(),
        transcriber=object(),
        publisher=publisher,
    )
    preview_job = await claim_job(readiness_pool, worker_id="test-preview")
    assert preview_job is not None
    await worker.process(preview_job)

    preview_event = await readiness_pool.fetchval(
        "SELECT payload FROM outbox_events WHERE idempotency_key LIKE 'preview-ready:%'"
    )
    assert "Даю добро: commit и push" in str(preview_event["reply_markup"])
    assert await readiness_pool.fetchval(
        "SELECT status FROM drafts WHERE id=$1", draft_id
    ) == "approved"

    callback_data = preview_event["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    ingress = TelegramIngress(object(), readiness_pool)
    update = Update(
        update_id=889901,
        callback_query=CallbackQuery(
            id="callback-2",
            from_user=User(id=telegram_id, is_bot=False, first_name="Publishing"),
            chat_instance="test-instance",
            data=callback_data,
        ),
    )
    assert await ingress.accept(update)
    confirm_job = await claim_job(readiness_pool, worker_id="test-confirm")
    assert confirm_job is not None
    assert confirm_job.kind == "publish_confirm"
    await worker.process(confirm_job)

    assert await readiness_pool.fetchval(
        "SELECT status FROM drafts WHERE id=$1", draft_id
    ) == "published"
    final_text = await readiness_pool.fetchval(
        "SELECT payload->>'text' FROM outbox_events "
        "WHERE idempotency_key LIKE 'publish-succeeded:%'"
    )
    assert "запускайте развёртывание репозитория" in final_text
