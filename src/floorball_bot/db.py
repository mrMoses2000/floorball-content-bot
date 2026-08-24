from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import asyncpg


async def _init_connection(connection: asyncpg.Connection) -> None:
    for type_name in ("json", "jsonb"):
        await connection.set_type_codec(
            type_name,
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )


async def create_pool(dsn: str, *, min_size: int = 1, max_size: int = 5) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        dsn=dsn,
        min_size=min_size,
        max_size=max_size,
        command_timeout=30,
        timeout=15,
        init=_init_connection,
    )


async def run_migrations(pool: asyncpg.Pool, migrations_dir: Path) -> list[str]:
    applied: list[str] = []
    files = sorted(migrations_dir.glob("[0-9][0-9][0-9]_*.sql"))
    async with pool.acquire() as connection:
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename TEXT PRIMARY KEY,
                checksum TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        for path in files:
            source = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(source.encode()).hexdigest()
            existing = await connection.fetchrow(
                "SELECT checksum FROM schema_migrations WHERE filename=$1", path.name
            )
            if existing:
                if existing["checksum"] != checksum:
                    raise RuntimeError(f"applied migration changed: {path.name}")
                continue
            async with connection.transaction():
                await connection.execute(source)
                await connection.execute(
                    "INSERT INTO schema_migrations(filename, checksum) VALUES ($1,$2)",
                    path.name,
                    checksum,
                )
            applied.append(path.name)
    return applied


@asynccontextmanager
async def transaction(pool: asyncpg.Pool) -> AsyncIterator[asyncpg.Connection]:
    async with pool.acquire() as connection, connection.transaction():
        yield connection


def row_dict(row: asyncpg.Record | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
