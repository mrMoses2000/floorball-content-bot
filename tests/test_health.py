from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from floorball_bot.db import create_pool, run_migrations
from floorball_bot.health import health_report, record_heartbeat


@pytest.fixture
async def health_pool():
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
        "TRUNCATE users, jobs, outbox_events, runtime_heartbeats RESTART IDENTITY CASCADE"
    )
    yield pool
    await pool.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_health_requires_fresh_runtime_and_backup(health_pool, tmp_path):
    report = await health_report(health_pool, tmp_path / "media", tmp_path / "backups")

    assert report["ok"] is False
    assert report["telegram_polling"] == "stale"
    assert report["backup_status"] == "missing_or_stale"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_health_accepts_fresh_heartbeats_and_backup(health_pool, tmp_path):
    await record_heartbeat(health_pool, "telegram_ingress", {"state": "polling"})
    await record_heartbeat(health_pool, "worker", {"state": "running"})
    daily = tmp_path / "backups" / "daily"
    daily.mkdir(parents=True)
    (daily / "database-20260827T000000Z.dump").write_bytes(b"test")

    report = await health_report(health_pool, tmp_path / "media", tmp_path / "backups")

    assert report["ok"] is True
    assert report["telegram_polling"] == "ok"
    assert report["worker"] == "ok"
    assert report["backup_status"] == "ok"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_health_reports_editorial_backlog_without_marking_it_as_runtime_failure(
    health_pool, tmp_path
):
    user_id = await health_pool.fetchval(
        """
        INSERT INTO users(phone_e164, display_name)
        VALUES ($1,'Health Editor') RETURNING id
        """,
        f"+77{uuid4().int % 10**9:09d}",
    )
    session_id = await health_pool.fetchval(
        "INSERT INTO conversation_sessions(user_id, workflow) VALUES ($1,'trainer') RETURNING id",
        user_id,
    )
    for status in ("submitted", "under_review", "changes_requested"):
        await health_pool.execute(
            """
            INSERT INTO drafts(session_id, entity_type, status, created_by, updated_by)
            VALUES ($1,'city',$2,$3,$3)
            """,
            session_id,
            status,
            user_id,
        )

    report = await health_report(health_pool, tmp_path / "media", tmp_path / "backups")

    assert report["editorial_backlog"] | {"oldest_submitted_at": None} == {
        "submitted": 1,
        "under_review": 1,
        "changes_requested": 1,
        "oldest_submitted_at": None,
    }
    assert report["editorial_backlog"]["oldest_submitted_at"] is not None
