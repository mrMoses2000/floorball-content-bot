from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_miniapp_api import signed_init_data

from floorball_bot.db import create_pool, run_migrations
from floorball_bot.miniapp_api import create_miniapp_app

pytestmark = pytest.mark.postgres


@pytest.mark.asyncio
async def test_miniapp_http_auth_resumption_and_stale_writes(tmp_path):
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not configured")
    pool = await create_pool(dsn)
    try:
        if not (await pool.fetchval("SELECT current_database()")).endswith("_test"):
            raise RuntimeError("refusing a non-test database")
        await run_migrations(pool, Path(__file__).parents[1] / "migrations")
        await pool.execute("TRUNCATE users RESTART IDENTITY CASCADE")
        user = await pool.fetchval(
            "INSERT INTO users(telegram_id,display_name) VALUES (789,'Editor') RETURNING id"
        )
        await pool.execute(
            "INSERT INTO user_roles(user_id,role_name) VALUES ($1,'superadmin')", user
        )
        app = create_miniapp_app(pool, bot_token="test-token", dist_root=tmp_path)  # noqa: S106
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/api/miniapp/v1/bootstrap")).status == 401
            headers = {"Authorization": "tma " + signed_init_data("test-token", telegram_id=789)}
            assert (await client.get("/api/miniapp/v1/bootstrap", headers=headers)).status == 200
            response = await client.post("/api/miniapp/v1/sessions/strategy", headers=headers)
            assert response.status == 201
            session = await pool.fetchval(
                "SELECT id FROM conversation_sessions WHERE user_id=$1 AND workflow='strategy'",
                user,
            )
            path = f"/api/miniapp/v1/sessions/{session}/fields/mission"
            change = {"request_id": str(uuid4()), "revision": 1, "value": "Saved mission"}
            assert (await client.patch(path, headers=headers, json=change)).status == 200
            assert (await client.patch(path, headers=headers, json=change)).status == 200
            change["request_id"] = str(uuid4())
            assert (await client.patch(path, headers=headers, json=change)).status == 409
            stranger = {
                "Authorization": "tma " + signed_init_data("test-token", telegram_id=999)
            }
            assert (await client.patch(path, headers=stranger, json=change)).status == 403
            assert (await client.post(
                "/api/miniapp/v1/sessions/history", headers=headers
            )).status == 201
            assert await pool.fetchval(
                "SELECT status FROM conversation_sessions WHERE id=$1", session
            ) == "paused"
            assert (await client.post(
                "/api/miniapp/v1/sessions/strategy", headers=headers
            )).status == 201
            assert await pool.fetchval(
                "SELECT count(*) FROM conversation_sessions "
                "WHERE user_id=$1 AND workflow='strategy'",
                user,
            ) == 1
            memory = await pool.fetchval(
                "SELECT structured_memory FROM conversation_memory WHERE session_id=$1", session
            )
            assert memory["fields"]["mission"] == "Saved mission"
    finally:
        await pool.close()
