from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import asyncpg


async def health_report(pool: asyncpg.Pool, state_root: Path) -> dict:
    db_ok = await pool.fetchval("SELECT 1") == 1
    dead_jobs = await pool.fetchval("SELECT count(*) FROM jobs WHERE status='dead'")
    dead_outbox = await pool.fetchval("SELECT count(*) FROM outbox_events WHERE status='dead'")
    latest_publication = await pool.fetchval(
        "SELECT max(updated_at) FROM publication_jobs WHERE status='published'"
    )
    usage = shutil.disk_usage(state_root.parent if state_root.exists() else Path("/"))
    percent = round(usage.used * 100 / usage.total, 2)
    return {
        "ok": db_ok and percent < 90 and dead_jobs == 0,
        "checked_at": datetime.now(UTC).isoformat(),
        "database": "ok" if db_ok else "failed",
        "disk_used_percent": percent,
        "disk_status": "critical" if percent >= 90 else "warning" if percent >= 80 else "ok",
        "dead_jobs": dead_jobs,
        "dead_outbox": dead_outbox,
        "latest_publication": latest_publication.isoformat() if latest_publication else None,
    }
