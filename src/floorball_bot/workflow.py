from __future__ import annotations

import hashlib
import json
from uuid import UUID

import asyncpg

from floorball_bot.domain import Actor, DraftStatus, Role, can_transition
from floorball_bot.errors import AuthorizationError, InvalidTransition


def canonical_hash(content: dict) -> str:
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


async def add_revision(
    connection: asyncpg.Connection,
    *,
    draft_id: UUID,
    actor: Actor,
    content: dict,
    source_message_id: UUID | None = None,
) -> int:
    draft = await connection.fetchrow("SELECT * FROM drafts WHERE id=$1 FOR UPDATE", draft_id)
    if not draft:
        raise ValueError("draft not found")
    if draft["created_by"] != actor.user_id and Role.SUPERADMIN not in actor.roles:
        if draft["city_id"] is None or not actor.can_access_city(draft["city_id"]):
            raise AuthorizationError("draft is outside actor scope")
    content_hash = canonical_hash(content)
    existing_revision = await connection.fetchval(
        "SELECT revision FROM draft_revisions WHERE draft_id=$1 AND content_hash=$2",
        draft_id,
        content_hash,
    )
    if existing_revision is not None:
        return existing_revision
    revision = draft["current_revision"] + 1
    await connection.execute(
        """
        INSERT INTO draft_revisions(
            draft_id, revision, content, content_hash, source_message_id, created_by
        )
        VALUES ($1,$2,$3::jsonb,$4,$5,$6)
        """,
        draft_id,
        revision,
        content,
        content_hash,
        source_message_id,
        actor.user_id,
    )
    await connection.execute(
        "UPDATE drafts SET current_revision=$2, updated_by=$3, updated_at=now() WHERE id=$1",
        draft_id,
        revision,
        actor.user_id,
    )
    return revision


async def transition_draft(
    connection: asyncpg.Connection,
    *,
    draft_id: UUID,
    actor: Actor,
    target: DraftStatus,
    reason: str = "",
) -> bool:
    draft = await connection.fetchrow("SELECT * FROM drafts WHERE id=$1 FOR UPDATE", draft_id)
    if not draft:
        raise ValueError("draft not found")
    current = DraftStatus(draft["status"])
    if current == target:
        return False
    if not can_transition(current, target):
        raise InvalidTransition(f"{current} -> {target} is not allowed")
    reviewer_targets = {
        DraftStatus.UNDER_REVIEW,
        DraftStatus.CHANGES_REQUESTED,
        DraftStatus.APPROVED,
        DraftStatus.REJECTED,
    }
    if target in reviewer_targets and not actor.has_any_role(Role.REVIEWER, Role.SUPERADMIN):
        raise AuthorizationError("reviewer role is required")
    if (
        target in {DraftStatus.PUBLISHING, DraftStatus.PUBLISHED, DraftStatus.REVOKED}
        and Role.SUPERADMIN not in actor.roles
    ):
        raise AuthorizationError("superadmin role is required")
    await connection.execute(
        "UPDATE drafts SET status=$2, updated_by=$3, updated_at=now() WHERE id=$1",
        draft_id,
        target.value,
        actor.user_id,
    )
    action = {
        DraftStatus.SUBMITTED: "submitted",
        DraftStatus.UNDER_REVIEW: "review_started",
        DraftStatus.CHANGES_REQUESTED: "changes_requested",
        DraftStatus.APPROVED: "approved",
        DraftStatus.REJECTED: "rejected",
        DraftStatus.REVOKED: "revoked",
    }.get(target)
    if action:
        await connection.execute(
            """
            INSERT INTO approval_events(draft_id, revision, actor_id, action, reason)
            VALUES ($1,$2,$3,$4,$5)
            """,
            draft_id,
            draft["current_revision"],
            actor.user_id,
            action,
            reason[:2000],
        )
    return True
