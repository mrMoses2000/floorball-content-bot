from __future__ import annotations

import os
from pathlib import Path

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
    await pool.execute(
        "TRUNCATE jobs, outbox_events, runtime_heartbeats RESTART IDENTITY CASCADE"
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
