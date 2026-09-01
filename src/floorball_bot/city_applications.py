from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import UUID

import asyncpg
from pydantic import BaseModel, ConfigDict, Field

from floorball_bot.auth import require_roles
from floorball_bot.dialogue.evaluator import evaluate_gaps
from floorball_bot.dialogue.models import FieldSpec, FieldType, LocalizedQuestion
from floorball_bot.domain import Actor, Role, normalize_phone, verify_self_contact
from floorball_bot.errors import AuthorizationError


class CityApplicationRejected(ValueError):
    pass


class CityProposalSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    mode: Literal["city_proposal"]
    version: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    ui_label: LocalizedQuestion
    fields: tuple[FieldSpec, ...] = Field(min_length=1)
    completion_rules: tuple = ()


@dataclass(frozen=True)
class LoadedCityProposalSpec:
    spec: CityProposalSpec
    sha256: str


class CityInitializationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    application_id: UUID
    slug: str
    city_id: UUID | None = None
    user_id: UUID | None = None
    duplicate_reasons: tuple[str, ...] = ()
    applied: bool = False
    already_applied: bool = False


_TRANSLITERATION = str.maketrans(
    {
        "а": "a", "ә": "a", "б": "b", "в": "v", "г": "g", "ғ": "g",
        "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
        "й": "i", "к": "k", "қ": "q", "л": "l", "м": "m", "н": "n",
        "ң": "n", "о": "o", "ө": "o", "п": "p", "р": "r", "с": "s",
        "т": "t", "у": "u", "ұ": "u", "ү": "u", "ф": "f", "х": "h",
        "һ": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
        "ъ": "", "ы": "y", "і": "i", "ь": "", "э": "e", "ю": "yu",
        "я": "ya",
    }
)


def load_city_proposal_spec() -> LoadedCityProposalSpec:
    path = Path(__file__).resolve().parent / "dialogue" / "specs" / "city_proposal.v1.json"
    spec = CityProposalSpec.model_validate_json(path.read_text(encoding="utf-8"))
    canonical = json.dumps(
        spec.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return LoadedCityProposalSpec(
        spec=spec, sha256=hashlib.sha256(canonical.encode()).hexdigest()
    )


def canonical_city_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.strip().casefold()).translate(
        _TRANSLITERATION
    )
    ascii_value = normalized.encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")[:80].rstrip("-")
    if not slug:
        raise CityApplicationRejected("city name cannot be converted to a safe slug")
    return slug


def parse_city_proposal_answer(field: FieldSpec, raw: str) -> Any:
    value = raw.strip()
    if field.max_length and len(value) > field.max_length:
        raise CityApplicationRejected(f"answer exceeds {field.max_length} characters")
    if field.type == FieldType.BOOLEAN:
        normalized = value.casefold()
        if normalized in {"да", "иә", "yes", "подтверждаю", "растаймын"}:
            return True
        if normalized in {"нет", "жоқ", "no"}:
            return False
        raise CityApplicationRejected("answer yes/да/иә or no/нет/жоқ")
    if field.type == FieldType.URL:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise CityApplicationRejected("source URL must use HTTPS")
    if field.id == "coordinates":
        try:
            longitude, latitude = (float(part.strip()) for part in value.split(",", 1))
        except (TypeError, ValueError) as exc:
            raise CityApplicationRejected("coordinates must be longitude, latitude") from exc
        if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
            raise CityApplicationRejected("coordinates are outside valid bounds")
        return [longitude, latitude]
    return value


async def record_start_intent(
    connection: asyncpg.Connection, *, telegram_id: int, update_id: int
) -> None:
    await connection.execute(
        """
        INSERT INTO telegram_start_intents(telegram_id, update_id, start_parameter)
        VALUES ($1,$2,'new_city') ON CONFLICT (update_id) DO NOTHING
        """,
        telegram_id,
        update_id,
    )


