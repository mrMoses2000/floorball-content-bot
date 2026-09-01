from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import asyncpg

from floorball_bot.domain import Actor
from floorball_bot.errors import AuthorizationError


@dataclass(frozen=True)
class CallbackToken:
    callback_data: str
    expires_at: datetime


async def create_callback(
    connection: asyncpg.Connection,
    *,
    actor: Actor,
    action: str,
    target_id: UUID,
    ttl_seconds: int = 900,
    revision_hash: str = "",
    manifest_hash: str = "",
) -> CallbackToken:
    nonce = secrets.token_urlsafe(12)
    nonce_hash = hashlib.sha256(nonce.encode()).hexdigest()
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    action_id = await connection.fetchval(
        """
        INSERT INTO callback_actions(
            actor_id, action, target_id, nonce_hash, expires_at,
            revision_hash, manifest_hash
        ) VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id
        """,
        actor.user_id,
        action,
        target_id,
        nonce_hash,
        expires_at,
        revision_hash,
        manifest_hash,
    )
    return CallbackToken(f"a:{action_id}:{nonce}", expires_at)


async def consume_callback(
    connection: asyncpg.Connection,
    *,
    actor: Actor,
    callback_data: str,
) -> tuple[str, UUID]:
    parts = callback_data.split(":", 2)
    if len(parts) != 3 or parts[0] != "a":
        raise AuthorizationError("invalid callback")
    try:
        action_id = UUID(parts[1])
    except ValueError as exc:
        raise AuthorizationError("invalid callback record") from exc
    nonce_hash = hashlib.sha256(parts[2].encode()).hexdigest()
    row = await connection.fetchrow(
        """
        SELECT callback.* FROM callback_actions callback
        WHERE callback.id=$1 AND callback.actor_id=$2 AND callback.nonce_hash=$3
          AND consumed_at IS NULL AND expires_at > now()
          AND (
            callback.manifest_hash='' OR EXISTS (
                SELECT 1 FROM publication_jobs publication
                WHERE publication.id=callback.target_id
                  AND publication.status='preview_ready'
                  AND publication.revision_hash=callback.revision_hash
                  AND publication.screenshot_manifest_hash=callback.manifest_hash
                  AND publication.artifacts_invalidated_at IS NULL
            )
          )
        FOR UPDATE
        """,
        action_id,
        actor.user_id,
        nonce_hash,
    )
    if not row:
        raise AuthorizationError("callback expired or belongs to another actor")
    await connection.execute("UPDATE callback_actions SET consumed_at=now() WHERE id=$1", action_id)
    return row["action"], row["target_id"]
