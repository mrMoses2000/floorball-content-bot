import os
from pathlib import Path
from uuid import uuid4

import pytest

from floorball_bot.callbacks import consume_callback, create_callback
from floorball_bot.db import create_pool, run_migrations
from floorball_bot.domain import Actor, Role
from floorball_bot.errors import AuthorizationError
from floorball_bot.workflow import add_revision, canonical_hash

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
    await pool.execute("TRUNCATE users, publication_jobs RESTART IDENTITY CASCADE")
    yield pool
    await pool.close()


async def seed_preview(pool):
    user_id = await pool.fetchval(
        "INSERT INTO users(phone_e164,display_name) VALUES ($1,'Admin') RETURNING id",
        f"+77{uuid4().int % 10**9:09d}",
    )
    await pool.execute(
        "INSERT INTO user_roles(user_id,role_name) VALUES ($1,'superadmin')", user_id
    )
    actor = Actor(
        user_id=user_id,
        telegram_id=1,
        roles=frozenset({Role.SUPERADMIN}),
        city_scopes=frozenset(),
    )
    session_id = await pool.fetchval(
        "INSERT INTO conversation_sessions(user_id,workflow) VALUES ($1,'trainer') RETURNING id",
        user_id,
    )
    first = {"value": "revision-1"}
    revision_hash = canonical_hash(first)
    draft_id = await pool.fetchval(
        """
        INSERT INTO drafts(
            session_id,entity_type,status,current_revision,approved_revision,
            created_by,updated_by
        ) VALUES ($1,'city','approved',1,1,$2,$2) RETURNING id
        """,
        session_id,
        user_id,
    )
    await pool.execute(
        """
        INSERT INTO draft_revisions(draft_id,revision,content,content_hash,created_by)
        VALUES ($1,1,$2::jsonb,$3,$4)
        """,
        draft_id,
        first,
        revision_hash,
        user_id,
    )
    manifest_hash = "b" * 64
    publication_id = await pool.fetchval(
        """
        INSERT INTO publication_jobs(
            draft_id,revision,revision_hash,status,requested_by,
            screenshot_manifest_hash,preview_expires_at
        ) VALUES ($1,1,$2,'preview_ready',$3,$4,now()+interval '30 minutes')
        RETURNING id
        """,
        draft_id,
        revision_hash,
        user_id,
        manifest_hash,
    )
    await pool.execute(
        """
        INSERT INTO publication_artifacts(
            publication_id,route,language,viewport,path,sha256,width,height,
            revision_hash,manifest_hash
        ) VALUES ($1,'/','ru','desktop-1280x720',$2,$3,1280,720,$4,$5)
        """,
        publication_id,
        f"/var/lib/floorball-test/{publication_id}.png",
        "c" * 64,
        revision_hash,
        manifest_hash,
    )
    return actor, draft_id, publication_id, revision_hash, manifest_hash


@pytest.mark.asyncio
async def test_new_revision_invalidates_publication_artifacts_and_bound_callbacks(pg_pool):
    actor, draft_id, publication_id, revision_hash, manifest_hash = await seed_preview(pg_pool)
    async with pg_pool.acquire() as connection, connection.transaction():
        callback = await create_callback(
            connection,
            actor=actor,
            action="preview_cancel",
            target_id=publication_id,
            revision_hash=revision_hash,
            manifest_hash=manifest_hash,
        )
        assert await add_revision(
            connection,
            draft_id=draft_id,
            actor=actor,
            content={"value": "revision-2"},
        ) == 2

    assert await pg_pool.fetchval(
        "SELECT status FROM publication_jobs WHERE id=$1", publication_id
    ) == "cancelled"
    assert not await pg_pool.fetchval(
        "SELECT valid FROM publication_artifacts WHERE publication_id=$1", publication_id
    )
    async with pg_pool.acquire() as connection, connection.transaction():
        with pytest.raises(AuthorizationError):
            await consume_callback(
                connection, actor=actor, callback_data=callback.callback_data
            )


@pytest.mark.asyncio
async def test_callback_rejects_mismatched_screenshot_manifest(pg_pool):
    actor, _draft_id, publication_id, revision_hash, _manifest_hash = await seed_preview(pg_pool)
    async with pg_pool.acquire() as connection, connection.transaction():
        callback = await create_callback(
            connection,
            actor=actor,
            action="preview_cancel",
            target_id=publication_id,
            revision_hash=revision_hash,
            manifest_hash="d" * 64,
        )
        with pytest.raises(AuthorizationError):
            await consume_callback(
                connection, actor=actor, callback_data=callback.callback_data
            )
