from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import asyncpg
from pydantic import BaseModel, ConfigDict, Field

from floorball_bot.dialogue.models import DialogueMode as AgentMode
from floorball_bot.domain import Actor, Role
from floorball_bot.errors import AuthorizationError

CONTEXT_SCHEMA_VERSION = "agent-context.v1"
SITE_CONTRACT_VERSION = 1


class StrictContextModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LocalizedValue(StrictContextModel):
    ru: str = ""
    kz: str = ""
    en: str = ""


class CityIdentity(StrictContextModel):
    slug: str = Field(max_length=80, pattern=r"^[a-z0-9-]+$")
    name: LocalizedValue
    locative: LocalizedValue
    region: str = Field(default="", max_length=180)
    longitude: float | None = None
    latitude: float | None = None
    revision: int = Field(ge=1)
    updated_at: datetime


class CityNarrative(StrictContextModel):
    description: LocalizedValue
    history: LocalizedValue
    source_count: int = Field(ge=0)
    revision: int = Field(ge=0)


class CityRecordCounts(StrictContextModel):
    registered_clubs: int = Field(ge=0)
    registered_coaches: int = Field(ge=0)
    registered_players: int = Field(ge=0)
    active_schedule_slots: int = Field(ge=0)
    selected_player_profiles: int = Field(ge=0)
    approved_player_profiles: int = Field(ge=0)
    gallery_items: int = Field(ge=0)
    approved_gallery_items: int = Field(ge=0)


class CitySiteMetrics(StrictContextModel):
    players: int | None = Field(default=None, ge=0, le=1_000_000)
    coaches: int | None = Field(default=None, ge=0, le=1_000_000)
    clubs: int | None = Field(default=None, ge=0, le=1_000_000)
    data_status: Literal["approved-city-registry", "verified-coach-data"]
    public_updated_at: datetime | None = None


class ClubContext(StrictContextModel):
    name: str = Field(max_length=180)
    age_groups: list[str] = Field(default_factory=list, max_length=10)
    notes: str = Field(default="", max_length=600)
    public_contact_ready: bool
    revision: int = Field(ge=1)


class ScheduleContext(StrictContextModel):
    day: Literal[
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    ]
    time: str = Field(max_length=80)
    venue: str = Field(max_length=240)
    address: str = Field(default="", max_length=300)
    group: str = Field(default="", max_length=180)
    revision: int = Field(ge=1)


class ContextCoverage(StrictContextModel):
    available_fields: list[str]
    empty_fields: list[str]


class CityDirectoryEntry(StrictContextModel):
    slug: str = Field(max_length=80, pattern=r"^[a-z0-9-]+$")
    name: LocalizedValue


class CityDirectorySnapshot(StrictContextModel):
    schema_version: Literal["agent-context.v1"] = CONTEXT_SCHEMA_VERSION
    site_contract_version: Literal[1] = SITE_CONTRACT_VERSION
    mode: Literal[AgentMode.TRAINER] = AgentMode.TRAINER
    access: Literal["public_directory"] = "public_directory"
    cities: list[CityDirectoryEntry] = Field(max_length=200)


class TrainerContextSnapshot(StrictContextModel):
    schema_version: Literal["agent-context.v1"] = CONTEXT_SCHEMA_VERSION
    site_contract_version: Literal[1] = SITE_CONTRACT_VERSION
    mode: Literal[AgentMode.TRAINER] = AgentMode.TRAINER
    access: Literal["full", "coverage_only"]
    city: CityIdentity
    narrative: CityNarrative | None
    site_metrics: CitySiteMetrics
    records: CityRecordCounts
    clubs: list[ClubContext] = Field(max_length=10)
    schedules: list[ScheduleContext] = Field(max_length=20)
    coverage: ContextCoverage


class FederationSectionContext(StrictContextModel):
    section_key: str = Field(max_length=80)
    item_key: str = Field(max_length=120)
    content: dict[str, Any]
    public_content: dict[str, Any]
    source_count: int = Field(ge=0)
    revision: int = Field(ge=1)
    updated_at: datetime


