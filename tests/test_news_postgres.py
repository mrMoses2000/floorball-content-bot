import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from floorball_bot.db import create_pool, run_migrations
from floorball_bot.dialogue.repository import DialogueSpecRepository
from floorball_bot.domain import Actor, Role
from floorball_bot.exporters import project_news_payload
from floorball_bot.projection.news import apply_approved_news_draft
from floorball_bot.workflow import canonical_hash

pytestmark = pytest.mark.postgres


def complete_fields():
    return {
        "scope": "national",
        "city_slug": "",
        "slug": "kazakhstan-cup-2026",
        "published_at": "2026-09-01T10:00:00Z",
        "title_ru": "Кубок Казахстана",
        "title_kz": "Қазақстан кубогы",
        "title_en": "",
        "excerpt_ru": "Подтверждённый анонс.",
        "excerpt_kz": "Расталған аңдатпа.",
        "excerpt_en": "",
        "body_ru": [{"type": "paragraph", "text": "Текст новости."}],
        "body_kz": [{"type": "paragraph", "text": "Жаңалық мәтіні."}],
        "body_en": [],
        "sources": [{"label": "Федерация", "url": "https://floorball.kz/documents"}],
        "media": None,
        "video_url": "",
        "publication_permission": True,
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
        "TRUNCATE news_projection_applications, news_items, drafts, conversation_sessions, "
        "users RESTART IDENTITY CASCADE"
    )
    yield pool
    await pool.close()


@pytest.mark.asyncio
async def test_approved_city_news_applies_idempotently_and_exports_public_only(pg_pool):
    city_id = await pg_pool.fetchval(
        """
        INSERT INTO cities(slug,name_ru,name_kz,name_en)
        VALUES ('almaty','Алматы','Алматы','Almaty') RETURNING id
        """
    )
    creator_id = await pg_pool.fetchval(
        """
        INSERT INTO users(phone_e164,display_name) VALUES ($1,'City author') RETURNING id
        """,
        f"+77{uuid4().int % 10**9:09d}",
    )
    reviewer_id = await pg_pool.fetchval(
        """
        INSERT INTO users(phone_e164,display_name) VALUES ($1,'Reviewer') RETURNING id
        """,
        f"+77{uuid4().int % 10**9:09d}",
    )
    await pg_pool.execute(
        "INSERT INTO user_roles(user_id,role_name) VALUES ($1,'city_coach'),($2,'reviewer')",
        creator_id,
        reviewer_id,
    )
    await pg_pool.execute(
        "INSERT INTO user_city_scopes(user_id,city_id) VALUES ($1,$2)", creator_id, city_id
    )
    loaded = DialogueSpecRepository().load("news")
    fields = {**complete_fields(), "scope": "city", "city_slug": "almaty"}
    context_hash = "a" * 64
    content = {
        "dialogue_mode": "news",
        "definition_version": loaded.spec.version,
        "definition_hash": loaded.sha256,
        "context_hash": context_hash,
        "fields": fields,
    }
    session_id = await pg_pool.fetchval(
        """
        INSERT INTO conversation_sessions(
            user_id,workflow,status,definition_version,definition_hash,context_hash
        ) VALUES ($1,'news','completed',$2,$3,$4) RETURNING id
        """,
        creator_id,
        loaded.spec.version,
        loaded.sha256,
        context_hash,
    )
    draft_id = await pg_pool.fetchval(
        """
        INSERT INTO drafts(
            session_id,entity_type,status,current_revision,approved_revision,created_by,updated_by
        ) VALUES ($1,'news','approved',1,1,$2,$3) RETURNING id
        """,
        session_id,
        creator_id,
        reviewer_id,
    )
    await pg_pool.execute(
        """
        INSERT INTO draft_revisions(draft_id,revision,content,content_hash,created_by)
        VALUES ($1,1,$2::jsonb,$3,$4)
        """,
        draft_id,
        content,
        canonical_hash(content),
        creator_id,
    )
    actor = Actor(
        user_id=reviewer_id,
        telegram_id=1,
        roles=frozenset({Role.REVIEWER}),
        city_scopes=frozenset(),
    )
    first = await apply_approved_news_draft(pg_pool, draft_id=draft_id, actor=actor)
    repeated = await apply_approved_news_draft(pg_pool, draft_id=draft_id, actor=actor)
    payload = await project_news_payload(
        pg_pool, generated_at=datetime(2026, 9, 1, 10, 0, 1, tzinfo=UTC)
    )

    assert first.applied
    assert not repeated.applied
    assert repeated.news_id == first.news_id
    assert payload.items[0].citySlug == "almaty"
    public = payload.model_dump(mode="json")
    assert "created_by" not in str(public)
    assert public["items"][0]["bodyRu"][0]["type"] == "paragraph"
