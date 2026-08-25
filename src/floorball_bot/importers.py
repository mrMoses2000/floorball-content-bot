from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg

from floorball_bot.domain import CityPayload, FederationPayload


@dataclass(frozen=True)
class ImportRecord:
    source: str
    entity_type: str
    entity_key: str
    content_hash: str
    content: dict[str, Any]
    sort_order: int = 0


def canonical_hash(value: Any) -> str:
    source = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(source.encode()).hexdigest()


def read_city_bundle(path: Path) -> list[ImportRecord]:
    payload = CityPayload.model_validate_json(path.read_text(encoding="utf-8"))
    return [
        ImportRecord(
            source="google_forms_import",
            entity_type="city",
            entity_key=city.slug,
            content_hash=canonical_hash(
                {
                    "sort_order": sort_order,
                    "value": city.model_dump(mode="json", exclude_none=True),
                }
            ),
            content=city.model_dump(mode="json", exclude_none=True),
            sort_order=sort_order,
        )
        for sort_order, city in enumerate(payload.cities)
    ]


def reconcile(existing: dict[str, str], incoming: list[ImportRecord]) -> dict[str, list[str]]:
    incoming_map = {record.entity_key: record.content_hash for record in incoming}
    return {
        "create": sorted(key for key in incoming_map if key not in existing),
        "update": sorted(
            key for key, value in incoming_map.items() if existing.get(key) not in {None, value}
        ),
        "unchanged": sorted(
            key for key, value in incoming_map.items() if existing.get(key) == value
        ),
        "missing_from_source": sorted(key for key in existing if key not in incoming_map),
    }


def read_federation_bundle(path: Path) -> list[ImportRecord]:
    payload = FederationPayload.model_validate_json(path.read_text(encoding="utf-8"))
    federation = payload.federation.model_dump(mode="json")
    return [
        ImportRecord(
            source="federation_bundle_import",
            entity_type="federation_section",
            entity_key=key,
            content_hash=canonical_hash(value),
            content={"value": value, "generatedAt": payload.generatedAt},
        )
        for key, value in sorted(federation.items())
    ]


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def _sync_city_clubs(
    connection: asyncpg.Connection, city_id: UUID, clubs: list[dict[str, Any]], actor: UUID | None
) -> None:
    keys: list[str] = []
    for index, club in enumerate(clubs, start=1):
        source_key = f"bundle:club:{index}"
        keys.append(source_key)
        await connection.execute(
            """
            INSERT INTO clubs(
                city_id, source_key, name, age_groups, notes, contact_name,
                contact_phone_private, contact_is_public, created_by, updated_by
            ) VALUES ($1,$2,$3,$4::jsonb,$5,$6,$7,$8,$9,$9)
            ON CONFLICT (city_id, source_key) WHERE source_key IS NOT NULL DO UPDATE SET
                name=EXCLUDED.name, age_groups=EXCLUDED.age_groups, notes=EXCLUDED.notes,
                contact_name=EXCLUDED.contact_name,
                contact_phone_private=EXCLUDED.contact_phone_private,
                contact_is_public=EXCLUDED.contact_is_public, status='active', deleted_at=NULL,
                updated_by=EXCLUDED.updated_by, updated_at=now(), revision=clubs.revision+1
            """,
            city_id,
            source_key,
            club["name"],
            club.get("ageGroups", []),
            club.get("notes", ""),
            club.get("contactName", ""),
            club.get("contactPhone", ""),
            bool(club.get("contactName") or club.get("contactPhone")),
            actor,
        )
    await connection.execute(
        """
        UPDATE clubs SET status='inactive', deleted_at=now(), updated_at=now(), revision=revision+1
        WHERE city_id=$1 AND source_key LIKE 'bundle:club:%'
          AND NOT (source_key = ANY($2::text[])) AND deleted_at IS NULL
        """,
        city_id,
        keys,
    )