async def bind_city_applicant(
    connection: asyncpg.Connection,
    *,
    sender_id: int,
    contact_user_id: int | None,
    raw_phone: str,
    display_name: str,
) -> UUID:
    intent = await connection.fetchrow(
        """
        SELECT id FROM telegram_start_intents
        WHERE telegram_id=$1 AND status='pending' AND expires_at>now()
        ORDER BY created_at DESC LIMIT 1 FOR UPDATE
        """,
        sender_id,
    )
    if not intent:
        raise AuthorizationError("new-city start intent is missing or expired")
    recent_attempts = await connection.fetchval(
        """
        SELECT count(*) FROM city_applicant_contact_attempts
        WHERE telegram_id=$1 AND attempted_at>now()-interval '1 hour'
        """,
        sender_id,
    )
    if recent_attempts >= 5:
        raise AuthorizationError("too many contact verification attempts")
    succeeded = contact_user_id == sender_id
    await connection.execute(
        """
        INSERT INTO city_applicant_contact_attempts(telegram_id, contact_user_id, succeeded)
        VALUES ($1,$2,$3)
        """,
        sender_id,
        contact_user_id,
        succeeded,
    )
    verify_self_contact(sender_id=sender_id, contact_user_id=contact_user_id)
    phone = normalize_phone(raw_phone)
    if await connection.fetchval(
        "SELECT EXISTS(SELECT 1 FROM users WHERE phone_e164=$1 OR telegram_id=$2)",
        phone,
        sender_id,
    ):
        raise AuthorizationError("existing editor accounts cannot use the public applicant flow")
    phone_owner = await connection.fetchval(
        "SELECT telegram_id FROM city_applicants WHERE phone_e164=$1", phone
    )
    if phone_owner is not None and phone_owner != sender_id:
        raise AuthorizationError("phone is already bound to another applicant")
    applicant_id = await connection.fetchval(
        """
        INSERT INTO city_applicants(
            telegram_id, phone_e164, display_name, contact_verified_at
        ) VALUES ($1,$2,$3,now())
        ON CONFLICT (telegram_id) DO UPDATE SET
            phone_e164=EXCLUDED.phone_e164,
            display_name=EXCLUDED.display_name,
            contact_verified_at=now(), updated_at=now()
        RETURNING id
        """,
        sender_id,
        phone,
        display_name[:180],
    )
    await connection.execute(
        "UPDATE telegram_start_intents SET status='contact_verified', updated_at=now() WHERE id=$1",
        intent["id"],
    )
    return applicant_id


async def get_or_create_city_application(
    connection: asyncpg.Connection, *, applicant_id: UUID
) -> asyncpg.Record:
    existing = await connection.fetchrow(
        """
        SELECT * FROM city_applications
        WHERE applicant_id=$1 AND status IN (
            'collecting','submitted','under_review','changes_requested','verified'
        ) ORDER BY created_at DESC LIMIT 1 FOR UPDATE
        """,
        applicant_id,
    )
    if existing:
        return existing
    loaded = load_city_proposal_spec()
    first = evaluate_gaps(loaded.spec, {}).next_field_id or "ready_to_submit"
    row = await connection.fetchrow(
        """
        INSERT INTO city_applications(
            applicant_id, spec_version, spec_hash, current_step
        ) VALUES ($1,$2,$3,$4) RETURNING *
        """,
        applicant_id,
        loaded.spec.version,
        loaded.sha256,
        first,
    )
    await connection.execute(
        """
        INSERT INTO city_application_events(application_id, applicant_id, action)
        VALUES ($1,$2,'created')
        """,
        row["id"],
        applicant_id,
    )
    return row


