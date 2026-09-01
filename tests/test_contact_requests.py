import os
from email.utils import parseaddr
from pathlib import Path
from uuid import uuid4

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import ValidationError

from floorball_bot.contact_api import create_contact_app
from floorball_bot.contact_requests import (
    ContactDelivery,
    ContactRequest,
    ContactRequestConflict,
    ContactRequestRateLimited,
    FakeContactMailer,
    accept_contact_request,
)
from floorball_bot.db import create_pool, run_migrations
from floorball_bot.domain import ExtractedCityPatch
from floorball_bot.providers.codex import FakeExtractor
from floorball_bot.providers.transcription import FakeTranscriber
from floorball_bot.queue import claim_job
from floorball_bot.smtp_mailer import build_contact_email
from floorball_bot.worker import Worker

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
    await pool.execute(
        "TRUNCATE contact_requests, jobs, outbox_events RESTART IDENTITY CASCADE"
    )
    yield pool
    await pool.close()


def request_payload(*, request_id=None, message="Хочу узнать о тренировках.", website=""):
    return ContactRequest.model_validate(
        {
            "version": 1,
            "requestId": str(request_id or uuid4()),
            "locale": "ru",
            "name": "Иван",
            "replyTo": "ivan@example.kz",
            "subject": "training",
            "message": message,
            "website": website,
        }
    )


@pytest.mark.asyncio
async def test_accept_persists_before_queueing_fixed_recipient_delivery(pg_pool):
    request = request_payload()

    result = await accept_contact_request(
        pg_pool,
        request=request,
        client_fingerprint="a" * 64,
    )

    assert result.request_id == request.request_id
    assert result.status == "accepted"
    assert result.created
    row = await pg_pool.fetchrow("SELECT * FROM contact_requests WHERE id=$1", request.request_id)
    assert row["recipient"] == "Knff@gmail.com"
    assert row["reply_to"] == "ivan@example.kz"
    assert row["status"] == "pending"
    job = await pg_pool.fetchrow("SELECT kind, payload FROM jobs WHERE kind='contact_delivery'")
    assert job["payload"] == {"request_id": str(request.request_id)}


@pytest.mark.asyncio
async def test_same_request_is_idempotent_but_reused_id_with_new_payload_conflicts(pg_pool):
    request_id = uuid4()
    first = request_payload(request_id=request_id)

    created = await accept_contact_request(
        pg_pool, request=first, client_fingerprint="b" * 64
    )
    duplicate = await accept_contact_request(
        pg_pool, request=first, client_fingerprint="b" * 64
    )

    assert created.created
    assert not duplicate.created
    assert await pg_pool.fetchval("SELECT count(*) FROM contact_requests") == 1
    assert await pg_pool.fetchval("SELECT count(*) FROM jobs WHERE kind='contact_delivery'") == 1

    with pytest.raises(ContactRequestConflict):
        await accept_contact_request(
            pg_pool,
            request=request_payload(request_id=request_id, message="Подменённый текст"),
            client_fingerprint="b" * 64,
        )


@pytest.mark.asyncio
async def test_honeypot_returns_acceptance_without_persisting_or_queueing(pg_pool):
    result = await accept_contact_request(
        pg_pool,
        request=request_payload(website="https://spam.example"),
        client_fingerprint="c" * 64,
    )

    assert result.status == "accepted"
    assert not result.created
    assert await pg_pool.fetchval("SELECT count(*) FROM contact_requests") == 0
    assert await pg_pool.fetchval("SELECT count(*) FROM jobs") == 0


@pytest.mark.asyncio
async def test_rate_limit_counts_created_requests_not_idempotent_retries(pg_pool):
    fingerprint = "d" * 64
    first = request_payload()
    await accept_contact_request(
        pg_pool, request=first, client_fingerprint=fingerprint, max_per_window=2
    )
    await accept_contact_request(
        pg_pool, request=first, client_fingerprint=fingerprint, max_per_window=2
    )
    await accept_contact_request(
        pg_pool, request=request_payload(), client_fingerprint=fingerprint, max_per_window=2
    )

    with pytest.raises(ContactRequestRateLimited):
        await accept_contact_request(
            pg_pool,
            request=request_payload(),
            client_fingerprint=fingerprint,
            max_per_window=2,
        )


def test_contact_contract_rejects_header_injection_and_unknown_subject():
    injected = request_payload().model_dump(by_alias=True)
    injected["name"] = "Иван\nBcc: victim@example.com"
    with pytest.raises(ValidationError):
        ContactRequest.model_validate(injected)

    with pytest.raises(ValidationError):
        ContactRequest.model_validate(
            {
                **request_payload().model_dump(by_alias=True),
                "subject": "localized-visible-label",
            }
        )


@pytest.mark.asyncio
async def test_worker_delivers_fixed_recipient_and_marks_request_sent(pg_pool):
    request = request_payload()
    await accept_contact_request(
        pg_pool, request=request, client_fingerprint="e" * 64
    )
    job = await claim_job(pg_pool, worker_id="contact-test")
    mailer = FakeContactMailer()
    worker = Worker(
        pg_pool,
        extractor=FakeExtractor(ExtractedCityPatch(source_language="ru")),
        transcriber=FakeTranscriber(),
        contact_mailer=mailer,
    )

    await worker.process(job)

    assert await pg_pool.fetchval("SELECT status FROM jobs WHERE id=$1", job.id) == "succeeded"
    stored = await pg_pool.fetchrow(
        "SELECT status, attempts, external_id, sent_at FROM contact_requests WHERE id=$1",
        request.request_id,
    )
    assert stored["status"] == "sent"
    assert stored["attempts"] == 1
    assert stored["external_id"] == f"fake:{request.request_id}"
    assert stored["sent_at"] is not None
    assert len(mailer.deliveries) == 1
    delivery = mailer.deliveries[0]
    assert delivery.recipient == "Knff@gmail.com"
    assert delivery.reply_to == "ivan@example.kz"
    assert delivery.message == "Хочу узнать о тренировках."