async def _sync_city_schedules(
    connection: asyncpg.Connection,
    city_id: UUID,
    schedules: list[dict[str, Any]],
    actor: UUID | None,
) -> None:
    keys: list[str] = []
    for index, item in enumerate(schedules, start=1):
        source_key = f"bundle:schedule:{index}"
        keys.append(source_key)
        await connection.execute(
            """
            INSERT INTO training_schedules(
                city_id, source_key, day, time_text, venue, address, group_name,
                active, created_by, updated_by
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,TRUE,$8,$8)
            ON CONFLICT (city_id, source_key) WHERE source_key IS NOT NULL DO UPDATE SET
                day=EXCLUDED.day, time_text=EXCLUDED.time_text, venue=EXCLUDED.venue,
                address=EXCLUDED.address, group_name=EXCLUDED.group_name,
                active=TRUE, deleted_at=NULL, updated_by=EXCLUDED.updated_by,
                updated_at=now(), revision=training_schedules.revision+1
            """,
            city_id,
            source_key,
            item["day"],
            item["time"],
            item["venue"],
            item.get("address", ""),
            item.get("group", ""),
            actor,
        )
    await connection.execute(
        """
        UPDATE training_schedules
        SET active=FALSE, deleted_at=now(), updated_at=now(), revision=revision+1
        WHERE city_id=$1 AND source_key LIKE 'bundle:schedule:%'
          AND NOT (source_key = ANY($2::text[])) AND deleted_at IS NULL
        """,
        city_id,
        keys,
    )


async def _sync_city_players(
    connection: asyncpg.Connection,
    city_id: UUID,
    city_slug: str,
    players: list[dict[str, Any]],
    valid_from: date,
    actor: UUID | None,
) -> None:
    keys: list[str] = []
    membership_keys: list[str] = []
    for index, player in enumerate(players, start=1):
        public_id = str(player.get("id") or f"player-{index}")
        source_key = f"bundle:player:{city_slug}:{public_id}"
        membership_key = f"bundle:membership:{city_slug}:{public_id}"
        keys.append(source_key)
        membership_keys.append(membership_key)
        player_id = await connection.fetchval(
            """
            INSERT INTO players(
                source_key, photo_url, name_ru, name_kz, name_en,
                position_ru, position_kz, position_en, bio_ru, bio_kz, bio_en,
                selected_for_publication, approved_for_publication, created_by, updated_by
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,TRUE,TRUE,$12,$12)
            ON CONFLICT (source_key) WHERE source_key IS NOT NULL DO UPDATE SET
                photo_url=EXCLUDED.photo_url, name_ru=EXCLUDED.name_ru,
                name_kz=EXCLUDED.name_kz, name_en=EXCLUDED.name_en,
                position_ru=EXCLUDED.position_ru, position_kz=EXCLUDED.position_kz,
                position_en=EXCLUDED.position_en, bio_ru=EXCLUDED.bio_ru,
                bio_kz=EXCLUDED.bio_kz, bio_en=EXCLUDED.bio_en, status='active',
                selected_for_publication=TRUE, approved_for_publication=TRUE,
                deleted_at=NULL, updated_by=EXCLUDED.updated_by, updated_at=now(),
                revision=players.revision+1
            RETURNING id
            """,
            source_key,
            player.get("photo", ""),
            player.get("nameRu", ""),
            player.get("nameKz", ""),
            player.get("nameEn", ""),
            player.get("positionRu", ""),
            player.get("positionKz", ""),
            player.get("positionEn", ""),
            player.get("bioRu", ""),
            player.get("bioKz", ""),
            player.get("bioEn", ""),
            actor,
        )
        await connection.execute(
            """
            INSERT INTO player_city_memberships(
                player_id, city_id, source_key, valid_from
            ) VALUES ($1,$2,$3,$4)
            ON CONFLICT (source_key) WHERE source_key IS NOT NULL DO UPDATE SET
                player_id=EXCLUDED.player_id, city_id=EXCLUDED.city_id,
                valid_from=EXCLUDED.valid_from, valid_to=NULL
            """,
            player_id,
            city_id,
            membership_key,
            valid_from,
        )
    await connection.execute(
        """
        DELETE FROM player_city_memberships
        WHERE city_id=$1 AND source_key LIKE 'bundle:membership:%'
          AND NOT (source_key = ANY($2::text[]))
        """,
        city_id,
        membership_keys,
    )
    await connection.execute(
        """
        UPDATE players
        SET status='inactive', selected_for_publication=FALSE,
            approved_for_publication=FALSE, deleted_at=now(), updated_at=now(),
            revision=revision+1
        WHERE source_key LIKE $1 AND NOT (source_key = ANY($2::text[]))
          AND deleted_at IS NULL
        """,
        f"bundle:player:{city_slug}:%",
        keys,
    )


