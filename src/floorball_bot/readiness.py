from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import asyncpg

from floorball_bot.callbacks import create_callback
from floorball_bot.context_gateway import load_context_actor
from floorball_bot.domain import PublicCity, PublicFederation, Role
from floorball_bot.exporters import (
    project_city_payload,
    project_federation_payload,
    project_news_payload,
)
from floorball_bot.queue import enqueue_outbox, stable_idempotency_key
from floorball_bot.workflow import canonical_hash


@dataclass(frozen=True)
class ReadinessResult:
    entity_type: str
    entity_key: str
    ready: bool
    missing: tuple[str, ...]
    content_hash: str
    draft_id: UUID | None
    notifications_created: int


def city_missing(city: PublicCity) -> tuple[str, ...]:
    missing: list[str] = []
    required_text = {
        "nameRu": city.nameRu,
        "nameKz": city.nameKz,
        "nameEn": city.nameEn,
        "descRu": city.descRu,
        "descKz": city.descKz,
        "historyRu": city.historyRu,
        "historyKz": city.historyKz,
    }
    missing.extend(key for key, value in required_text.items() if not value.strip())
    for key, value in {
        "players": city.players,
        "coaches": city.coaches,
        "clubs": city.clubs,
    }.items():
        if value is None:
            missing.append(key)
    if city.clubs and not city.clubs_list:
        missing.append("clubs_list")
    if city.dataStatus != "verified-coach-data":
        missing.append("verified-coach-data")
    return tuple(missing)


def federation_missing(federation: PublicFederation) -> tuple[str, ...]:
    missing: list[str] = []
    mission = federation.mission
    for key, value in {
        "mission.statementRu": mission.statementRu,
        "mission.statementKz": mission.statementKz,
        "mission.visionRu": mission.visionRu,
        "mission.visionKz": mission.visionKz,
    }.items():
        if not value.strip():
            missing.append(key)
    if not federation.history:
        missing.append("history")
    for index, item in enumerate(federation.history):
        if not item.sourceUrl:
            missing.append(f"history[{index}].sourceUrl")
        if not item.titleRu or not item.titleKz:
            missing.append(f"history[{index}].titleRu/titleKz")
    if not federation.leadership:
        missing.append("leadership")
    for index, leader in enumerate(federation.leadership):
        if not all((leader.nameRu, leader.nameKz, leader.roleRu, leader.roleKz)):
            missing.append(f"leadership[{index}].identity")
        if not leader.bioRu or not leader.bioKz:
            missing.append(f"leadership[{index}].bioRu/bioKz")
    return tuple(missing)


async def _snapshot_time(connection: asyncpg.Connection, entity_type: str) -> datetime:
    if entity_type == "city":
        value = await connection.fetchval(
            """
            SELECT max(GREATEST(updated_at, COALESCE(public_updated_at, updated_at)))
            FROM cities WHERE active=TRUE AND deleted_at IS NULL
            """
        )
    elif entity_type == "federation":
        value = await connection.fetchval(
            """
            SELECT max(updated_at) FROM (
                SELECT updated_at FROM federation_sections WHERE deleted_at IS NULL
                UNION ALL
                SELECT updated_at FROM leadership_profiles WHERE deleted_at IS NULL
            ) source
            """
        )
    else:
        value = await connection.fetchval(
            "SELECT max(updated_at) FROM news_items WHERE deleted_at IS NULL"
        )
    return value or datetime(1970, 1, 1, tzinfo=UTC)