def test_contact_email_has_fixed_to_safe_reply_to_and_trace_id():
    request = request_payload()
    delivery = ContactDelivery(
        request_id=request.request_id,
        locale=request.locale,
        name=request.name,
        reply_to=request.reply_to,
        subject=request.subject,
        message=request.message,
        recipient="Knff@gmail.com",
    )

    message = build_contact_email(delivery, sender="sender@gmail.com")

    assert message["To"] == "Knff@gmail.com"
    assert parseaddr(message["From"]) == ("floorball.kz", "sender@gmail.com")
    assert message["Reply-To"] == "ivan@example.kz"
    assert "\n" not in message["Subject"]
    assert message["Message-ID"] == f"<contact-{request.request_id}@floorball.kz>"
    assert message["X-Floorball-Request-ID"] == str(request.request_id)
    body = message.get_body(preferencelist=("plain",)).get_content()
    assert str(request.request_id) in body
    assert "Хочу узнать о тренировках." in body


@pytest.mark.asyncio
async def test_http_api_accepts_allowed_origin_and_rejects_cross_site_origin(pg_pool):
    app = create_contact_app(
        pg_pool,
        fingerprint_secret=b"test-secret-that-is-at-least-32-bytes-long",
        allowed_origins=("https://floorball.kz",),
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        health = await client.get("/healthz")
        assert health.status == 200
        assert await health.json() == {"status": "ok"}

        request = request_payload()
        response = await client.post(
            "/api/contact/v1/requests",
            json=request.model_dump(mode="json", by_alias=True),
            headers={"Origin": "https://floorball.kz"},
        )
        assert response.status == 202
        assert await response.json() == {
            "requestId": str(request.request_id),
            "status": "accepted",
        }
        assert response.headers["Access-Control-Allow-Origin"] == "https://floorball.kz"

        forbidden = await client.post(
            "/api/contact/v1/requests",
            json=request_payload().model_dump(mode="json", by_alias=True),
            headers={"Origin": "https://evil.example"},
        )
        assert forbidden.status == 403
        assert await pg_pool.fetchval("SELECT count(*) FROM contact_requests") == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_delivery_retries_are_bounded_and_terminal_failure_notifies_superadmin(pg_pool):
    telegram_id = uuid4().int % 1_000_000_000
    admin_id = await pg_pool.fetchval(
        """
        INSERT INTO users(phone_e164, display_name, telegram_id)
        VALUES ($1,'Contact Admin',$2) RETURNING id
        """,
        f"+77{uuid4().int % 10**9:09d}",
        telegram_id,
    )
    await pg_pool.execute(
        "INSERT INTO user_roles(user_id, role_name) VALUES ($1,'superadmin')", admin_id
    )
    request = request_payload()
    await accept_contact_request(
        pg_pool, request=request, client_fingerprint="f" * 64
    )
    worker = Worker(
        pg_pool,
        extractor=FakeExtractor(ExtractedCityPatch(source_language="ru")),
        transcriber=FakeTranscriber(),
        contact_mailer=FakeContactMailer(error=OSError("smtp unavailable")),
    )

    for attempt in range(1, 6):
        await pg_pool.execute(
            "UPDATE jobs SET available_at=now() WHERE kind='contact_delivery'"
        )
        job = await claim_job(pg_pool, worker_id=f"contact-retry-{attempt}")
        assert job is not None
        await worker.process(job)

    request_status = await pg_pool.fetchrow(
        "SELECT status, attempts FROM contact_requests WHERE id=$1", request.request_id
    )
    assert dict(request_status) == {"status": "dead", "attempts": 5}
    assert await pg_pool.fetchval(
        "SELECT status FROM jobs WHERE kind='contact_delivery'"
    ) == "dead"
    notification = await pg_pool.fetchval(
        "SELECT payload FROM outbox_events WHERE payload->>'chat_id'=$1",
        str(telegram_id),
    )
    assert str(request.request_id) in notification["text"]
    assert "не доставлена" in notification["text"]


@pytest.mark.asyncio
async def test_worker_recovers_request_left_sending_after_crash(pg_pool):
    request = request_payload()
    await accept_contact_request(
        pg_pool, request=request, client_fingerprint="1" * 64
    )
    first_job = await claim_job(pg_pool, worker_id="crashed-worker")
    await pg_pool.execute(
        """
        UPDATE contact_requests
        SET status='sending', attempts=1, locked_by='crashed-worker', locked_at=now()
        WHERE id=$1
        """,
        request.request_id,
    )
    await pg_pool.execute(
        """
        UPDATE jobs SET status='retry', available_at=now(), locked_by=NULL, locked_at=NULL
        WHERE id=$1
        """,
        first_job.id,
    )
    recovered_job = await claim_job(pg_pool, worker_id="recovery-worker")
    mailer = FakeContactMailer()
    worker = Worker(
        pg_pool,
        extractor=FakeExtractor(ExtractedCityPatch(source_language="ru")),
        transcriber=FakeTranscriber(),
        contact_mailer=mailer,
    )

    await worker.process(recovered_job)

    stored = await pg_pool.fetchrow(
        "SELECT status, attempts FROM contact_requests WHERE id=$1", request.request_id
    )
    assert dict(stored) == {"status": "sent", "attempts": 2}
    assert await pg_pool.fetchval(
        "SELECT status FROM jobs WHERE id=$1", recovered_job.id
    ) == "succeeded"
    assert len(mailer.deliveries) == 1