async def application_duplicate_reasons(
    connection: asyncpg.Connection,
    *,
    application_id: UUID,
    name_ru: str,
    name_kz: str,
    slug: str,
) -> tuple[str, ...]:
    reasons: list[str] = []
    city = await connection.fetchrow(
        """
        SELECT slug, name_ru, name_kz FROM cities
        WHERE slug=$1 OR lower(name_ru)=lower($2) OR lower(name_kz)=lower($3)
        LIMIT 1
        """,
        slug,
        name_ru,
        name_kz,
    )
    if city:
        reasons.append(f"canonical city already exists: {city['slug']}")
    duplicate = await connection.fetchval(
        """
        SELECT id FROM city_applications
        WHERE id<>$1 AND status NOT IN ('rejected','cancelled')
          AND (
            slug_candidate=$2 OR lower(fields->>'city_name_ru')=lower($3)
            OR lower(fields->>'city_name_kz')=lower($4)
          )
        LIMIT 1
        """,
        application_id,
        slug,
        name_ru,
        name_kz,
    )
    if duplicate:
        reasons.append(f"another application matches: {duplicate}")
    return tuple(reasons)


async def verify_city_application(
    connection: asyncpg.Connection, *, application_id: UUID, actor: Actor
) -> str:
    require_roles(actor, Role.SUPERADMIN)
    row = await connection.fetchrow(
        "SELECT * FROM city_applications WHERE id=$1 FOR UPDATE", application_id
    )
    if not row or row["status"] not in {
        "submitted",
        "under_review",
        "changes_requested",
        "verified",
    }:
        raise CityApplicationRejected("application is not reviewable")
    if row["status"] == "verified":
        return row["slug_candidate"]
    loaded = load_city_proposal_spec()
    if row["spec_version"] != loaded.spec.version or row["spec_hash"] != loaded.sha256:
        raise CityApplicationRejected("application questionnaire version changed")
    fields = dict(row["fields"])
    gaps = evaluate_gaps(loaded.spec, fields)
    if not gaps.can_publish:
        missing = ", ".join(gap.field_id for gap in gaps.required_for_publish)
        raise CityApplicationRejected(f"publish-ready fields are missing: {missing}")
    slug = canonical_city_slug(fields["city_name_ru"])
    reasons = await application_duplicate_reasons(
        connection,
        application_id=application_id,
        name_ru=fields["city_name_ru"],
        name_kz=fields["city_name_kz"],
        slug=slug,
    )
    if reasons:
        raise CityApplicationRejected("; ".join(reasons))
    await connection.execute(
        """
        UPDATE city_applications SET status='verified', slug_candidate=$2,
            verified_by=$3, verified_at=now(), updated_at=now()
        WHERE id=$1
        """,
        application_id,
        slug,
        actor.user_id,
    )
    await connection.execute(
        """
        INSERT INTO city_application_events(application_id, actor_id, action)
        VALUES ($1,$2,'verified')
        """,
        application_id,
        actor.user_id,
    )
    return slug