class FederationContextSnapshot(StrictContextModel):
    schema_version: Literal["agent-context.v1"] = CONTEXT_SCHEMA_VERSION
    site_contract_version: Literal[1] = SITE_CONTRACT_VERSION
    mode: Literal[AgentMode.STRATEGY, AgentMode.HISTORY]
    access: Literal["full"] = "full"
    sections: list[FederationSectionContext]
    coverage: ContextCoverage


class LeadershipProfileContext(StrictContextModel):
    name: LocalizedValue
    role: LocalizedValue
    bio: LocalizedValue
    focus: LocalizedValue
    public_contact_ready: bool
    portrait_attached: bool
    revision: int = Field(ge=1)
    updated_at: datetime


class LeadershipContextSnapshot(StrictContextModel):
    schema_version: Literal["agent-context.v1"] = CONTEXT_SCHEMA_VERSION
    site_contract_version: Literal[1] = SITE_CONTRACT_VERSION
    mode: Literal[AgentMode.LEADERSHIP] = AgentMode.LEADERSHIP
    access: Literal["full"] = "full"
    profiles: list[LeadershipProfileContext] = Field(max_length=12)
    coverage: ContextCoverage


AgentContextSnapshot = (
    CityDirectorySnapshot
    | TrainerContextSnapshot
    | FederationContextSnapshot
    | LeadershipContextSnapshot
)


class SiteFieldMapping(StrictContextModel):
    db_field: str = Field(max_length=160)
    site_field: str = Field(max_length=160)
    rule: str = Field(max_length=500)


class SiteContractMapping(StrictContextModel):
    mode: AgentMode
    db_sources: list[str]
    site_paths: list[str]
    fields: list[SiteFieldMapping]
    notes: list[str] = Field(default_factory=list)


class AgentContextCatalog(StrictContextModel):
    schema_version: Literal["agent-context.v1"] = CONTEXT_SCHEMA_VERSION
    site_contract_version: Literal[1] = SITE_CONTRACT_VERSION
    mappings: list[SiteContractMapping]


_MODE_ROLES: dict[AgentMode, frozenset[Role]] = {
    AgentMode.TRAINER: frozenset(
        {Role.COACH_FORM, Role.CITY_COACH, Role.REVIEWER, Role.SUPERADMIN}
    ),
    AgentMode.STRATEGY: frozenset(
        {Role.FEDERATION_EDITOR, Role.REVIEWER, Role.SUPERADMIN}
    ),
    AgentMode.HISTORY: frozenset(
        {Role.FEDERATION_EDITOR, Role.REVIEWER, Role.SUPERADMIN}
    ),
    AgentMode.LEADERSHIP: frozenset(
        {Role.FEDERATION_EDITOR, Role.REVIEWER, Role.SUPERADMIN}
    ),
}

_FEDERATION_SECTION_KEYS: dict[AgentMode, tuple[str, ...]] = {
    AgentMode.STRATEGY: ("mission", "vision", "values", "goals"),
    AgentMode.HISTORY: ("history", "achievements", "roadmap"),
}

