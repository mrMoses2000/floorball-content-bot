from __future__ import annotations

import hashlib
import json
import re
from typing import Literal, Protocol
from uuid import UUID

import asyncpg
from pydantic import BaseModel, ConfigDict, Field, field_validator

from floorball_bot.queue import enqueue_job, stable_idempotency_key

CONTACT_RECIPIENT = "Knff@gmail.com"
_EMAIL = re.compile(r"^[^\s@\r\n]+@[^\s@\r\n]+\.[^\s@\r\n]+$")


class ContactRequestConflict(ValueError):
    pass


class ContactRequestRateLimited(ValueError):
    pass


class ContactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    version: Literal[1]
    request_id: UUID = Field(alias="requestId")
    locale: Literal["ru", "kz", "en"]
    name: str = Field(min_length=1, max_length=160)
    reply_to: str = Field(alias="replyTo", min_length=3, max_length=254)
    subject: Literal["training", "tournaments", "club", "partnership", "other"]
    message: str = Field(min_length=3, max_length=5000)
    website: str = Field(default="", max_length=2000)

    @field_validator("name", "reply_to", "message", mode="before")
    @classmethod
    def trim_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("name", "reply_to")
    @classmethod
    def reject_header_controls(cls, value: str) -> str:
        if "\r" in value or "\n" in value or "\0" in value:
            raise ValueError("header control characters are forbidden")
        return value

    @field_validator("reply_to")
    @classmethod
    def validate_email(cls, value: str) -> str:
        if not _EMAIL.fullmatch(value):
            raise ValueError("invalid reply email")
        return value

    @field_validator("message")
    @classmethod
    def reject_nul(cls, value: str) -> str:
        if "\0" in value:
            raise ValueError("NUL is forbidden")
        return value

    def payload_hash(self) -> str:
        public = self.model_dump(mode="json", by_alias=True, exclude={"website"})
        encoded = json.dumps(
            public, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(encoded.encode()).hexdigest()


class ContactAcceptance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    status: Literal["accepted"] = "accepted"
    created: bool


class ContactDelivery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    locale: Literal["ru", "kz", "en"]
    name: str
    reply_to: str
    subject: Literal["training", "tournaments", "club", "partnership", "other"]
    message: str
    recipient: Literal["Knff@gmail.com"]


class ContactMailer(Protocol):
    async def send(self, delivery: ContactDelivery) -> str: ...


class FakeContactMailer:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.deliveries: list[ContactDelivery] = []

    async def send(self, delivery: ContactDelivery) -> str:
        self.deliveries.append(delivery)
        if self.error:
            raise self.error
        return f"fake:{delivery.request_id}"


async def accept_contact_request(
    pool: asyncpg.Pool,
    *,
    request: ContactRequest,
    client_fingerprint: str,
    max_per_window: int = 5,
) -> ContactAcceptance:
    """Persist a validated request and its delivery job in one transaction."""
    if not re.fullmatch(r"[0-9a-f]{64}", client_fingerprint):
        raise ValueError("client fingerprint must be a SHA-256 hex digest")
    if request.website:
        return ContactAcceptance(request_id=request.request_id, created=False)
    content_hash = request.payload_hash()
    async with pool.acquire() as connection, connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext($1))",
            f"contact:{client_fingerprint}",
        )
        existing = await connection.fetchrow(
            "SELECT payload_hash FROM contact_requests WHERE id=$1 FOR UPDATE",
            request.request_id,
        )
        if existing:
            if existing["payload_hash"] != content_hash:
                raise ContactRequestConflict("requestId is already bound to another payload")
            return ContactAcceptance(request_id=request.request_id, created=False)
        recent = await connection.fetchval(
            """
            SELECT count(*) FROM contact_requests
            WHERE client_fingerprint=$1 AND created_at >= now()-interval '10 minutes'
            """,
            client_fingerprint,
        )
        if recent >= max_per_window:
            raise ContactRequestRateLimited("contact request rate limit exceeded")
        await connection.execute(
            """
            INSERT INTO contact_requests(
                id, contract_version, payload_hash, locale, name, reply_to,
                subject, message, recipient, client_fingerprint
            ) VALUES ($1,1,$2,$3,$4,$5,$6,$7,$8,$9)
            """,
            request.request_id,
            content_hash,
            request.locale,
            request.name,
            request.reply_to,
            request.subject,
            request.message,
            CONTACT_RECIPIENT,
            client_fingerprint,
        )
        await enqueue_job(
            connection,
            kind="contact_delivery",
            payload={"request_id": str(request.request_id)},
            idempotency_key=stable_idempotency_key(
                "contact-delivery", request.request_id, content_hash
            ),
            max_attempts=5,
        )
    return ContactAcceptance(request_id=request.request_id, created=True)