async def initialize_city_application(
    pool: asyncpg.Pool, *, application_id: UUID, actor: Actor, apply: bool
) -> CityInitializationResult:
    require_roles(actor, Role.SUPERADMIN)
    async with pool.acquire() as connection, connection.transaction():
        row = await connection.fetchrow(
            """
            SELECT a.*, p.telegram_id, p.phone_e164, p.display_name,
                   p.initialized_user_id
            FROM city_applications a
            JOIN city_applicants p ON p.id=a.applicant_id
            WHERE a.id=$1 FOR UPDATE OF a, p
            """,
            application_id,
        )
        if not row:
            raise CityApplicationRejected("application not found")
        if row["status"] == "initialized":
            return CityInitializationResult(
                application_id=application_id,
                slug=row["slug_candidate"],
                city_id=row["initialized_city_id"],
                user_id=row["initialized_user_id"],
                already_applied=True,
            )
        fields = dict(row["fields"])
        slug = row["slug_candidate"] or canonical_city_slug(fields.get("city_name_ru", ""))
        reasons = await application_duplicate_reasons(
            connection,
            application_id=application_id,
            name_ru=fields.get("city_name_ru", ""),
            name_kz=fields.get("city_name_kz", ""),
            slug=slug,
        )
        preview = CityInitializationResult(
            application_id=application_id, slug=slug, duplicate_reasons=reasons
        )
        if not apply:
            return preview
        if row["status"] != "verified" or row["verified_by"] is None:
            raise CityApplicationRejected("explicit superadmin verification is required")
        if reasons:
            raise CityApplicationRejected("; ".join(reasons))
        await connection.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"city-slug:{slug}")
        reasons = await application_duplicate_reasons(
            connection,
            application_id=application_id,
            name_ru=fields["city_name_ru"],
            name_kz=fields["city_name_kz"],
            slug=slug,
        )
        if reasons:
            raise CityApplicationRejected("; ".join(reasons))
        await connection.execute(
            """
            INSERT INTO city_slug_reservations(slug, application_id, reserved_by)
            VALUES ($1,$2,$3)
            """,
            slug,
            application_id,
            actor.user_id,
        )
        existing_user = await connection.fetchval(
            "SELECT id FROM users WHERE phone_e164=$1 OR telegram_id=$2 LIMIT 1",
            row["phone_e164"],
            row["telegram_id"],
        )
        if existing_user:
            raise CityApplicationRejected("applicant identity already belongs to a user")
        user_id = await connection.fetchval(
            """
            INSERT INTO users(
                phone_e164, display_name, telegram_id, telegram_bound_at, active
            ) VALUES ($1,$2,$3,now(),TRUE) RETURNING id
            """,
            row["phone_e164"],
            fields.get("applicant_name") or row["display_name"] or "City applicant",
            row["telegram_id"],
        )
        coordinates = fields.get("coordinates") or [None, None]
        aliases = [
            item.strip()[:180]
            for item in str(fields.get("region_aliases", "")).split(",")
            if item.strip()
        ][:30]
        city_id = await connection.fetchval(
            """
            INSERT INTO cities(
                slug, name_ru, name_kz, name_en, region, longitude, latitude,
                region_aliases, active, created_by, updated_by
            ) VALUES ($1,$2,$3,'',$4,$5,$6,$7,FALSE,$8,$8) RETURNING id
            """,
            slug,
            fields["city_name_ru"],
            fields["city_name_kz"],
            fields["region"],
            coordinates[0],
            coordinates[1],
            aliases,
            actor.user_id,
        )
        sources = [{"label": "Источник заявки", "url": fields["source_url"]}]
        await connection.execute(
            """
            INSERT INTO city_content(
                city_id, description_ru, description_kz, history_ru, history_kz,
                sources, created_by, updated_by
            ) VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$7)
            """,
            city_id,
            fields["summary_ru"],
            fields["summary_kz"],
            fields["history_ru"],
            fields["history_kz"],
            sources,
            actor.user_id,
        )
        await connection.execute(
            "INSERT INTO user_roles(user_id, role_name, granted_by) VALUES ($1,'city_coach',$2)",
            user_id,
            actor.user_id,
        )
        await connection.execute(
            "INSERT INTO user_city_scopes(user_id, city_id, granted_by) VALUES ($1,$2,$3)",
            user_id,
            city_id,
            actor.user_id,
        )
        await connection.execute(
            "UPDATE city_applicants SET initialized_user_id=$2, updated_at=now() WHERE id=$1",
            row["applicant_id"],
            user_id,
        )
        await connection.execute(
            """
            UPDATE city_applications SET status='initialized', initialized_city_id=$2,
                updated_at=now() WHERE id=$1
            """,
            application_id,
            city_id,
        )
        await connection.execute(
            """
            INSERT INTO city_application_events(application_id, actor_id, action)
            VALUES ($1,$2,'initialized')
            """,
            application_id,
            actor.user_id,
        )
        return CityInitializationResult(
            application_id=application_id,
            slug=slug,
            city_id=city_id,
            user_id=user_id,
            applied=True,
        )
