from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

import asyncpg
from pydantic import BaseModel, ConfigDict

from floorball_bot.auth import require_roles
from floorball_bot.dialogue.repository import DialogueSpecRepository
from floorball_bot.domain import Actor, Role
from floorball_bot.projection.trainer import CityDirectoryEntry, plan_trainer_projection
from floorball_bot.workflow import canonical_hash


class ProjectionRejected(ValueError):
    """The approved draft cannot safely be applied to canonical data."""


class TrainerApplicationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    application_id: UUID
    draft_id: UUID
    revision: int
    city_id: UUID
    applied: bool
    before_hash: str
    after_hash: str


async def _canonical_snapshot(
    connection: asyncpg.Connection, city_id: UUID
) -> dict[str, Any]:
    city = await connection.fetchrow(
        """
        SELECT slug, players_estimate, coaches_estimate, clubs_estimate, data_status, revision
        FROM cities WHERE id=$1
        """,
        city_id,
    )
    content = await connection.fetchrow(
        """
        SELECT description_ru, description_kz, history_ru, history_kz, revision
        FROM city_content WHERE city_id=$1
        """,
        city_id,
    )
    clubs = await connection.fetch(
        """
        SELECT source_key, name, age_groups, notes, contact_name,
               contact_phone_private, contact_is_public, status, revision
        FROM clubs
        WHERE city_id=$1 AND source_key LIKE 'draft:%'
        ORDER BY source_key
        """,
        city_id,
    )
    schedules = await connection.fetch(
        """
        SELECT source_key, day, time_text, venue, address, group_name, active, revision
        FROM training_schedules
        WHERE city_id=$1 AND source_key LIKE 'draft:%'
        ORDER BY source_key
        """,
        city_id,
    )
    return {
        "city": dict(city) if city else None,
        "content": dict(content) if content else None,
        "clubs": [dict(row) for row in clubs],
        "schedules": [dict(row) for row in schedules],
    }


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProjectionRejected(f"{label} must be an object")
    return value


async def _upsert_city_content(
    connection: asyncpg.Connection,
    *,
    city_id: UUID,
    actor_id: UUID,
    description_ru: str | None,
    description_kz: str | None,
    history_ru: str | None,
    history_kz: str | None,
) -> None:
    await connection.execute(
        """
        INSERT INTO city_content(
            city_id, description_ru, description_kz, history_ru, history_kz,
            created_by, updated_by
        ) VALUES ($1,COALESCE($2,''),COALESCE($3,''),COALESCE($4,''),COALESCE($5,''),$6,$6)
        ON CONFLICT (city_id) DO UPDATE SET
            description_ru=COALESCE($2, city_content.description_ru),
            description_kz=COALESCE($3, city_content.description_kz),
            history_ru=COALESCE($4, city_content.history_ru),
            history_kz=COALESCE($5, city_content.history_kz),
            revision=city_content.revision+1,
            updated_by=$6,
            updated_at=now()
        """,
        city_id,
        description_ru,
        description_kz,
        history_ru,
        history_kz,
        actor_id,
    )


