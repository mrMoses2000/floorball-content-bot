from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg


async def record_heartbeat(
    pool: asyncpg.Pool, component: str, metadata: dict[str, Any] | None = None
) -> None:
    await pool.execute(
        """
        INSERT INTO runtime_heartbeats(component, observed_at, metadata)
        VALUES ($1,now(),$2::jsonb)
        ON CONFLICT (component) DO UPDATE
        SET observed_at=now(), metadata=EXCLUDED.metadata
        """,
        component,
        metadata or {},
    )


def _latest_backup(backup_root: Path) -> datetime | None:
    dumps = list((backup_root / "daily").glob("database-*.dump"))
    if not dumps:
        return None
    latest = max(dumps, key=lambda path: path.stat().st_mtime)
    return datetime.fromtimestamp(latest.stat().st_mtime, tz=UTC)


async def health_report(pool: asyncpg.Pool, state_root: Path, backup_root: Path) -> dict:
    db_ok = await pool.fetchval("SELECT 1") == 1
    dead_jobs = await pool.fetchval("SELECT count(*) FROM jobs WHERE status='dead'")
    dead_outbox = await pool.fetchval("SELECT count(*) FROM outbox_events WHERE status='dead'")
    latest_publication = await pool.fetchval(
        "SELECT max(updated_at) FROM publication_jobs WHERE status='published'"
    )
    publication_reconciliation = await pool.fetchrow(
        """
        SELECT
            count(*) FILTER (
                WHERE status IN ('confirming','pushing','remote_verified')
            )::integer AS active,
            count(*) FILTER (
                WHERE status IN ('confirming','pushing','remote_verified')
                  AND updated_at < now()-interval '15 minutes'
            )::integer AS stuck,
            count(*) FILTER (
                WHERE status='failed' AND reconciliation_error LIKE 'remote ref mismatch%'
            )::integer AS ref_mismatch
        FROM publication_jobs
        """
    )
    backlog = await pool.fetchrow(
        """
        SELECT
            count(*) FILTER (WHERE status='submitted')::integer AS submitted,
            count(*) FILTER (WHERE status='under_review')::integer AS under_review,
            count(*) FILTER (WHERE status='changes_requested')::integer AS changes_requested,
            min(created_at) FILTER (WHERE status='submitted') AS oldest_submitted_at
        FROM drafts
        """
    )
    heartbeat_rows = await pool.fetch(
        """
        SELECT component, observed_at,
               extract(epoch FROM (now()-observed_at))::double precision AS age_seconds
        FROM runtime_heartbeats
        """
    )
    heartbeats = {
        row["component"]: {
            "observed_at": row["observed_at"].isoformat(),
            "age_seconds": round(max(0.0, row["age_seconds"]), 1),
        }
        for row in heartbeat_rows
    }
    telegram_fresh = heartbeats.get("telegram_ingress", {}).get("age_seconds", 10_000) < 120
    worker_fresh = heartbeats.get("worker", {}).get("age_seconds", 10_000) < 420
    latest_backup = _latest_backup(backup_root)
    backup_age_seconds = (
        (datetime.now(UTC) - latest_backup).total_seconds() if latest_backup else None
    )
    backup_fresh = backup_age_seconds is not None and backup_age_seconds < 48 * 60 * 60
    usage = shutil.disk_usage(state_root.parent if state_root.exists() else Path("/"))
    percent = round(usage.used * 100 / usage.total, 2)
    return {
        "ok": (
            db_ok
            and percent < 90
            and dead_jobs == 0
            and dead_outbox == 0
            and telegram_fresh
            and worker_fresh
            and backup_fresh
            and publication_reconciliation["stuck"] == 0
            and publication_reconciliation["ref_mismatch"] == 0
        ),
        "checked_at": datetime.now(UTC).isoformat(),
        "database": "ok" if db_ok else "failed",
        "disk_used_percent": percent,
        "disk_status": "critical" if percent >= 90 else "warning" if percent >= 80 else "ok",
        "dead_jobs": dead_jobs,
        "dead_outbox": dead_outbox,
        "heartbeats": heartbeats,
        "telegram_polling": "ok" if telegram_fresh else "stale",
        "worker": "ok" if worker_fresh else "stale",
        "latest_backup": latest_backup.isoformat() if latest_backup else None,
        "backup_status": "ok" if backup_fresh else "missing_or_stale",
        "latest_publication": latest_publication.isoformat() if latest_publication else None,
        "publication_reconciliation": dict(publication_reconciliation),
        "editorial_backlog": {
            "submitted": backlog["submitted"],
            "under_review": backlog["under_review"],
            "changes_requested": backlog["changes_requested"],
            "oldest_submitted_at": (
                backlog["oldest_submitted_at"].isoformat()
                if backlog["oldest_submitted_at"]
                else None
            ),
        },
    }
