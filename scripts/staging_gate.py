#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from floorball_bot.db import create_pool, run_migrations

ROOT = Path(__file__).resolve().parents[1]
SITE_APP = Path(os.getenv("STAGING_SITE_APP", "/home/moses/floorball.kz/app")).resolve()

STAGING_TESTS = (
    "tests/test_site_contract_roundtrip.py",
    "tests/test_trainer_projection_postgres.py",
    "tests/test_news_postgres.py",
    "tests/test_city_applications_postgres.py",
    "tests/test_publication_artifacts_postgres.py",
    "tests/test_publisher.py",
    "tests/test_telegram_e2e.py",
    "tests/test_official_documents.py",
)


def database_name(dsn: str) -> str:
    parsed = urlparse(dsn)
    query = parse_qs(parsed.query)
    return unquote(query.get("dbname", [parsed.path.lstrip("/")])[-1])


def validate_environment() -> str:
    dsn = os.getenv("STAGING_POSTGRES_DSN", "").strip()
    if not dsn:
        raise RuntimeError("STAGING_POSTGRES_DSN is required")
    name = database_name(dsn)
    if not name.endswith(("_staging", "_test")):
        raise RuntimeError(f"refusing non-staging database: {name}")
    if os.getenv("PUBLISH_ENABLED", "false").strip().casefold() not in {"false", "0", "no"}:
        raise RuntimeError("the first staging gate requires PUBLISH_ENABLED=false")
    if not SITE_APP.is_dir() or not (SITE_APP / "package.json").is_file():
        raise RuntimeError(f"site app is missing: {SITE_APP}")
    return dsn


async def migrate_staging(dsn: str) -> str:
    pool = await create_pool(dsn)
    try:
        current = await pool.fetchval("SELECT current_database()")
        if not current.endswith(("_staging", "_test")):
            raise RuntimeError(f"connected to unsafe database: {current}")
        await run_migrations(pool, ROOT / "migrations")
        return current
    finally:
        await pool.close()


def run(command: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)  # noqa: S603


def main() -> None:
    dsn = validate_environment()
    database = asyncio.run(migrate_staging(dsn))
    environment = dict(os.environ)
    environment["TEST_POSTGRES_DSN"] = dsn
    environment["PUBLISH_ENABLED"] = "false"
    run(
        [str(ROOT / ".venv/bin/pytest"), "-q", *STAGING_TESTS],
        cwd=ROOT,
        env=environment,
    )
    run(["npm", "test", "--", "--run"], cwd=SITE_APP, env=environment)
    run(["npm", "run", "lint"], cwd=SITE_APP, env=environment)
    run(["npm", "run", "build"], cwd=SITE_APP, env=environment)
    run(["npm", "audit", "--audit-level=high"], cwd=SITE_APP, env=environment)
    print(
        json.dumps(
            {
                "ok": True,
                "database": database,
                "publish_enabled": False,
                "git_remote": "local bare remotes only (enforced by tests)",
                "email": "excluded",
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