async def _replace_draft_owned_children(
    connection: asyncpg.Connection,
    *,
    draft_id: UUID,
    city_id: UUID,
    actor_id: UUID,
    plan,
) -> None:
    club_prefix = f"draft:{draft_id}:club:"
    schedule_prefix = f"draft:{draft_id}:schedule:"
    club_keys = [f"{club_prefix}{index}" for index in range(len(plan.clubs))]
    schedule_keys = [f"{schedule_prefix}{index}" for index in range(len(plan.schedules))]

    await connection.execute(
        """
        UPDATE clubs SET status='inactive', revision=revision+1, updated_by=$3, updated_at=now()
        WHERE city_id=$1 AND source_key LIKE $2 AND NOT (source_key=ANY($4::text[]))
        """,
        city_id,
        f"{club_prefix}%",
        actor_id,
        club_keys,
    )
    for index, club in enumerate(plan.clubs):
        await connection.execute(
            """
            INSERT INTO clubs(
                city_id, name, age_groups, notes, contact_name, contact_phone_private,
                contact_is_public, status, created_by, updated_by, source_key
            ) VALUES ($1,$2,$3::jsonb,$4,$5,$6,$7,'active',$8,$8,$9)
            ON CONFLICT (city_id, source_key) WHERE source_key IS NOT NULL DO UPDATE SET
                name=EXCLUDED.name,
                age_groups=EXCLUDED.age_groups,
                notes=EXCLUDED.notes,
                contact_name=EXCLUDED.contact_name,
                contact_phone_private=EXCLUDED.contact_phone_private,
                contact_is_public=EXCLUDED.contact_is_public,
                status='active',
                revision=clubs.revision+1,
                updated_by=EXCLUDED.updated_by,
                updated_at=now()
            """,
            city_id,
            club.name,
            list(club.age_groups),
            club.notes,
            club.contact_name,
            club.contact_phone_private,
            club.contact_is_public,
            actor_id,
            club_keys[index],
        )

    await connection.execute(
        """
        UPDATE training_schedules
        SET active=FALSE, revision=revision+1, updated_by=$3, updated_at=now()
        WHERE city_id=$1 AND source_key LIKE $2 AND NOT (source_key=ANY($4::text[]))
        """,
        city_id,
        f"{schedule_prefix}%",
        actor_id,
        schedule_keys,
    )
    for index, schedule in enumerate(plan.schedules):
        await connection.execute(
            """
            INSERT INTO training_schedules(
                city_id, day, time_text, venue, address, group_name, active,
                created_by, updated_by, source_key
            ) VALUES ($1,$2,$3,$4,$5,$6,TRUE,$7,$7,$8)
            ON CONFLICT (city_id, source_key) WHERE source_key IS NOT NULL DO UPDATE SET
                day=EXCLUDED.day,
                time_text=EXCLUDED.time_text,
                venue=EXCLUDED.venue,
                address=EXCLUDED.address,
                group_name=EXCLUDED.group_name,
                active=TRUE,
                revision=training_schedules.revision+1,
                updated_by=EXCLUDED.updated_by,
                updated_at=now()
            """,
            city_id,
            schedule.day,
            schedule.time,
            schedule.venue,
            schedule.address,
            schedule.group,
            actor_id,
            schedule_keys[index],
        )


