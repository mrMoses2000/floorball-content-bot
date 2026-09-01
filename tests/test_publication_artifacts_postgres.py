import asyncio
import hashlib
import os
from pathlib import Path
from uuid import uuid4

import pytest

from floorball_bot import publisher as publisher_module
from floorball_bot.callbacks import consume_callback, create_callback
from floorball_bot.db import create_pool, run_migrations
from floorball_bot.domain import Actor, Role
from floorball_bot.errors import AuthorizationError, RetryableProviderError, ValidationBlocked
from floorball_bot.publisher import GitPublisher
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


async def configure_reconcile_state(
    pool, publication_id, actor_id, *, status="pushing"
):
    await pool.execute(
        """
        UPDATE publication_jobs SET status=$2, confirmed_by=$3,
            base_commit=$4, base_static_commit=$5,
            expected_main_commit=$6, expected_static_commit=$7,
            publish_lease_owner='', publish_lease_expires_at=now()-interval '1 second'
        WHERE id=$1
        """,
        publication_id,
        status,
        actor_id,
        "a" * 40,
        "b" * 40,
        "c" * 40,
        "d" * 40,
    )


def remote_ref_runner(*, state, calls):
    async def run(*args, **_kwargs):
        calls.append(args)
        if args[:3] == ("git", "ls-remote", "origin"):
            if args[3] == "refs/heads/main":
                commit = "c" * 40 if state["pushed"] else state["main"]
            else:
                commit = "d" * 40 if state["pushed"] else state["static"]
            return f"{commit}\t{args[3]}\n"
        if args[:3] == ("git", "push", "--atomic"):
            state["pushed"] = True
        return ""

    return run


@pytest.mark.asyncio
async def test_reconciler_finishes_crash_after_atomic_push(pg_pool, tmp_path, monkeypatch):
    actor, _draft_id, publication_id, _revision_hash, _manifest_hash = await seed_preview(pg_pool)
    await configure_reconcile_state(pg_pool, publication_id, actor.user_id)
    worktrees = tmp_path / "worktrees"
    (worktrees / str(publication_id)).mkdir(parents=True)
    repository = tmp_path / "site"
    repository.mkdir()
    calls = []
    state = {"pushed": True, "main": "a" * 40, "static": "b" * 40}
    monkeypatch.setattr(
        publisher_module, "run_command", remote_ref_runner(state=state, calls=calls)
    )
    publisher = GitPublisher(pg_pool, repository, worktrees)

    async def cleanup(_worktree):
        return None

    monkeypatch.setattr(publisher, "cleanup", cleanup)

    assert await publisher.reconcile_publication(publication_id) == ("c" * 40, "d" * 40)
    row = await pg_pool.fetchrow(
        "SELECT status,main_commit,static_commit,publish_lease_owner FROM publication_jobs "
        "WHERE id=$1",
        publication_id,
    )
    assert dict(row) == {
        "status": "published",
        "main_commit": "c" * 40,
        "static_commit": "d" * 40,
        "publish_lease_owner": "",
    }
    assert not any(call[:3] == ("git", "push", "--atomic") for call in calls)


@pytest.mark.asyncio
async def test_reconciler_retries_crash_before_push_from_expected_commits(
    pg_pool, tmp_path, monkeypatch
):
    actor, _draft_id, publication_id, _revision_hash, _manifest_hash = await seed_preview(pg_pool)
    await configure_reconcile_state(pg_pool, publication_id, actor.user_id)
    worktrees = tmp_path / "worktrees"
    (worktrees / str(publication_id)).mkdir(parents=True)
    repository = tmp_path / "site"
    repository.mkdir()
    calls = []
    state = {"pushed": False, "main": "a" * 40, "static": "b" * 40}
    monkeypatch.setattr(
        publisher_module, "run_command", remote_ref_runner(state=state, calls=calls)
    )
    publisher = GitPublisher(pg_pool, repository, worktrees)

    async def cleanup(_worktree):
        return None

    monkeypatch.setattr(publisher, "cleanup", cleanup)

    assert await publisher.reconcile_publication(publication_id) == ("c" * 40, "d" * 40)
    assert any(call[:3] == ("git", "push", "--atomic") for call in calls)


