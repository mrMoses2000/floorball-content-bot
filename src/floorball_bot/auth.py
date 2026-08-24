from __future__ import annotations

from uuid import UUID

import asyncpg

from floorball_bot.domain import Actor, Role, normalize_phone, verify_self_contact
from floorball_bot.errors import AuthorizationError


async def get_actor_by_telegram_id(
    connection: asyncpg.Connection, telegram_id: int
) -> Actor | None:
    user = await connection.fetchrow(
        "SELECT id, telegram_id, active FROM users WHERE telegram_id=$1 AND deleted_at IS NULL",
        telegram_id,
    )
    if not user:
        return None
    roles = await connection.fetch(
        "SELECT role_name FROM user_roles WHERE user_id=$1 AND revoked_at IS NULL", user["id"]
    )
    scopes = await connection.fetch(
        "SELECT city_id FROM user_city_scopes WHERE user_id=$1 AND revoked_at IS NULL", user["id"]
    )
    return Actor(
        user_id=user["id"],
        telegram_id=user["telegram_id"],
        active=user["active"],
        roles=frozenset(Role(row["role_name"]) for row in roles),
        city_scopes=frozenset(row["city_id"] for row in scopes),
    )


async def bind_self_contact(
    connection: asyncpg.Connection,
    *,
    sender_id: int,
    contact_user_id: int | None,
    raw_phone: str,
) -> Actor:
    verify_self_contact(sender_id=sender_id, contact_user_id=contact_user_id)
    phone = normalize_phone(raw_phone)
    row = await connection.fetchrow(
        """
        SELECT id, telegram_id FROM users
        WHERE phone_e164=$1 AND active=TRUE AND deleted_at IS NULL
        FOR UPDATE
        """,
        phone,
    )
    if not row:
        raise AuthorizationError("phone is not pre-authorized")
    if row["telegram_id"] not in (None, sender_id):
        raise AuthorizationError("account is already bound; superadmin approval is required")
    await connection.execute(
        """
        UPDATE users SET telegram_id=$2, telegram_bound_at=COALESCE(telegram_bound_at, now()),
                         updated_at=now(), revision=revision+1
        WHERE id=$1
        """,
        row["id"],
        sender_id,
    )
    actor = await get_actor_by_telegram_id(connection, sender_id)
    if actor is None:
        raise AuthorizationError("unable to bind authorized account")
    return actor


def require_active(actor: Actor | None) -> Actor:
    if actor is None or not actor.active:
        raise AuthorizationError("active account is required")
    return actor


def require_roles(actor: Actor, *allowed: Role) -> None:
    if not actor.has_any_role(*allowed):
        raise AuthorizationError("role is not permitted")


def require_city_scope(actor: Actor, city_id: UUID) -> None:
    require_roles(actor, Role.SUPERADMIN, Role.CITY_COACH, Role.REVIEWER)
    if not actor.can_access_city(city_id) and Role.REVIEWER not in actor.roles:
        raise AuthorizationError("city is outside assigned scope")