async def _ensure_snapshot_draft(
    connection: asyncpg.Connection,
    *,
    creator_id: UUID,
    entity_type: str,
    content: dict[str, Any],
) -> UUID:
    content_hash = canonical_hash(content)
    existing = await connection.fetchval(
        """
        SELECT d.id
        FROM drafts d
        JOIN draft_revisions r ON r.draft_id=d.id AND r.revision=d.current_revision
        WHERE d.entity_type=$1 AND r.content_hash=$2
          AND d.status IN ('under_review','approved','publishing','published')
        ORDER BY d.created_at DESC LIMIT 1
        """,
        entity_type,
        content_hash,
    )
    if existing:
        return existing
    session_id = await connection.fetchval(
        """
        INSERT INTO conversation_sessions(
            user_id, workflow, status, current_step, subject_key
        ) VALUES ($1,'publish','completed','snapshot_ready',$2)
        RETURNING id
        """,
        creator_id,
        entity_type,
    )
    draft_id = await connection.fetchval(
        """
        INSERT INTO drafts(
            session_id, entity_type, status, current_revision, created_by, updated_by
        ) VALUES ($1,$2,'under_review',1,$3,$3)
        RETURNING id
        """,
        session_id,
        entity_type,
        creator_id,
    )
    await connection.execute(
        """
        INSERT INTO draft_revisions(draft_id, revision, content, content_hash, created_by)
        VALUES ($1,1,$2::jsonb,$3,$4)
        """,
        draft_id,
        content,
        content_hash,
        creator_id,
    )
    return draft_id