async def _sync_city_gallery(
    connection: asyncpg.Connection, city_id: UUID, gallery: list[dict[str, Any]]
) -> None:
    keys: list[str] = []
    for index, item in enumerate(gallery, start=1):
        public_id = str(item.get("id") or f"gallery-{index}")
        source_key = f"bundle:gallery:{public_id}"
        keys.append(source_key)
        await connection.execute(
            """
            INSERT INTO city_gallery_items(
                city_id, source_key, public_id, src, thumbnail, width, height,
                alt_ru, alt_kz, alt_en, caption_ru, caption_kz, caption_en,
                author, taken_at, sort_order
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
            ON CONFLICT (city_id, source_key) DO UPDATE SET
                public_id=EXCLUDED.public_id, src=EXCLUDED.src,
                thumbnail=EXCLUDED.thumbnail, width=EXCLUDED.width,
                height=EXCLUDED.height, alt_ru=EXCLUDED.alt_ru, alt_kz=EXCLUDED.alt_kz,
                alt_en=EXCLUDED.alt_en, caption_ru=EXCLUDED.caption_ru,
                caption_kz=EXCLUDED.caption_kz, caption_en=EXCLUDED.caption_en,
                author=EXCLUDED.author, taken_at=EXCLUDED.taken_at,
                selected_for_publication=TRUE, approved_for_publication=TRUE,
                sort_order=EXCLUDED.sort_order, deleted_at=NULL, updated_at=now()
            """,
            city_id,
            source_key,
            public_id,
            item["src"],
            item.get("thumbnail") or item["src"],
            item.get("width"),
            item.get("height"),
            item.get("altRu", ""),
            item.get("altKz", ""),
            item.get("altEn", ""),
            item.get("captionRu", ""),
            item.get("captionKz", ""),
            item.get("captionEn", ""),
            item.get("author", ""),
            item.get("takenAt", ""),
            index,
        )
    await connection.execute(
        """
        UPDATE city_gallery_items
        SET selected_for_publication=FALSE, approved_for_publication=FALSE,
            deleted_at=now(), updated_at=now()
        WHERE city_id=$1 AND source_key LIKE 'bundle:gallery:%'
          AND NOT (source_key = ANY($2::text[])) AND deleted_at IS NULL
        """,
        city_id,
        keys,
    )


async def latest_import_hashes(
    pool: asyncpg.Pool, *, source: str, entity_type: str
) -> dict[str, str]:
    rows = await pool.fetch(
        """
        SELECT DISTINCT ON (entity_key) entity_key, content_hash
        FROM import_snapshots
        WHERE source=$1 AND entity_type=$2
        ORDER BY entity_key, imported_at DESC
        """,
        source,
        entity_type,
    )
    return {row["entity_key"]: row["content_hash"] for row in rows}