@pytest.mark.asyncio
async def test_reconciler_blocks_mixed_remote_refs(pg_pool, tmp_path, monkeypatch):
    actor, _draft_id, publication_id, _revision_hash, _manifest_hash = await seed_preview(pg_pool)
    await configure_reconcile_state(pg_pool, publication_id, actor.user_id)
    worktrees = tmp_path / "worktrees"
    (worktrees / str(publication_id)).mkdir(parents=True)
    repository = tmp_path / "site"
    repository.mkdir()
    state = {"pushed": False, "main": "c" * 40, "static": "b" * 40}
    monkeypatch.setattr(
        publisher_module, "run_command", remote_ref_runner(state=state, calls=[])
    )
    publisher = GitPublisher(pg_pool, repository, worktrees)

    with pytest.raises(ValidationBlocked, match="manual recovery"):
        await publisher.reconcile_publication(publication_id)
    assert await pg_pool.fetchval(
        "SELECT status FROM publication_jobs WHERE id=$1", publication_id
    ) == "failed"


class HoldingPublisher(GitPublisher):
    def __init__(self, *args, claimed, release, **kwargs):
        super().__init__(*args, **kwargs)
        self.claimed = claimed
        self.release = release

    async def _advance_publication(self, publication_id, lease_owner):
        self.claimed.set()
        await self.release.wait()
        return "c" * 40, "d" * 40


@pytest.mark.asyncio
async def test_only_one_reconciler_can_own_a_publication_lease(pg_pool, tmp_path):
    actor, _draft_id, publication_id, _revision_hash, _manifest_hash = await seed_preview(pg_pool)
    await configure_reconcile_state(pg_pool, publication_id, actor.user_id)
    repository = tmp_path / "site"
    worktrees = tmp_path / "worktrees"
    repository.mkdir()
    worktrees.mkdir()
    claimed = asyncio.Event()
    release = asyncio.Event()
    publisher = HoldingPublisher(
        pg_pool, repository, worktrees, claimed=claimed, release=release
    )

    first = asyncio.create_task(publisher.reconcile_publication(publication_id))
    await claimed.wait()
    with pytest.raises(RetryableProviderError, match="active"):
        await publisher.reconcile_publication(publication_id)
    release.set()
    assert await first == ("c" * 40, "d" * 40)


class ClaimOnlyPublisher(GitPublisher):
    async def _advance_publication(self, publication_id, lease_owner):
        return "c" * 40, "d" * 40


@pytest.mark.asyncio
async def test_confirmation_claim_binds_actor_chat_and_manifest_under_short_lease(
    pg_pool, tmp_path
):
    actor, _draft_id, publication_id, _revision_hash, manifest_hash = await seed_preview(pg_pool)
    nonce = "exact-preview-nonce"
    await pg_pool.execute(
        "UPDATE publication_jobs SET preview_nonce_hash=$2 WHERE id=$1",
        publication_id,
        hashlib.sha256(nonce.encode()).hexdigest(),
    )
    repository = tmp_path / "site"
    worktrees = tmp_path / "worktrees"
    repository.mkdir()
    worktrees.mkdir()
    publisher = ClaimOnlyPublisher(pg_pool, repository, worktrees)

    assert await publisher.confirm_and_push(
        publication_id,
        nonce,
        actor.user_id,
        manifest_hash,
        chat_id=778899,
    ) == ("c" * 40, "d" * 40)
    row = await pg_pool.fetchrow(
        """
        SELECT status,confirmed_by,confirmation_chat_id,publish_lease_owner,
               publish_lease_expires_at>now() AS lease_live
        FROM publication_jobs WHERE id=$1
        """,
        publication_id,
    )
    assert row["status"] == "confirming"
    assert row["confirmed_by"] == actor.user_id
    assert row["confirmation_chat_id"] == 778899
    assert row["publish_lease_owner"].startswith("confirm-")
    assert row["lease_live"]


@pytest.mark.asyncio
async def test_confirmation_db_boundary_rejects_actor_without_active_superadmin(
    pg_pool, tmp_path
):
    actor, _draft_id, publication_id, _revision_hash, manifest_hash = await seed_preview(pg_pool)
    nonce = "exact-preview-nonce"
    await pg_pool.execute("DELETE FROM user_roles WHERE user_id=$1", actor.user_id)
    await pg_pool.execute(
        "UPDATE publication_jobs SET preview_nonce_hash=$2 WHERE id=$1",
        publication_id,
        hashlib.sha256(nonce.encode()).hexdigest(),
    )
    repository = tmp_path / "site"
    worktrees = tmp_path / "worktrees"
    repository.mkdir()
    worktrees.mkdir()
    publisher = ClaimOnlyPublisher(pg_pool, repository, worktrees)

    with pytest.raises(ValidationBlocked, match="invalid or expired"):
        await publisher.confirm_and_push(
            publication_id, nonce, actor.user_id, manifest_hash
        )
