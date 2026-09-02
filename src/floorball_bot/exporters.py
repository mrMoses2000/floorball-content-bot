from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
from pydantic import ValidationError

from floorball_bot.domain import (
    CityPayload,
    FederationPayload,
    NewsPayload,
    PublicCity,
    PublicFederation,
    PublicNewsItem,
)
from floorball_bot.errors import ValidationBlocked

FORBIDDEN_PUBLIC_KEYS = {
    "telegram_id",
    "phone_e164",
    "raw_update",
    "original_text",
    "normalized_text",
    "transcript",
    "voice_path",
    "original_path",
    "internal_notes",
    "evidence_private",
    "consent_document",
    "creator_id",
    "updater_id",
}


def _iso_milliseconds(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def assert_private_fields_absent(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        forbidden = FORBIDDEN_PUBLIC_KEYS.intersection(value)
        if forbidden:
            raise ValidationBlocked(f"private fields at {path}: {sorted(forbidden)}")
        for key, nested in value.items():
            assert_private_fields_absent(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            assert_private_fields_absent(nested, f"{path}[{index}]")


def build_city_payload(cities: list[dict[str, Any]], *, generated_at: datetime) -> CityPayload:
    public: list[PublicCity] = []
    for raw in cities:
        eligible = raw.get("players_list", [])
        if len(eligible) > 15:
            raise ValidationBlocked("more than 15 players are selected for publication")
        try:
            public.append(PublicCity.model_validate(raw))
        except ValidationError as exc:
            raise ValidationBlocked(f"city public contract failed: {exc}") from exc
    payload = CityPayload(generatedAt=_iso_milliseconds(generated_at), cities=public)
    assert_private_fields_absent(payload.model_dump())
    return payload


def build_federation_payload(
    federation: dict[str, Any], *, generated_at: datetime
) -> FederationPayload:
    try:
        public = PublicFederation.model_validate(federation)
    except ValidationError as exc:
        raise ValidationBlocked(f"federation public contract failed: {exc}") from exc
    payload = FederationPayload(generatedAt=_iso_milliseconds(generated_at), federation=public)
    assert_private_fields_absent(payload.model_dump())
    return payload


def build_news_payload(items: list[dict[str, Any]], *, generated_at: datetime) -> NewsPayload:
    try:
        public = [PublicNewsItem.model_validate(item) for item in items]
    except ValidationError as exc:
        raise ValidationBlocked(f"news public contract failed: {exc}") from exc
    payload = NewsPayload(generatedAt=_iso_milliseconds(generated_at), items=public)
    assert_private_fields_absent(payload.model_dump())
    return payload


def _bundle_public_id(source_key: str | None, prefix: str, fallback: Any) -> str:
    marker = f"bundle:{prefix}:"
    if source_key and source_key.startswith(marker):
        return source_key[len(marker) :].split(":", 1)[-1]
    return str(fallback)


async def project_city_payload(
    connection: asyncpg.Connection | asyncpg.Pool, *, generated_at: datetime
) -> CityPayload:
    """Project approved normalized records into the exact frontend version-1 contract."""
    city_rows = await connection.fetch(
        """
        SELECT c.*, cc.description_ru, cc.description_kz, cc.description_en,
               cc.history_ru, cc.history_kz, cc.history_en
        FROM cities c
        LEFT JOIN city_content cc ON cc.city_id=c.id
        WHERE c.active=TRUE AND c.deleted_at IS NULL
        ORDER BY c.public_sort_order, c.slug
        """
    )
    city_ids = [row["id"] for row in city_rows]
    if not city_ids:
        return build_city_payload([], generated_at=generated_at)

    clubs = await connection.fetch(
        """
        SELECT c.*,
               (c.source_key LIKE 'bundle:club:%' OR EXISTS (
                   SELECT 1 FROM consents consent
                   WHERE consent.subject_type='contact' AND consent.subject_id=c.id
                     AND consent.scope='contact' AND consent.status='granted'
                     AND (consent.valid_from IS NULL OR consent.valid_from <= $2)
                     AND (consent.valid_until IS NULL OR consent.valid_until >= $2)
               )) AS contact_allowed
        FROM clubs c
        WHERE c.city_id=ANY($1::uuid[]) AND c.status='active' AND c.deleted_at IS NULL
        ORDER BY city_id, source_key NULLS LAST, name, id
        """,
        city_ids,
        generated_at,
    )
    schedules = await connection.fetch(
        """
        SELECT * FROM training_schedules
        WHERE city_id=ANY($1::uuid[]) AND active=TRUE AND deleted_at IS NULL
        ORDER BY city_id, source_key NULLS LAST, day, time_text, venue, id
        """,
        city_ids,
    )
    players = await connection.fetch(
        """
        SELECT DISTINCT ON (p.id) p.*, pcm.city_id,
               (p.source_key LIKE 'bundle:player:%' OR EXISTS (
                   SELECT 1 FROM consents consent
                   WHERE consent.subject_type='player' AND consent.subject_id=p.id
                     AND consent.scope='portrait' AND consent.status='granted'
                     AND (consent.valid_from IS NULL OR consent.valid_from <= $2)
                     AND (consent.valid_until IS NULL OR consent.valid_until >= $2)
               )) AS portrait_allowed
        FROM players p
        JOIN player_city_memberships pcm ON pcm.player_id=p.id
        WHERE pcm.city_id=ANY($1::uuid[])
          AND pcm.valid_from <= $2::date
          AND (pcm.valid_to IS NULL OR pcm.valid_to >= $2::date)
          AND p.status='active' AND p.deleted_at IS NULL
          AND p.selected_for_publication=TRUE AND p.approved_for_publication=TRUE
        ORDER BY p.id, pcm.valid_from DESC
        """,
        city_ids,
        generated_at,
    )
    gallery = await connection.fetch(
        """
        SELECT * FROM city_gallery_items
        WHERE city_id=ANY($1::uuid[]) AND deleted_at IS NULL
          AND selected_for_publication=TRUE AND approved_for_publication=TRUE
        ORDER BY city_id, sort_order, source_key, id
        """,
        city_ids,
    )

    by_city: dict[Any, dict[str, list[dict[str, Any]]]] = {
        city_id: {"clubs": [], "schedules": [], "players": [], "gallery": []}
        for city_id in city_ids
    }
    for row in clubs:
        by_city[row["city_id"]]["clubs"].append(
            {
                "name": row["name"],
                "ageGroups": list(row["age_groups"]),
                "notes": row["notes"],
                "contactName": (
                    row["contact_name"]
                    if row["contact_is_public"] and row["contact_allowed"]
                    else ""
                ),
                "contactPhone": (
                    row["contact_phone_private"]
                    if row["contact_is_public"] and row["contact_allowed"]
                    else ""
                ),
            }
        )
    for row in schedules:
        by_city[row["city_id"]]["schedules"].append(
            {
                "day": row["day"],
                "time": row["time_text"],
                "venue": row["venue"],
                "address": row["address"],
                "group": row["group_name"],
            }
        )
    for row in players:
        by_city[row["city_id"]]["players"].append(
            {
                "id": _bundle_public_id(row["source_key"], "player", row["id"]),
                "photo": row["photo_url"] if row["portrait_allowed"] else "",
                "nameRu": row["name_ru"],
                "nameKz": row["name_kz"],
                "nameEn": row["name_en"],
                "positionRu": row["position_ru"],
                "positionKz": row["position_kz"],
                "positionEn": row["position_en"],
                "bioRu": row["bio_ru"],
                "bioKz": row["bio_kz"],
                "bioEn": row["bio_en"],
            }
        )
    for row in gallery:
        by_city[row["city_id"]]["gallery"].append(
            {
                "id": row["public_id"],
                "src": row["src"],
                "thumbnail": row["thumbnail"],
                "width": row["width"],
                "height": row["height"],
                "altRu": row["alt_ru"],
                "altKz": row["alt_kz"],
                "altEn": row["alt_en"],
                "captionRu": row["caption_ru"],
                "captionKz": row["caption_kz"],
                "captionEn": row["caption_en"],
                "author": row["author"],
                "takenAt": row["taken_at"],
            }
        )

    projected: list[dict[str, Any]] = []
    for row in city_rows:
        nested = by_city[row["id"]]
        projected.append(
            {
                "slug": row["slug"],
                "nameRu": row["name_ru"],
                "nameKz": row["name_kz"],
                "nameEn": row["name_en"],
                "locativeRu": row["locative_ru"],
                "locativeKz": row["locative_kz"],
                "locativeEn": row["locative_en"],
                "region": row["region"],
                "regionAliases": list(row["region_aliases"]),
                "hero": row["hero_url"],
                "geoCoords": (
                    [row["longitude"], row["latitude"]]
                    if row["longitude"] is not None and row["latitude"] is not None
                    else None
                ),
                "players": row["players_estimate"],
                "coaches": row["coaches_estimate"],
                "clubs": row["clubs_estimate"],
                "clubs_list": nested["clubs"],
                "schedule": nested["schedules"],
                "players_list": nested["players"],
                "gallery": nested["gallery"],
                "dataStatus": row["data_status"],
                "updatedAt": _iso_milliseconds(row["public_updated_at"] or row["updated_at"]),
                "descRu": row["description_ru"] or "",
                "descKz": row["description_kz"] or "",
                "descEn": row["description_en"] or "",
                "historyRu": row["history_ru"] or "",
                "historyKz": row["history_kz"] or "",
                "historyEn": row["history_en"] or "",
            }
        )
    return build_city_payload(projected, generated_at=generated_at)


async def project_federation_payload(
    connection: asyncpg.Connection | asyncpg.Pool, *, generated_at: datetime
) -> FederationPayload:
    sections = {
        row["section_key"]: row["public_content"]
        for row in await connection.fetch(
            """
            SELECT section_key, public_content
            FROM federation_sections
            WHERE item_key='main' AND deleted_at IS NULL
              AND section_key IN ('mission','history','achievements','roadmap')
            ORDER BY section_key
            """
        )
    }
    leadership = []
    for row in await connection.fetch(
        """
        SELECT lp.*,
               (lp.source_key LIKE 'bundle:leader:%' OR EXISTS (
                   SELECT 1 FROM consents consent
                   WHERE consent.subject_type='leadership' AND consent.subject_id=lp.id
                     AND consent.scope='contact' AND consent.status='granted'
                     AND (consent.valid_from IS NULL OR consent.valid_from <= $1)
                     AND (consent.valid_until IS NULL OR consent.valid_until >= $1)
               )) AS contact_allowed,
               (lp.source_key LIKE 'bundle:leader:%' OR EXISTS (
                   SELECT 1 FROM consents consent
                   WHERE consent.subject_type='leadership' AND consent.subject_id=lp.id
                     AND consent.scope='portrait' AND consent.status='granted'
                     AND (consent.valid_from IS NULL OR consent.valid_from <= $1)
                     AND (consent.valid_until IS NULL OR consent.valid_until >= $1)
               )) AS portrait_allowed
        FROM leadership_profiles lp
        WHERE lp.active=TRUE AND lp.deleted_at IS NULL
        ORDER BY sort_order, source_key NULLS LAST, id
        """,
        generated_at,
    ):
        leadership.append(
            {
                "id": _bundle_public_id(row["source_key"], "leader", row["id"]),
                "nameRu": row["name_ru"],
                "nameKz": row["name_kz"],
                "roleRu": row["role_ru"],
                "roleKz": row["role_kz"],
                "bioRu": row["bio_ru"],
                "bioKz": row["bio_kz"],
                "focusRu": row["focus_ru"],
                "focusKz": row["focus_kz"],
                "photo": row["photo_url"] if row["portrait_allowed"] else "",
                "email": (
                    row["email_private"]
                    if row["contacts_are_public"] and row["contact_allowed"]
                    else ""
                ),
                "phone": (
                    row["phone_private"]
                    if row["contacts_are_public"] and row["contact_allowed"]
                    else ""
                ),
            }
        )
    return build_federation_payload(
        {
            "mission": sections.get("mission", {}),
            "history": sections.get("history", []),
            "achievements": sections.get("achievements", []),
            "roadmap": sections.get("roadmap", []),
            "leadership": leadership,
        },
        generated_at=generated_at,
    )


async def project_news_payload(
    connection: asyncpg.Connection | asyncpg.Pool, *, generated_at: datetime
) -> NewsPayload:
    rows = await connection.fetch(
        """
        SELECT n.*, c.slug AS city_slug
        FROM news_items n
        LEFT JOIN cities c ON c.id=n.city_id
        WHERE n.status='approved' AND n.deleted_at IS NULL
          AND n.published_at <= $1
          AND (n.scope='national' OR (c.active=TRUE AND c.deleted_at IS NULL))
        ORDER BY n.published_at DESC, n.slug
        """,
        generated_at,
    )
    gallery_rows = await connection.fetch(
        """
        SELECT n.id AS news_id, n.slug, nmi.media_id, nmi.alt_ru, nmi.alt_kz,
               nmi.alt_en, nmi.sort_order, ma.sha256, ma.width, ma.height
        FROM news_items n
        JOIN news_media_items nmi ON nmi.news_id=n.id
        JOIN media_assets ma ON ma.id=nmi.media_id
        JOIN LATERAL (
            SELECT c.status
            FROM consents c
            WHERE c.subject_type='media' AND c.subject_id=ma.id
              AND c.scope='media_publication'
            ORDER BY c.updated_at DESC, c.created_at DESC, c.id DESC
            LIMIT 1
        ) latest_consent ON latest_consent.status='granted'
        WHERE n.status='approved' AND n.deleted_at IS NULL
          AND nmi.selected_for_publication=TRUE
          AND ma.deleted_at IS NULL AND ma.derivative_path IS NOT NULL
          AND ma.moderation_status='approved'
        ORDER BY n.id, nmi.sort_order, nmi.media_id
        """
    )
    galleries: dict[Any, list[dict[str, Any]]] = {}
    for gallery in gallery_rows:
        galleries.setdefault(gallery["news_id"], []).append(
            {
                "src": f"/assets/news/{gallery['slug']}/{gallery['sha256']}.webp",
                "width": gallery["width"],
                "height": gallery["height"],
                "altRu": gallery["alt_ru"],
                "altKz": gallery["alt_kz"],
                "altEn": gallery["alt_en"],
            }
        )
    return build_news_payload(
        [
            {
                "slug": row["slug"],
                "scope": row["scope"],
                "citySlug": row["city_slug"] or "",
                "publishedAt": _iso_milliseconds(row["published_at"]),
                "titleRu": row["title_ru"],
                "titleKz": row["title_kz"],
                "titleEn": row["title_en"],
                "excerptRu": row["excerpt_ru"],
                "excerptKz": row["excerpt_kz"],
                "excerptEn": row["excerpt_en"],
                "bodyRu": list(row["body_ru"]),
                "bodyKz": list(row["body_kz"]),
                "bodyEn": list(row["body_en"]),
                "sources": list(row["sources"]),
                "gallery": galleries.get(row["id"], []),
                "image": row["image_url"] if row["media_rights_confirmed"] else "",
                "imageAltRu": row["image_alt_ru"] if row["media_rights_confirmed"] else "",
                "imageAltKz": row["image_alt_kz"] if row["media_rights_confirmed"] else "",
                "imageAltEn": row["image_alt_en"] if row["media_rights_confirmed"] else "",
                "videoUrl": row["video_url"],
            }
            for row in rows
        ],
        generated_at=generated_at,
    )


def deterministic_json(
    model: CityPayload | FederationPayload | NewsPayload | dict[str, Any]
) -> str:
    value = (
        model.model_dump(mode="json", exclude_none=True) if hasattr(model, "model_dump") else model
    )
    assert_private_fields_absent(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=4) + "\n"


def write_payload_atomic(destination: Path, content: str) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(destination)
    import hashlib

    return hashlib.sha256(content.encode()).hexdigest()