async def apply_city_import(
    connection: asyncpg.Connection,
    records: list[ImportRecord],
    *,
    imported_by: UUID | None = None,
) -> int:
    imported = 0
    for record in records:
        already_imported = await connection.fetchval(
            """
            SELECT 1 FROM import_snapshots
            WHERE source=$1 AND entity_type=$2 AND entity_key=$3 AND content_hash=$4
            """,
            record.source,
            record.entity_type,
            record.entity_key,
            record.content_hash,
        )
        if already_imported:
            continue
        value = record.content
        city_id = await connection.fetchval(
            """
            INSERT INTO cities(
                slug, name_ru, name_kz, name_en, locative_ru, locative_kz,
                locative_en, region, longitude, latitude, hero_url,
                players_estimate, coaches_estimate, clubs_estimate, data_status,
                public_updated_at, public_sort_order, created_by, updated_by
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$18)
            ON CONFLICT (slug) DO UPDATE SET
                name_ru=EXCLUDED.name_ru, name_kz=EXCLUDED.name_kz,
                name_en=EXCLUDED.name_en, locative_ru=EXCLUDED.locative_ru,
                locative_kz=EXCLUDED.locative_kz, locative_en=EXCLUDED.locative_en,
                region=EXCLUDED.region, longitude=EXCLUDED.longitude,
                latitude=EXCLUDED.latitude, hero_url=EXCLUDED.hero_url,
                players_estimate=EXCLUDED.players_estimate,
                coaches_estimate=EXCLUDED.coaches_estimate,
                clubs_estimate=EXCLUDED.clubs_estimate, data_status=EXCLUDED.data_status,
                public_updated_at=EXCLUDED.public_updated_at,
                public_sort_order=EXCLUDED.public_sort_order,
                updated_by=EXCLUDED.updated_by,
                updated_at=now(), revision=cities.revision+1
            RETURNING id
            """,
            value["slug"],
            value["nameRu"],
            value["nameKz"],
            value["nameEn"],
            value.get("locativeRu", ""),
            value.get("locativeKz", ""),
            value.get("locativeEn", ""),
            value.get("region", ""),
            value.get("geoCoords", [None, None])[0] if value.get("geoCoords") else None,
            value.get("geoCoords", [None, None])[1] if value.get("geoCoords") else None,
            value.get("hero", "/assets/heroes/clubs.png"),
            value.get("players"),
            value.get("coaches"),
            value.get("clubs"),
            value.get("dataStatus", "verified-coach-data"),
            _timestamp(value.get("updatedAt")),
            record.sort_order,
            imported_by,
        )
        await connection.execute(
            """
            INSERT INTO city_content(
                city_id, description_ru, description_kz, description_en,
                history_ru, history_kz, history_en, created_by, updated_by
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$8)
            ON CONFLICT (city_id) DO UPDATE SET
                description_ru=EXCLUDED.description_ru,
                description_kz=EXCLUDED.description_kz,
                description_en=EXCLUDED.description_en,
                history_ru=EXCLUDED.history_ru, history_kz=EXCLUDED.history_kz,
                history_en=EXCLUDED.history_en, updated_by=EXCLUDED.updated_by,
                updated_at=now(), revision=city_content.revision+1
            """,
            city_id,
            value.get("descRu", ""),
            value.get("descKz", ""),
            value.get("descEn", ""),
            value.get("historyRu", ""),
            value.get("historyKz", ""),
            value.get("historyEn", ""),
            imported_by,
        )
        public_date = (_timestamp(value.get("updatedAt")) or datetime.now().astimezone()).date()
        await _sync_city_clubs(connection, city_id, value.get("clubs_list", []), imported_by)
        await _sync_city_schedules(
            connection, city_id, value.get("schedule", []), imported_by
        )
        await _sync_city_players(
            connection,
            city_id,
            value["slug"],
            value.get("players_list", []),
            public_date,
            imported_by,
        )
        await _sync_city_gallery(connection, city_id, value.get("gallery", []))
        inserted = await connection.fetchval(
            """
            INSERT INTO import_snapshots(
                source, entity_type, entity_key, content_hash, content, imported_by
            ) VALUES ($1,$2,$3,$4,$5::jsonb,$6)
            ON CONFLICT DO NOTHING RETURNING id
            """,
            record.source,
            record.entity_type,
            record.entity_key,
            record.content_hash,
            record.content,
            imported_by,
        )
        imported += int(inserted is not None)
    return imported