# These are output-boundary names. Database queries may use private identifiers internally,
# but no matching key is permitted to cross into a model prompt or CLI JSON response.
_PRIVATE_EXACT_KEYS = {
    "id",
    "user_id",
    "telegram_id",
    "city_id",
    "club_id",
    "media_id",
    "entity_id",
    "subject_id",
    "created_by",
    "updated_by",
    "granted_by",
    "reviewed_by",
    "phone_e164",
    "phone_private",
    "email_private",
    "contact_phone_private",
    "evidence_private",
    "original_path",
    "derivative_path",
}
_PRIVATE_KEY_WORDS = frozenset({"id", "phone", "email", "telegram", "whatsapp", "contact"})
_SAFE_CONTEXT_KEYS = frozenset({"public_contact_ready"})
_UUID_PATTERN = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b"
)
_EMAIL_PATTERN = re.compile(r"(?<![\w.-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_PHONE_CANDIDATE_PATTERN = re.compile(r"(?<!\w)(?:\+?\d[\d ()-]{8,}\d)(?!\w)")


def _key_words(key: str) -> set[str]:
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()
    return {part for part in re.split(r"[^a-z0-9]+", snake) if part}


def _is_private_key(key: str) -> bool:
    lowered = key.casefold()
    if lowered in _SAFE_CONTEXT_KEYS:
        return False
    return lowered in _PRIVATE_EXACT_KEYS or bool(_key_words(key) & _PRIVATE_KEY_WORDS)


def _redact_agent_string(value: str) -> str:
    value = _UUID_PATTERN.sub("[redacted-id]", value)
    value = _EMAIL_PATTERN.sub("[redacted-contact]", value)

    def redact_phone(match: re.Match[str]) -> str:
        candidate = match.group(0)
        digits = re.sub(r"\D", "", candidate)
        return "[redacted-contact]" if len(digits) >= 10 else candidate

    return _PHONE_CANDIDATE_PATTERN.sub(redact_phone, value)[:2400]


def redact_agent_value(value: Any, *, _depth: int = 0) -> Any:
    """Return bounded JSON data with identifiers and contact-shaped keys removed."""
    if _depth > 8:
        return "[depth-limit]"
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, nested in list(value.items())[:100]:
            if _is_private_key(str(key)):
                continue
            safe_nested = redact_agent_value(nested, _depth=_depth + 1)
            if safe_nested in ({}, []):
                continue
            redacted[str(key)] = safe_nested
        return redacted
    if isinstance(value, list):
        return [redact_agent_value(item, _depth=_depth + 1) for item in value[:50]]
    if isinstance(value, str):
        return _redact_agent_string(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:2400]


def assert_agent_context_safe(value: Any, path: str = "$") -> None:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        for key, nested in value.items():
            if _is_private_key(str(key)):
                raise ValueError(f"private context key at {path}.{key}")
            assert_agent_context_safe(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            assert_agent_context_safe(nested, f"{path}[{index}]")
    elif isinstance(value, str) and _redact_agent_string(value) != value:
        raise ValueError(f"private context value at {path}")


def context_schema_catalog() -> AgentContextCatalog:
    catalog = AgentContextCatalog(
        mappings=[
            SiteContractMapping(
                mode=AgentMode.TRAINER,
                db_sources=[
                    "cities",
                    "city_content",
                    "clubs",
                    "coaches",
                    "training_schedules",
                    "players + player_city_memberships",
                    "city_gallery_items",
                ],
                site_paths=[
                    "cities[].slug/nameRu/nameKz/nameEn",
                    "cities[].locativeRu/locativeKz/locativeEn/region/geoCoords",
                    "cities[].descRu/descKz/descEn/historyRu/historyKz/historyEn",
                    "cities[].players/coaches/clubs",
                    "cities[].clubs_list[]/schedule[]/players_list[]/gallery[]",
                ],
                fields=[
                    SiteFieldMapping(
                        db_field="cities.slug/name_*/locative_*/region/longitude/latitude",
                        site_field="cities[].slug/name*/locative*/region/geoCoords",
                        rule="Only active, non-deleted cities; coordinates remain nullable.",
                    ),
                    SiteFieldMapping(
                        db_field="cities.players_estimate/coaches_estimate/clubs_estimate",
                        site_field="cities[].players/coaches/clubs",
                        rule="Nullable verified estimates; zero is distinct from unknown.",
                    ),
                    SiteFieldMapping(
                        db_field="city_content.description_*/history_*",
                        site_field="cities[].desc*/history*",
                        rule="Reviewer-approved localized values within contract limits.",
                    ),
                    SiteFieldMapping(
                        db_field="clubs",
                        site_field="cities[].clubs_list[]",
                        rule="Active rows; contact values require explicit public-contact consent.",
                    ),
                    SiteFieldMapping(
                        db_field="training_schedules",
                        site_field="cities[].schedule[]",
                        rule="Active rows with allowlisted weekday, time and venue.",
                    ),
                    SiteFieldMapping(
                        db_field="players + player_city_memberships",
                        site_field="cities[].players_list[]",
                        rule="Current membership plus selected and approved publication flags.",
                    ),
                    SiteFieldMapping(
                        db_field="city_gallery_items",
                        site_field="cities[].gallery[]",
                        rule="Selected, approved, non-deleted rows only; maximum 30.",
                    ),
                ],
                notes=[
                    "Public estimates and normalized record counts are reported separately.",
                    "Contacts and identifiers are never exposed by the context gateway.",
                ],
            ),
            SiteContractMapping(
                mode=AgentMode.STRATEGY,
                db_sources=["federation_sections: mission, vision, values, goals"],
                site_paths=["federation.mission"],
                fields=[
                    SiteFieldMapping(
                        db_field="federation_sections.public_content",
                        site_field="federation.mission",
                        rule=(
                            "Approved strategy projection containing mission, vision, "
                            "values and goals."
                        ),
                    )
                ],
            ),
            SiteContractMapping(
                mode=AgentMode.HISTORY,
                db_sources=["federation_sections: history, achievements, roadmap"],
                site_paths=[
                    "federation.history[]",
                    "federation.achievements[]",
                    "federation.roadmap[]",
                ],
                fields=[
                    SiteFieldMapping(
                        db_field="federation_sections.public_content",
                        site_field="federation.history[]/achievements[]/roadmap[]",
                        rule=(
                            "Approved arrays selected by section_key; "
                            "source URLs remain allowlisted."
                        ),
                    )
                ],
            ),
            SiteContractMapping(
                mode=AgentMode.LEADERSHIP,
                db_sources=["leadership_profiles"],
                site_paths=["federation.leadership[]"],
                fields=[
                    SiteFieldMapping(
                        db_field="leadership_profiles name_*/role_*/bio_*/focus_*",
                        site_field="federation.leadership[]",
                        rule=(
                            "Active profiles; contacts and portrait require "
                            "their publication gates."
                        ),
                    )
                ],
                notes=[
                    "The snapshot reports contact readiness but never returns contact values.",
                    "Portrait presence is reported without exposing an internal media identifier.",
                ],
            ),
        ]
    )
    assert_agent_context_safe(catalog)
    return catalog


async def load_context_actor(connection: asyncpg.Connection, user_id: UUID) -> Actor | None:
    row = await connection.fetchrow(
        """
        SELECT id, telegram_id, active
        FROM users
        WHERE id=$1 AND deleted_at IS NULL
        """,
        user_id,
    )
    if not row:
        return None
    roles = await connection.fetch(
        "SELECT role_name FROM user_roles WHERE user_id=$1 AND revoked_at IS NULL", user_id
    )
    scopes = await connection.fetch(
        "SELECT city_id FROM user_city_scopes WHERE user_id=$1 AND revoked_at IS NULL", user_id
    )
    return Actor(
        user_id=row["id"],
        telegram_id=row["telegram_id"] or 0,
        active=row["active"],
        roles=frozenset(Role(item["role_name"]) for item in roles),
        city_scopes=frozenset(item["city_id"] for item in scopes),
    )


class AgentContextGateway:
    """Read-only, role-scoped context snapshots built from a fixed query allowlist."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def snapshot(
        self,
        *,
        actor: Actor,
        mode: AgentMode,
        city_slug: str | None = None,
    ) -> AgentContextSnapshot:
        self._authorize_mode(actor, mode)
        if mode == AgentMode.TRAINER:
            if not city_slug:
                snapshot = await self.city_directory(actor)
            else:
                snapshot = await self._trainer_snapshot(actor, city_slug)
        elif mode in {AgentMode.STRATEGY, AgentMode.HISTORY}:
            snapshot = await self._federation_snapshot(mode)
        else:
            snapshot = await self._leadership_snapshot()
        assert_agent_context_safe(snapshot)
        return snapshot

    async def city_directory(self, actor: Actor) -> CityDirectorySnapshot:
        """Return public city labels, filtered to assigned scopes when applicable."""
        self._authorize_mode(actor, AgentMode.TRAINER)
        global_directory = actor.has_any_role(
            Role.SUPERADMIN, Role.REVIEWER, Role.COACH_FORM
        )
        async with self.pool.acquire() as connection, connection.transaction(
            isolation="repeatable_read", readonly=True
        ):
            if global_directory:
                rows = await connection.fetch(
                    """
                    SELECT slug, name_ru, name_kz, name_en
                    FROM cities
                    WHERE active=TRUE AND deleted_at IS NULL
                    ORDER BY lower(name_ru), slug
                    LIMIT 200
                    """
                )
            else:
                rows = await connection.fetch(
                    """
                    SELECT slug, name_ru, name_kz, name_en
                    FROM cities
                    WHERE active=TRUE AND deleted_at IS NULL
                      AND id=ANY($1::uuid[])
                    ORDER BY lower(name_ru), slug
                    LIMIT 200
                    """,
                    list(actor.city_scopes),
                )
        snapshot = CityDirectorySnapshot(
            cities=[
                CityDirectoryEntry(
                    slug=row["slug"],
                    name=LocalizedValue(
                        ru=row["name_ru"], kz=row["name_kz"], en=row["name_en"]
                    ),
                )
                for row in rows
            ]
        )
        assert_agent_context_safe(snapshot)
        return snapshot

    @staticmethod
    def _authorize_mode(actor: Actor, mode: AgentMode) -> None:
        if not actor.active:
            raise AuthorizationError("active account is required")
        if not actor.roles.intersection(_MODE_ROLES[mode]):
            raise AuthorizationError("actor role cannot access this context mode")

    async def _trainer_snapshot(
        self, actor: Actor, city_slug: str
    ) -> TrainerContextSnapshot:
        async with self.pool.acquire() as connection, connection.transaction(
            isolation="repeatable_read", readonly=True
        ):
            city = await connection.fetchrow(
                """
                SELECT c.id AS city_id, c.slug, c.name_ru, c.name_kz, c.name_en,
                       c.locative_ru, c.locative_kz, c.locative_en, c.region,
                       c.longitude, c.latitude, c.revision, c.updated_at,
                       c.players_estimate, c.coaches_estimate, c.clubs_estimate,
                       c.data_status, c.public_updated_at,
                       cc.description_ru, cc.description_kz, cc.description_en,
                       cc.history_ru, cc.history_kz, cc.history_en,
                       CASE WHEN jsonb_typeof(cc.sources)='array'
                            THEN jsonb_array_length(cc.sources) ELSE 0 END AS source_count,
                       COALESCE(cc.revision, 0) AS content_revision,
                       (SELECT count(*) FROM clubs cl
                        WHERE cl.city_id=c.id AND cl.status='active' AND cl.deleted_at IS NULL)
                           AS registered_clubs,
                       (SELECT count(*) FROM coaches co
                        WHERE co.city_id=c.id AND co.status='active' AND co.deleted_at IS NULL)
                           AS registered_coaches,
                       (SELECT count(DISTINCT p.id)
                        FROM players p
                        JOIN player_city_memberships pcm ON pcm.player_id=p.id
                        WHERE pcm.city_id=c.id AND p.status='active' AND p.deleted_at IS NULL
                          AND pcm.valid_from <= current_date
                          AND (pcm.valid_to IS NULL OR pcm.valid_to >= current_date))
                           AS registered_players,
                       (SELECT count(*) FROM training_schedules ts
                        WHERE ts.city_id=c.id AND ts.active=TRUE AND ts.deleted_at IS NULL)
                           AS active_schedule_slots,
                       (SELECT count(DISTINCT p.id)
                        FROM players p
                        JOIN player_city_memberships pcm ON pcm.player_id=p.id
                        WHERE pcm.city_id=c.id AND p.status='active' AND p.deleted_at IS NULL
                          AND p.selected_for_publication=TRUE)
                           AS selected_player_profiles,
                       (SELECT count(DISTINCT p.id)
                        FROM players p
                        JOIN player_city_memberships pcm ON pcm.player_id=p.id
                        WHERE pcm.city_id=c.id AND p.status='active' AND p.deleted_at IS NULL
                          AND p.selected_for_publication=TRUE
                          AND p.approved_for_publication=TRUE)
                           AS approved_player_profiles,
                       (SELECT count(*) FROM city_gallery_items gi
                        WHERE gi.city_id=c.id AND gi.deleted_at IS NULL)
                           AS gallery_items,
                       (SELECT count(*) FROM city_gallery_items gi
                        WHERE gi.city_id=c.id AND gi.deleted_at IS NULL
                          AND gi.selected_for_publication=TRUE
                          AND gi.approved_for_publication=TRUE)
                           AS approved_gallery_items
                FROM cities c
                LEFT JOIN city_content cc ON cc.city_id=c.id
                WHERE c.slug=$1 AND c.active=TRUE AND c.deleted_at IS NULL
                """,
                city_slug,
            )
            if not city:
                raise ValueError("active city was not found")

            full_access = self._has_full_city_access(actor, city["city_id"])
            clubs: list[ClubContext] = []
            schedules: list[ScheduleContext] = []
            narrative: CityNarrative | None = None
            if full_access:
                club_rows = await connection.fetch(
                    """
                    SELECT name, age_groups, notes,
                           contact_is_public
                           AND (contact_name <> '' OR contact_phone_private <> '')
                               AS public_contact_ready,
                           revision
                    FROM clubs
                    WHERE city_id=$1 AND status='active' AND deleted_at IS NULL
                    ORDER BY lower(name), name
                    LIMIT 10
                    """,
                    city["city_id"],
                )
                clubs = [
                    ClubContext(
                        name=_redact_agent_string(row["name"]),
                        age_groups=[
                            _redact_agent_string(str(value)[:100])
                            for value in list(row["age_groups"])[:10]
                        ],
                        notes=_redact_agent_string(row["notes"]),
                        public_contact_ready=row["public_contact_ready"],
                        revision=row["revision"],
                    )
                    for row in club_rows
                ]
                schedule_rows = await connection.fetch(
                    """
                    SELECT day, time_text, venue, address, group_name, revision
                    FROM training_schedules
                    WHERE city_id=$1 AND active=TRUE AND deleted_at IS NULL
                    ORDER BY day, time_text, venue
                    LIMIT 20
                    """,
                    city["city_id"],
                )
                schedules = [
                    ScheduleContext(
                        day=row["day"],
                        time=_redact_agent_string(row["time_text"]),
                        venue=_redact_agent_string(row["venue"]),
                        address=_redact_agent_string(row["address"]),
                        group=_redact_agent_string(row["group_name"]),
                        revision=row["revision"],
                    )
                    for row in schedule_rows
                ]
                narrative = CityNarrative(
                    description=LocalizedValue(
                        ru=_redact_agent_string(city["description_ru"] or ""),
                        kz=_redact_agent_string(city["description_kz"] or ""),
                        en=_redact_agent_string(city["description_en"] or ""),
                    ),
                    history=LocalizedValue(
                        ru=_redact_agent_string(city["history_ru"] or ""),
                        kz=_redact_agent_string(city["history_kz"] or ""),
                        en=_redact_agent_string(city["history_en"] or ""),
                    ),
                    source_count=city["source_count"],
                    revision=city["content_revision"],
                )

            coverage_values = {
                "name_ru": city["name_ru"],
                "name_kz": city["name_kz"],
                "name_en": city["name_en"],
                "description_ru": city["description_ru"],
                "description_kz": city["description_kz"],
                "description_en": city["description_en"],
                "history_ru": city["history_ru"],
                "history_kz": city["history_kz"],
                "history_en": city["history_en"],
                "history_sources": city["source_count"] > 0,
                "clubs": city["clubs_estimate"] is not None,
                "coaches": city["coaches_estimate"] is not None,
                "players": city["players_estimate"] is not None,
                "schedule": city["active_schedule_slots"] > 0,
                "approved_player_profiles": city["approved_player_profiles"] > 0,
                "gallery": city["approved_gallery_items"] > 0,
            }
            snapshot = TrainerContextSnapshot(
                access="full" if full_access else "coverage_only",
                city=CityIdentity(
                    slug=city["slug"],
                    name=LocalizedValue(
                        ru=city["name_ru"], kz=city["name_kz"], en=city["name_en"]
                    ),
                    locative=LocalizedValue(
                        ru=city["locative_ru"],
                        kz=city["locative_kz"],
                        en=city["locative_en"],
                    ),
                    region=city["region"],
                    longitude=city["longitude"],
                    latitude=city["latitude"],
                    revision=city["revision"],
                    updated_at=city["updated_at"],
                ),
                narrative=narrative,
                site_metrics=CitySiteMetrics(
                    players=city["players_estimate"],
                    coaches=city["coaches_estimate"],
                    clubs=city["clubs_estimate"],
                    data_status=city["data_status"],
                    public_updated_at=city["public_updated_at"],
                ),
                records=CityRecordCounts(
                    registered_clubs=city["registered_clubs"],
                    registered_coaches=city["registered_coaches"],
                    registered_players=city["registered_players"],
                    active_schedule_slots=city["active_schedule_slots"],
                    selected_player_profiles=city["selected_player_profiles"],
                    approved_player_profiles=city["approved_player_profiles"],
                    gallery_items=city["gallery_items"],
                    approved_gallery_items=city["approved_gallery_items"],
                ),
                clubs=clubs,
                schedules=schedules,
                coverage=self._coverage(coverage_values),
            )
            return snapshot

    @staticmethod
    def _has_full_city_access(actor: Actor, city_id: UUID) -> bool:
        if actor.has_any_role(Role.SUPERADMIN, Role.REVIEWER):
            return True
        if Role.CITY_COACH in actor.roles and city_id in actor.city_scopes:
            return True
        if Role.COACH_FORM in actor.roles:
            return False
        raise AuthorizationError("city is outside assigned scope")

    async def _federation_snapshot(self, mode: AgentMode) -> FederationContextSnapshot:
        keys = _FEDERATION_SECTION_KEYS[mode]
        async with self.pool.acquire() as connection, connection.transaction(
            isolation="repeatable_read", readonly=True
        ):
            rows = await connection.fetch(
                """
                SELECT section_key, item_key, content_ru, content_kz, content_en,
                       public_content,
                       CASE WHEN jsonb_typeof(sources)='array'
                            THEN jsonb_array_length(sources) ELSE 0 END AS source_count,
                       revision, updated_at
                FROM federation_sections
                WHERE section_key=ANY($1::text[]) AND deleted_at IS NULL
                ORDER BY section_key, item_key
                LIMIT 100
                """,
                list(keys),
            )
        sections = [
            FederationSectionContext(
                section_key=row["section_key"],
                item_key=row["item_key"],
                content={
                    "ru": redact_agent_value(dict(row["content_ru"])),
                    "kz": redact_agent_value(dict(row["content_kz"])),
                    "en": redact_agent_value(dict(row["content_en"])),
                },
                public_content=redact_agent_value(row["public_content"]),
                source_count=row["source_count"],
                revision=row["revision"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]
        present = {
            row.section_key
            for row in sections
            if any(row.content.values()) or bool(row.public_content)
        }
        return FederationContextSnapshot(
            mode=mode,
            sections=sections,
            coverage=ContextCoverage(
                available_fields=sorted(present),
                empty_fields=sorted(set(keys) - present),
            ),
        )

    async def _leadership_snapshot(self) -> LeadershipContextSnapshot:
        async with self.pool.acquire() as connection, connection.transaction(
            isolation="repeatable_read", readonly=True
        ):
            rows = await connection.fetch(
                """
                SELECT name_ru, name_kz, role_ru, role_kz, bio_ru, bio_kz,
                       focus_ru, focus_kz,
                       contacts_are_public AND (email_private <> '' OR phone_private <> '')
                           AS public_contact_ready,
                       media_id IS NOT NULL AS portrait_attached,
                       revision, updated_at
                FROM leadership_profiles
                WHERE active=TRUE AND deleted_at IS NULL
                ORDER BY sort_order, lower(name_ru), name_ru
                LIMIT 12
                """
            )
        profiles = [
            LeadershipProfileContext(
                name=LocalizedValue(
                    ru=_redact_agent_string(row["name_ru"]),
                    kz=_redact_agent_string(row["name_kz"]),
                ),
                role=LocalizedValue(
                    ru=_redact_agent_string(row["role_ru"]),
                    kz=_redact_agent_string(row["role_kz"]),
                ),
                bio=LocalizedValue(
                    ru=_redact_agent_string(row["bio_ru"]),
                    kz=_redact_agent_string(row["bio_kz"]),
                ),
                focus=LocalizedValue(
                    ru=_redact_agent_string(row["focus_ru"]),
                    kz=_redact_agent_string(row["focus_kz"]),
                ),
                public_contact_ready=row["public_contact_ready"],
                portrait_attached=row["portrait_attached"],
                revision=row["revision"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]
        values = {
            "profiles": bool(profiles),
            "public_contacts": any(item.public_contact_ready for item in profiles),
            "portraits": any(item.portrait_attached for item in profiles),
        }
        return LeadershipContextSnapshot(profiles=profiles, coverage=self._coverage(values))

    @staticmethod
    def _coverage(values: dict[str, Any]) -> ContextCoverage:
        return ContextCoverage(
            available_fields=sorted(key for key, value in values.items() if bool(value)),
            empty_fields=sorted(key for key, value in values.items() if not bool(value)),
        )
