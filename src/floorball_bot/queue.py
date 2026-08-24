from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import asyncpg


@dataclass(frozen=True)
class ClaimedJob:
    id: UUID
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    idempotency_key: str


def stable_idempotency_key(namespace: str, *parts: object) -> str:
    body = json.dumps([namespace, *parts], ensure_ascii=False, sort_keys=True, default=str)
    return f"{namespace}:{hashlib.sha256(body.encode()).hexdigest()}"


async def enqueue_job(
    connection: asyncpg.Connection,
    *,
    kind: str,
    payload: dict[str, Any],
    idempotency_key: str,
    max_attempts: int = 5,
) -> UUID | None:
    return await connection.fetchval(
        """
        INSERT INTO jobs(kind, payload, idempotency_key, max_attempts)
        VALUES ($1,$2::jsonb,$3,$4)
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id
        """,
        kind,
        payload,
        idempotency_key,
        max_attempts,
    )


async def claim_job(
    pool: asyncpg.Pool,
    *,
    worker_id: str,
    lease_seconds: int = 300,
) -> ClaimedJob | None:
    async with pool.acquire() as connection, connection.transaction():
        row = await connection.fetchrow(
            """
            WITH candidate AS (
                SELECT id FROM jobs
                WHERE (
                    status IN ('pending','retry') AND available_at <= now()
                ) OR (
                    status='running' AND locked_at < now() - make_interval(secs => $2)
                )
                ORDER BY available_at, created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE jobs j
            SET status='running', locked_at=now(), locked_by=$1,
                attempts=j.attempts+1, updated_at=now()
            FROM candidate
            WHERE j.id=candidate.id
            RETURNING j.*
            """,
            worker_id,
            lease_seconds,
        )
    if not row:
        return None
    return ClaimedJob(
        id=row["id"],
        kind=row["kind"],
        payload=row["payload"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        idempotency_key=row["idempotency_key"],
    )


async def complete_job(pool: asyncpg.Pool, job_id: UUID) -> None:
    await pool.execute(
        """
        UPDATE jobs SET status='succeeded', locked_at=NULL, locked_by=NULL, updated_at=now()
        WHERE id=$1
        """,
        job_id,
    )


async def fail_job(
    pool: asyncpg.Pool,
    job: ClaimedJob,
    error: str,
    *,
    retryable: bool,
) -> None:
    terminal = not retryable or job.attempts >= job.max_attempts
    delay = min(900.0, 2 ** min(job.attempts, 8)) + random.uniform(0, 1)  # noqa: S311
    await pool.execute(
        """
        UPDATE jobs
        SET status=$2, available_at=$3, locked_at=NULL, locked_by=NULL,
            last_error=$4, updated_at=now()
        WHERE id=$1
        """,
        job.id,
        "dead" if terminal else "retry",
        datetime.now(UTC) if terminal else datetime.now(UTC) + timedelta(seconds=delay),
        error[-4000:],
    )


async def enqueue_outbox(
    connection: asyncpg.Connection,
    *,
    event_type: str,
    payload: dict[str, Any],
    idempotency_key: str,
) -> UUID | None:
    return await connection.fetchval(
        """
        INSERT INTO outbox_events(event_type, payload, idempotency_key)
        VALUES ($1,$2::jsonb,$3)
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id
        """,
        event_type,
        payload,
        idempotency_key,
    )


async def accept_update(connection: asyncpg.Connection, update_id: int) -> bool:
    return (
        await connection.fetchval(
            """
            INSERT INTO processed_updates(update_id) VALUES ($1)
            ON CONFLICT DO NOTHING RETURNING update_id
            """,
            update_id,
        )
        is not None
    )