async def scan_readiness(pool: asyncpg.Pool) -> tuple[ReadinessResult, ...]:
    """Create idempotent readiness records and notify bound publishing administrators."""
    async with pool.acquire() as connection, connection.transaction(
        isolation="repeatable_read", readonly=True
    ):
        city_time = await _snapshot_time(connection, "city")
        federation_time = await _snapshot_time(connection, "federation")
        news_time = await _snapshot_time(connection, "news")
        city_payload = await project_city_payload(connection, generated_at=city_time)
        federation_payload = await project_federation_payload(
            connection, generated_at=federation_time
        )
        news_payload = await project_news_payload(connection, generated_at=news_time)

    entities: list[tuple[str, str, tuple[str, ...], dict[str, Any], dict[str, Any]]] = []
    city_content = city_payload.model_dump(mode="json", exclude_none=True)
    for city in city_payload.cities:
        entities.append(
            (
                "city",
                city.slug,
                city_missing(city),
                city.model_dump(mode="json", exclude_none=True),
                city_content,
            )
        )
    federation_content = federation_payload.model_dump(mode="json", exclude_none=True)
    entities.append(
        (
            "federation",
            "federation",
            federation_missing(federation_payload.federation),
            federation_payload.federation.model_dump(mode="json", exclude_none=True),
            federation_content,
        )
    )
    news_content = news_payload.model_dump(mode="json", exclude_none=True)
    entities.append(
        (
            "news",
            "news",
            () if news_payload.items else ("items",),
            news_content,
            news_content,
        )
    )

    results: list[ReadinessResult] = []
    async with pool.acquire() as connection, connection.transaction():
        subscribers = await connection.fetch(
            """
            SELECT DISTINCT u.id, u.telegram_id
            FROM notification_subscriptions s
            JOIN users u ON u.id=s.user_id AND u.active=TRUE AND u.deleted_at IS NULL
            JOIN user_roles ur ON ur.user_id=u.id AND ur.role_name='superadmin'
                              AND ur.revoked_at IS NULL
            WHERE s.event_type='content_ready' AND s.enabled=TRUE
            ORDER BY u.id
            """
        )
        if not subscribers:
            return tuple(
                ReadinessResult(
                    entity_type=entity_type,
                    entity_key=entity_key,
                    ready=not missing,
                    missing=missing,
                    content_hash=canonical_hash(entity_value),
                    draft_id=None,
                    notifications_created=0,
                )
                for entity_type, entity_key, missing, entity_value, _payload in entities
            )
        creator_id = subscribers[0]["id"]
        for entity_type, entity_key, missing, entity_value, payload in entities:
            entity_hash = canonical_hash(entity_value)
            ready = not missing
            previous = await connection.fetchrow(
                """
                SELECT id, content_hash, snapshot_draft_id
                FROM content_readiness
                WHERE entity_type=$1 AND entity_key=$2
                FOR UPDATE
                """,
                entity_type,
                entity_key,
            )
            draft_id = previous["snapshot_draft_id"] if previous else None
            if ready and (not previous or previous["content_hash"] != entity_hash or not draft_id):
                draft_id = await _ensure_snapshot_draft(
                    connection,
                    creator_id=creator_id,
                    entity_type=entity_type,
                    content=payload,
                )
            readiness_id = await connection.fetchval(
                """
                INSERT INTO content_readiness(
                    entity_type, entity_key, content_hash, ready, missing,
                    snapshot_draft_id, checked_at
                ) VALUES ($1,$2,$3,$4,$5::jsonb,$6,now())
                ON CONFLICT (entity_type, entity_key) DO UPDATE SET
                    content_hash=EXCLUDED.content_hash,
                    ready=EXCLUDED.ready,
                    missing=EXCLUDED.missing,
                    snapshot_draft_id=EXCLUDED.snapshot_draft_id,
                    checked_at=now(),
                    notified_hash=CASE
                        WHEN content_readiness.content_hash=EXCLUDED.content_hash
                        THEN content_readiness.notified_hash ELSE '' END,
                    notified_at=CASE
                        WHEN content_readiness.content_hash=EXCLUDED.content_hash
                        THEN content_readiness.notified_at ELSE NULL END
                RETURNING id
                """,
                entity_type,
                entity_key,
                entity_hash,
                ready,
                list(missing),
                draft_id,
            )
            notifications = 0
            if ready and draft_id:
                for subscriber in subscribers:
                    if subscriber["telegram_id"] is None:
                        continue
                    exists = await connection.fetchval(
                        """
                        SELECT 1 FROM readiness_notifications
                        WHERE readiness_id=$1 AND user_id=$2 AND content_hash=$3
                        """,
                        readiness_id,
                        subscriber["id"],
                        entity_hash,
                    )
                    if exists:
                        continue
                    actor = await load_context_actor(connection, subscriber["id"])
                    if actor is None or Role.SUPERADMIN not in actor.roles:
                        continue
                    callback = await create_callback(
                        connection,
                        actor=actor,
                        action="approve_preview",
                        target_id=draft_id,
                        ttl_seconds=7 * 24 * 60 * 60,
                    )
                    label = (
                        f"город {entity_key}"
                        if entity_type == "city"
                        else "разделы федерации"
                        if entity_type == "federation"
                        else "новости"
                    )
                    await enqueue_outbox(
                        connection,
                        event_type="telegram_message",
                        payload={
                            "chat_id": subscriber["telegram_id"],
                            "text": (
                                f"Данные готовы: {label}. Все обязательные поля для сайта "
                                f"заполнены. Снимок {entity_hash[:12]}.\n\n"
                                "Нажмите кнопку, чтобы одобрить эту ревизию и собрать "
                                "проверочный preview. До второго подтверждения commit/push "
                                "не будет."
                            ),
                            "reply_markup": {
                                "inline_keyboard": [[{
                                    "text": "Одобрить и собрать preview",
                                    "callback_data": callback.callback_data,
                                }]]
                            },
                        },
                        idempotency_key=stable_idempotency_key(
                            "content-ready", readiness_id, subscriber["id"], entity_hash
                        ),
                    )
                    await connection.execute(
                        """
                        INSERT INTO readiness_notifications(readiness_id, user_id, content_hash)
                        VALUES ($1,$2,$3)
                        """,
                        readiness_id,
                        subscriber["id"],
                        entity_hash,
                    )
                    notifications += 1
                if notifications:
                    await connection.execute(
                        """
                        UPDATE content_readiness
                        SET notified_hash=$2, notified_at=now()
                        WHERE id=$1
                        """,
                        readiness_id,
                        entity_hash,
                    )
            results.append(
                ReadinessResult(
                    entity_type=entity_type,
                    entity_key=entity_key,
                    ready=ready,
                    missing=missing,
                    content_hash=entity_hash,
                    draft_id=draft_id,
                    notifications_created=notifications,
                )
            )
    return tuple(results)
