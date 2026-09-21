import os
from pathlib import Path
from uuid import uuid4

import pytest

from floorball_bot.db import create_pool, run_migrations
from floorball_bot.official_documents import (
    InvalidOfficialDocument,
    send_missing_document_reminders,
    store_official_pdf,
)


def test_store_official_pdf_private_and_content_addressed(tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n")

    stored = store_official_pdf(source, tmp_path / "media", uuid4())

    assert stored.original_path.read_bytes() == source.read_bytes()
    assert stored.original_path.name == f"{stored.sha256}.pdf"
    assert stored.original_path.stat().st_mode & 0o777 == 0o600
    assert stored.original_path.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    "payload",
    [
        b"this is not a pdf",
        b"%PDF-1.4\nno eof marker",
        b"%PDF-1.4\n<</OpenAction 1 0 R>>\n%%EOF",
        b"%PDF-1.4\n<</JavaScript(test)>>\n%%EOF",
    ],
)
def test_store_official_pdf_rejects_invalid_or_active_content(tmp_path, payload):
    source = tmp_path / "unsafe.pdf"
    source.write_bytes(payload)

    with pytest.raises(InvalidOfficialDocument):
        store_official_pdf(source, tmp_path / "media", uuid4())


@pytest.fixture
async def document_pool():
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not configured")
    pool = await create_pool(dsn)
    await run_migrations(pool, Path(__file__).parents[1] / "migrations")
    await pool.execute(
        """
        TRUNCATE users, outbox_events, official_documents,
            official_document_upload_sessions, official_document_events
        RESTART IDENTITY CASCADE
        """
    )
    yield pool
    await pool.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_missing_document_reminder_targets_management_once_per_week(document_pool):
    admin_id = await document_pool.fetchval(
        """
        INSERT INTO users(phone_e164,display_name,telegram_id)
        VALUES ('+77010000031','Management',900031) RETURNING id
        """
    )
    coach_id = await document_pool.fetchval(
        """
        INSERT INTO users(phone_e164,display_name,telegram_id)
        VALUES ('+77010000032','Coach',900032) RETURNING id
        """
    )
    await document_pool.execute(
        """
        INSERT INTO user_roles(user_id,role_name)
        VALUES ($1,'federation_editor'),($2,'coach_form')
        """,
        admin_id,
        coach_id,
    )

    assert await send_missing_document_reminders(document_pool) == 1
    assert await send_missing_document_reminders(document_pool) == 0
    rows = await document_pool.fetch(
        """
        SELECT payload FROM outbox_events
        WHERE idempotency_key LIKE 'official-document-reminder:%'
        """
    )
    assert len(rows) == 1
    assert rows[0]["payload"]["chat_id"] == 900031
    assert "Устав федерации" in rows[0]["payload"]["text"]
    assert await document_pool.fetchval(
        "SELECT count(*) FROM official_document_events WHERE action='reminder_sent'"
    ) == 7
