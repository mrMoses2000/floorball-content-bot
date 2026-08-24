from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg

from floorball_bot.domain import CityPayload


@dataclass(frozen=True)
class ImportRecord:
    source: str
    entity_type: str
    entity_key: str
    content_hash: str
    content: dict[str, Any]


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
            content_hash=canonical_hash(city.model_dump(mode="json", exclude_none=True)),
            content=city.model_dump(mode="json", exclude_none=True),
        )
        for city in payload.cities
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
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("ok") is not True or payload.get("version") != 1:
        raise ValueError("unsupported federation payload")
    federation = payload.get("federation")
    if not isinstance(federation, dict):
        raise ValueError("federation object is required")
    required = {"mission", "history", "achievements", "roadmap", "leadership"}
    if not required.issubset(federation):
        raise ValueError("federation payload is incomplete")
    return [
        ImportRecord(
            source="federation_bundle_import",
            entity_type="federation_section",
            entity_key=key,
            content_hash=canonical_hash(value),
            content={"value": value},
        )
        for key, value in sorted(federation.items())
    ]


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
                locative_en, region, longitude, latitude, created_by, updated_by
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$11)
            ON CONFLICT (slug) DO UPDATE SET
                name_ru=EXCLUDED.name_ru, name_kz=EXCLUDED.name_kz,
                name_en=EXCLUDED.name_en, locative_ru=EXCLUDED.locative_ru,
                locative_kz=EXCLUDED.locative_kz, locative_en=EXCLUDED.locative_en,
                region=EXCLUDED.region, longitude=EXCLUDED.longitude,
                latitude=EXCLUDED.latitude, updated_by=EXCLUDED.updated_by,
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