async def apply_approved_trainer_draft(
    pool: asyncpg.Pool,
    *,
    draft_id: UUID,
    actor: Actor,
) -> TrainerApplicationResult:
    """Apply one immutable approved trainer revision exactly once.

    Authorization, pinned definition checks, completeness evaluation and every canonical write
    happen in one serializable transaction. A rejected plan therefore leaves no partial rows.
    """
    require_roles(actor, Role.REVIEWER, Role.SUPERADMIN)
    # The draft row is the per-draft mutex. READ COMMITTED is intentional: a waiter must see
    # the application row committed by the lock holder instead of retaining a stale snapshot.
    async with pool.acquire() as connection, connection.transaction():
        row = await connection.fetchrow(
            """
            SELECT d.id, d.entity_type, d.city_id, d.status, d.current_revision,
                   d.approved_revision, r.content, r.content_hash,
                   s.workflow, s.definition_version, s.definition_hash, s.context_hash,
                   u.preferred_language
            FROM drafts d
            JOIN conversation_sessions s ON s.id=d.session_id
            JOIN users u ON u.id=s.user_id
            LEFT JOIN draft_revisions r
              ON r.draft_id=d.id AND r.revision=d.approved_revision
            WHERE d.id=$1
            FOR UPDATE OF d
            """,
            draft_id,
        )
        if not row:
            raise ProjectionRejected("draft not found")
        if row["status"] != "approved" or row["approved_revision"] is None:
            raise ProjectionRejected("draft revision is not approved")
        if row["approved_revision"] != row["current_revision"]:
            raise ProjectionRejected("approved revision is stale")
        if row["entity_type"] != "city" or row["workflow"] != "trainer":
            raise ProjectionRejected("draft is not a trainer city dialogue")
        if row["content"] is None:
            raise ProjectionRejected("approved revision content is missing")

        loaded = DialogueSpecRepository().load("trainer")
        if row["definition_hash"] != loaded.sha256:
            raise ProjectionRejected("dialogue definition hash changed")
        if row["definition_version"] != loaded.spec.version:
            raise ProjectionRejected("dialogue definition version changed")
        content = _require_mapping(row["content"], "revision content")
        if content.get("dialogue_mode") != "trainer":
            raise ProjectionRejected("revision dialogue mode changed")
        if content.get("definition_hash") != row["definition_hash"]:
            raise ProjectionRejected("revision definition hash changed")
        if content.get("definition_version") != row["definition_version"]:
            raise ProjectionRejected("revision definition version changed")
        if content.get("context_hash", row["context_hash"]) != row["context_hash"]:
            raise ProjectionRejected("revision context hash changed")
        if canonical_hash(dict(content)) != row["content_hash"]:
            raise ProjectionRejected("revision content hash is invalid")

        existing = await connection.fetchrow(
            """
            SELECT id, city_id, before_hash, after_hash
            FROM canonical_projection_applications
            WHERE draft_id=$1 AND revision=$2 AND projection_kind='trainer_city'
            """,
            draft_id,
            row["approved_revision"],
        )
        if existing:
            return TrainerApplicationResult(
                application_id=existing["id"],
                draft_id=draft_id,
                revision=row["approved_revision"],
                city_id=existing["city_id"],
                applied=False,
                before_hash=existing["before_hash"],
                after_hash=existing["after_hash"],
            )

        directory_rows = await connection.fetch(
            """
            SELECT slug, name_ru, name_kz, name_en
            FROM cities WHERE active=TRUE AND deleted_at IS NULL ORDER BY slug
            """
        )
        directory = tuple(CityDirectoryEntry(**dict(item)) for item in directory_rows)
        fields = _require_mapping(content.get("fields"), "revision fields")
        plan = plan_trainer_projection(
            fields,
            preferred_language=row["preferred_language"],
            city_directory=directory,
        )
        if not plan.ready:
            raise ProjectionRejected(",".join(plan.blockers))
        city = await connection.fetchrow(
            "SELECT id FROM cities WHERE slug=$1 AND active=TRUE AND deleted_at IS NULL FOR UPDATE",
            plan.city_slug,
        )
        if not city:
            raise ProjectionRejected("resolved city is unavailable")
        city_id = city["id"]
        if row["city_id"] is not None and row["city_id"] != city_id:
            raise ProjectionRejected("draft city does not match selected city")

        before = canonical_hash(await _canonical_snapshot(connection, city_id))
        await connection.execute(
            """
            UPDATE cities SET
                players_estimate=$2,
                coaches_estimate=$3,
                clubs_estimate=$4,
                data_status='verified-coach-data',
                public_updated_at=now(),
                revision=revision+1,
                updated_by=$5,
                updated_at=now()
            WHERE id=$1
            """,
            city_id,
            plan.players_estimate,
            plan.coaches_estimate,
            plan.clubs_estimate,
            actor.user_id,
        )
        await _upsert_city_content(
            connection,
            city_id=city_id,
            actor_id=actor.user_id,
            description_ru=plan.description_ru,
            description_kz=plan.description_kz,
            history_ru=plan.history_ru,
            history_kz=plan.history_kz,
        )
        await _replace_draft_owned_children(
            connection,
            draft_id=draft_id,
            city_id=city_id,
            actor_id=actor.user_id,
            plan=plan,
        )
        after = canonical_hash(await _canonical_snapshot(connection, city_id))
        application_id = await connection.fetchval(
            """
            INSERT INTO canonical_projection_applications(
                draft_id, revision, projection_kind, content_hash,
                before_hash, after_hash, city_id, applied_by
            ) VALUES ($1,$2,'trainer_city',$3,$4,$5,$6,$7)
            RETURNING id
            """,
            draft_id,
            row["approved_revision"],
            row["content_hash"],
            before,
            after,
            city_id,
            actor.user_id,
        )
        return TrainerApplicationResult(
            application_id=application_id,
            draft_id=draft_id,
            revision=row["approved_revision"],
            city_id=city_id,
            applied=True,
            before_hash=before,
            after_hash=after,
        )