async def apply_federation_import(
    connection: asyncpg.Connection,
    records: list[ImportRecord],
    *,
    imported_by: UUID | None = None,
) -> int:
    imported = 0
    for record in records:
        already_imported = await connection.fetchval(
            """
            SELECT 1 FROM import_snapshots
            WHERE source=$1 AND entity_type=$2 AND entity_key=$3 AND content_hash=$4
            """,
            record.source,
            record.entity_type,
            record.entity_key,
            record.content_hash,
        )
        if already_imported:
            continue
        value = record.content["value"]
        if record.entity_key == "leadership":
            keys: list[str] = []
            for index, leader in enumerate(value, start=1):
                public_id = str(leader.get("id") or f"leader-{index}")
                source_key = f"bundle:leader:{public_id}"
                keys.append(source_key)
                await connection.execute(
                    """
                    INSERT INTO leadership_profiles(
                        source_key, name_ru, name_kz, role_ru, role_kz, bio_ru, bio_kz,
                        focus_ru, focus_kz, email_private, phone_private,
                        contacts_are_public, photo_url, sort_order, created_by, updated_by
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$15)
                    ON CONFLICT (source_key) WHERE source_key IS NOT NULL DO UPDATE SET
                        name_ru=EXCLUDED.name_ru, name_kz=EXCLUDED.name_kz,
                        role_ru=EXCLUDED.role_ru, role_kz=EXCLUDED.role_kz,
                        bio_ru=EXCLUDED.bio_ru, bio_kz=EXCLUDED.bio_kz,
                        focus_ru=EXCLUDED.focus_ru, focus_kz=EXCLUDED.focus_kz,
                        email_private=EXCLUDED.email_private,
                        phone_private=EXCLUDED.phone_private,
                        contacts_are_public=EXCLUDED.contacts_are_public,
                        photo_url=EXCLUDED.photo_url, sort_order=EXCLUDED.sort_order,
                        active=TRUE, deleted_at=NULL, updated_by=EXCLUDED.updated_by,
                        updated_at=now(), revision=leadership_profiles.revision+1
                    """,
                    source_key,
                    leader.get("nameRu", ""),
                    leader.get("nameKz", ""),
                    leader.get("roleRu", ""),
                    leader.get("roleKz", ""),
                    leader.get("bioRu", ""),
                    leader.get("bioKz", ""),
                    leader.get("focusRu", ""),
                    leader.get("focusKz", ""),
                    leader.get("email", ""),
                    leader.get("phone", ""),
                    bool(leader.get("email") or leader.get("phone")),
                    leader.get("photo", ""),
                    index,
                    imported_by,
                )
            await connection.execute(
                """
                UPDATE leadership_profiles
                SET active=FALSE, deleted_at=now(), updated_at=now(), revision=revision+1
                WHERE source_key LIKE 'bundle:leader:%'
                  AND NOT (source_key = ANY($1::text[])) AND deleted_at IS NULL
                """,
                keys,
            )
        else:
            await connection.execute(
                """
                INSERT INTO federation_sections(
                    section_key, item_key, public_content, created_by, updated_by
                ) VALUES ($1,'main',$2::jsonb,$3,$3)
                ON CONFLICT (section_key, item_key) DO UPDATE SET
                    public_content=EXCLUDED.public_content,
                    updated_by=EXCLUDED.updated_by, updated_at=now(),
                    deleted_at=NULL, revision=federation_sections.revision+1
                """,
                record.entity_key,
                value,
                imported_by,
            )
        inserted = await connection.fetchval(
            """
            INSERT INTO import_snapshots(
                source, entity_type, entity_key, content_hash, content, imported_by
            ) VALUES ($1,$2,$3,$4,$5::jsonb,$6)
            ON CONFLICT DO NOTHING RETURNING id
            """,
            record.source,
            record.entity_type,
            record.entity_key,
            record.content_hash,
            record.content,
            imported_by,
        )
        imported += int(inserted is not None)
    return imported
