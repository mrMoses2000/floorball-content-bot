from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import urlparse
from uuid import UUID

import phonenumbers
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _validate_public_url(value: str, *, allow_local: bool = False) -> str:
    if not value:
        return value
    if allow_local and value.startswith("/") and not value.startswith("//"):
        return value
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("public URL must use HTTPS")
    return value


class Role(StrEnum):
    SUPERADMIN = "superadmin"
    REVIEWER = "reviewer"
    FEDERATION_EDITOR = "federation_editor"
    CITY_COACH = "city_coach"
    COACH_FORM = "coach_form"
    MEDIA_EDITOR = "media_editor"


class DraftStatus(StrEnum):
    COLLECTING = "collecting"
    READY_FOR_USER_REVIEW = "ready_for_user_review"
    SUBMITTED = "submitted"
    UNDER_REVIEW = "under_review"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    PUBLISH_FAILED = "publish_failed"
    REVOKED = "revoked"


class TranslationStatus(StrEnum):
    SOURCE = "source"
    MACHINE_DRAFT = "machine_draft"
    REVIEWED = "reviewed"
    REJECTED = "rejected"


class ConsentStatus(StrEnum):
    PENDING = "pending"
    GRANTED = "granted"
    WITHDRAWN = "withdrawn"
    REJECTED = "rejected"


ALLOWED_TRANSITIONS: dict[DraftStatus, frozenset[DraftStatus]] = {
    DraftStatus.COLLECTING: frozenset({DraftStatus.READY_FOR_USER_REVIEW, DraftStatus.CANCELLED}),
    DraftStatus.READY_FOR_USER_REVIEW: frozenset(
        {DraftStatus.COLLECTING, DraftStatus.SUBMITTED, DraftStatus.CANCELLED}
    ),
    DraftStatus.SUBMITTED: frozenset({DraftStatus.UNDER_REVIEW}),
    DraftStatus.UNDER_REVIEW: frozenset(
        {DraftStatus.CHANGES_REQUESTED, DraftStatus.APPROVED, DraftStatus.REJECTED}
    ),
    DraftStatus.CHANGES_REQUESTED: frozenset({DraftStatus.SUBMITTED, DraftStatus.CANCELLED}),
    DraftStatus.APPROVED: frozenset({DraftStatus.PUBLISHING, DraftStatus.REVOKED}),
    DraftStatus.PUBLISHING: frozenset({DraftStatus.PUBLISHED, DraftStatus.PUBLISH_FAILED}),
    DraftStatus.PUBLISH_FAILED: frozenset({DraftStatus.PUBLISHING, DraftStatus.REVOKED}),
    DraftStatus.PUBLISHED: frozenset({DraftStatus.REVOKED}),
    DraftStatus.REJECTED: frozenset(),
    DraftStatus.CANCELLED: frozenset(),
    DraftStatus.REVOKED: frozenset(),
}


def can_transition(current: DraftStatus, target: DraftStatus) -> bool:
    return current == target or target in ALLOWED_TRANSITIONS[current]


def normalize_phone(raw: str, default_region: str = "KZ") -> str:
    parsed = phonenumbers.parse(raw, default_region)
    if not phonenumbers.is_valid_number(parsed):
        raise ValueError("invalid phone number")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def verify_self_contact(*, sender_id: int, contact_user_id: int | None) -> None:
    if contact_user_id is None or sender_id != contact_user_id:
        raise ValueError("contact must belong to the Telegram sender")


class LocalizedText(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ru: str = Field(default="", max_length=2400)
    kz: str = Field(default="", max_length=2400)
    en: str = Field(default="", max_length=2400)
    source_language: Literal["ru", "kz", "en", "unknown"] = "unknown"
    translation_status_ru: TranslationStatus = TranslationStatus.SOURCE
    translation_status_kz: TranslationStatus = TranslationStatus.MACHINE_DRAFT
    translation_status_en: TranslationStatus = TranslationStatus.MACHINE_DRAFT


class ExtractedCityPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_language: Literal["ru", "kz", "en", "unknown"]
    city_slug: str | None = Field(default=None, max_length=80, pattern=r"^[a-z0-9-]+$")
    name: LocalizedText | None = None
    description: LocalizedText | None = None
    history: LocalizedText | None = None
    players_count: Annotated[int | None, Field(default=None, ge=0, le=1_000_000)]
    coaches_count: Annotated[int | None, Field(default=None, ge=0, le=100_000)]
    clubs_count: Annotated[int | None, Field(default=None, ge=0, le=100_000)]
    next_questions: list[str] = Field(default_factory=list, max_length=2)
    warnings: list[str] = Field(default_factory=list, max_length=10)


class PublicClub(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=180)
    ageGroups: list[str] = Field(default_factory=list, max_length=10)
    notes: str = Field(default="", max_length=600)
    contactName: str = Field(default="", max_length=180)
    contactPhone: str = Field(default="", max_length=80)


class PublicSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    day: Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    time: str = Field(max_length=80)
    venue: str = Field(max_length=240)
    address: str = Field(default="", max_length=300)
    group: str = Field(default="", max_length=180)


class PublicPlayer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(max_length=120)
    photo: str = Field(default="", max_length=1000)
    nameRu: str = Field(default="", max_length=120)
    nameKz: str = Field(default="", max_length=120)
    nameEn: str = Field(default="", max_length=120)
    positionRu: str = Field(default="", max_length=120)
    positionKz: str = Field(default="", max_length=120)
    positionEn: str = Field(default="", max_length=120)
    bioRu: str = Field(default="", max_length=900)
    bioKz: str = Field(default="", max_length=900)
    bioEn: str = Field(default="", max_length=900)

    @field_validator("photo")
    @classmethod
    def validate_photo(cls, value: str) -> str:
        return _validate_public_url(value, allow_local=True)

    @model_validator(mode="after")
    def require_name_and_bio(self) -> PublicPlayer:
        if not (self.nameRu or self.nameKz or self.nameEn):
            raise ValueError("player needs at least one localized name")
        if not (self.bioRu or self.bioKz or self.bioEn):
            raise ValueError("player needs at least one localized bio")
        return self


class PublicGalleryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(max_length=120)
    src: str = Field(max_length=1000)
    thumbnail: str = Field(max_length=1000)
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    altRu: str = Field(default="", max_length=300)
    altKz: str = Field(default="", max_length=300)
    altEn: str = Field(default="", max_length=300)
    captionRu: str = Field(default="", max_length=600)
    captionKz: str = Field(default="", max_length=600)
    captionEn: str = Field(default="", max_length=600)
    author: str = Field(default="", max_length=180)
    takenAt: str = Field(default="", max_length=40)

    @field_validator("src", "thumbnail")
    @classmethod
    def validate_image_url(cls, value: str) -> str:
        return _validate_public_url(value, allow_local=True)


class PublicCity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(max_length=80, pattern=r"^[a-z0-9-]+$")
    nameRu: str = Field(max_length=120)
    nameKz: str = Field(max_length=120)
    nameEn: str = Field(max_length=120)
    locativeRu: str = Field(default="", max_length=120)
    locativeKz: str = Field(default="", max_length=120)
    locativeEn: str = Field(default="", max_length=120)
    region: str = Field(default="", max_length=180)
    hero: str = Field(default="/assets/heroes/clubs.png", max_length=1000)
    geoCoords: tuple[float, float] | None = None
    players: int | None = Field(default=None, ge=0, le=1_000_000)
    coaches: int | None = Field(default=None, ge=0, le=1_000_000)
    clubs: int | None = Field(default=None, ge=0, le=1_000_000)
    clubs_list: list[PublicClub] = Field(default_factory=list, max_length=10)
    schedule: list[PublicSchedule] = Field(default_factory=list, max_length=20)
    players_list: list[PublicPlayer] = Field(default_factory=list, max_length=15)
    gallery: list[PublicGalleryItem] = Field(default_factory=list, max_length=30)
    dataStatus: Literal["approved-city-registry", "verified-coach-data"] = "verified-coach-data"
    updatedAt: str = Field(max_length=60)
    descRu: str = Field(default="", max_length=1200)
    descKz: str = Field(default="", max_length=1200)
    descEn: str = Field(default="", max_length=1200)
    historyRu: str = Field(default="", max_length=2400)
    historyKz: str = Field(default="", max_length=2400)
    historyEn: str = Field(default="", max_length=2400)

    @field_validator("hero")
    @classmethod
    def validate_hero(cls, value: str) -> str:
        return _validate_public_url(value, allow_local=True)

    @model_validator(mode="after")
    def validate_coordinates(self) -> PublicCity:
        if self.geoCoords is not None:
            longitude, latitude = self.geoCoords
            if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
                raise ValueError("geoCoords must contain valid longitude and latitude")
        return self


class CityPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    version: Literal[1] = 1
    generatedAt: str
    cities: list[PublicCity]


class FederationValue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default="", max_length=80)
    titleRu: str = Field(default="", max_length=160)
    titleKz: str = Field(default="", max_length=160)
    descriptionRu: str = Field(default="", max_length=800)
    descriptionKz: str = Field(default="", max_length=800)

    @model_validator(mode="after")
    def require_title(self) -> FederationValue:
        if not (self.titleRu or self.titleKz):
            raise ValueError("federation value needs a localized title")
        return self


class FederationGoal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default="", max_length=80)
    titleRu: str = Field(default="", max_length=200)
    titleKz: str = Field(default="", max_length=200)
    descriptionRu: str = Field(default="", max_length=900)
    descriptionKz: str = Field(default="", max_length=900)
    kpiRu: str = Field(default="", max_length=300)
    kpiKz: str = Field(default="", max_length=300)
    ownerRu: str = Field(default="", max_length=180)
    ownerKz: str = Field(default="", max_length=180)
    deadlineRu: str = Field(default="", max_length=100)
    deadlineKz: str = Field(default="", max_length=100)

    @model_validator(mode="after")
    def require_title(self) -> FederationGoal:
        if not (self.titleRu or self.titleKz):
            raise ValueError("federation goal needs a localized title")
        return self


class FederationMission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    statementRu: str = Field(default="", max_length=2400)
    statementKz: str = Field(default="", max_length=2400)
    visionRu: str = Field(default="", max_length=2400)
    visionKz: str = Field(default="", max_length=2400)
    values: list[FederationValue] = Field(default_factory=list, max_length=8)
    goals: list[FederationGoal] = Field(default_factory=list, max_length=12)


class FederationTimelineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default="", max_length=80)
    yearRu: str = Field(default="", max_length=32)
    yearKz: str = Field(default="", max_length=32)
    titleRu: str = Field(default="", max_length=220)
    titleKz: str = Field(default="", max_length=220)
    descriptionRu: str = Field(default="", max_length=2400)
    descriptionKz: str = Field(default="", max_length=2400)
    sourceUrl: str = Field(default="", max_length=1000)

    @field_validator("sourceUrl")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        return _validate_public_url(value)

    @model_validator(mode="after")
    def require_title(self) -> FederationTimelineItem:
        if not (self.titleRu or self.titleKz):
            raise ValueError("timeline item needs a localized title")
        return self


class FederationRoadmapItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default="", max_length=80)
    phaseRu: str = Field(default="", max_length=160)
    phaseKz: str = Field(default="", max_length=160)
    labelRu: str = Field(default="", max_length=120)
    labelKz: str = Field(default="", max_length=120)
    itemsRu: str = Field(default="", max_length=2400)
    itemsKz: str = Field(default="", max_length=2400)
    done: bool = False

    @model_validator(mode="after")
    def require_phase(self) -> FederationRoadmapItem:
        if not (self.phaseRu or self.phaseKz):
            raise ValueError("roadmap item needs a localized phase")
        return self


class FederationLeader(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(max_length=80)
    nameRu: str = Field(default="", max_length=160)
    nameKz: str = Field(default="", max_length=160)
    roleRu: str = Field(default="", max_length=160)
    roleKz: str = Field(default="", max_length=160)
    bioRu: str = Field(default="", max_length=1200)
    bioKz: str = Field(default="", max_length=1200)
    focusRu: str = Field(default="", max_length=500)
    focusKz: str = Field(default="", max_length=500)
    photo: str = Field(default="", max_length=1000)
    email: str = Field(default="", max_length=180)
    phone: str = Field(default="", max_length=80)

    @field_validator("photo")
    @classmethod
    def validate_photo(cls, value: str) -> str:
        return _validate_public_url(value)

    @model_validator(mode="after")
    def require_name(self) -> FederationLeader:
        if not (self.nameRu or self.nameKz):
            raise ValueError("leader needs a localized name")
        return self


class PublicFederation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mission: FederationMission = Field(default_factory=FederationMission)
    history: list[FederationTimelineItem] = Field(default_factory=list, max_length=15)
    achievements: list[FederationTimelineItem] = Field(default_factory=list, max_length=15)
    roadmap: list[FederationRoadmapItem] = Field(default_factory=list, max_length=12)
    leadership: list[FederationLeader] = Field(default_factory=list, max_length=12)


class FederationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    version: Literal[1] = 1
    generatedAt: str = Field(max_length=60)
    federation: PublicFederation


class PublicNewsBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["paragraph", "heading", "quote"]
    text: str = Field(min_length=1, max_length=2400)


class PublicNewsSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=200)
    url: str = Field(max_length=1000)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _validate_public_url(value)


class PublicNewsItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(max_length=120, pattern=r"^[a-z0-9-]+$")
    scope: Literal["national", "city"]
    citySlug: str = Field(default="", max_length=80, pattern=r"^$|^[a-z0-9-]+$")
    publishedAt: str = Field(max_length=60)
    titleRu: str = Field(min_length=1, max_length=240)
    titleKz: str = Field(min_length=1, max_length=240)
    titleEn: str = Field(default="", max_length=240)
    excerptRu: str = Field(min_length=1, max_length=600)
    excerptKz: str = Field(min_length=1, max_length=600)
    excerptEn: str = Field(default="", max_length=600)
    bodyRu: list[PublicNewsBlock] = Field(min_length=1, max_length=30)
    bodyKz: list[PublicNewsBlock] = Field(min_length=1, max_length=30)
    bodyEn: list[PublicNewsBlock] = Field(default_factory=list, max_length=30)
    sources: list[PublicNewsSource] = Field(default_factory=list, max_length=10)
    image: str = Field(default="", max_length=1000)
    imageAltRu: str = Field(default="", max_length=300)
    imageAltKz: str = Field(default="", max_length=300)
    imageAltEn: str = Field(default="", max_length=300)
    videoUrl: str = Field(default="", max_length=1000)

    @field_validator("image")
    @classmethod
    def validate_image(cls, value: str) -> str:
        return _validate_public_url(value, allow_local=True)

    @field_validator("videoUrl")
    @classmethod
    def validate_video(cls, value: str) -> str:
        return _validate_public_url(value)

    @model_validator(mode="after")
    def validate_scope_and_media(self) -> PublicNewsItem:
        if (self.scope == "city") != bool(self.citySlug):
            raise ValueError("city news requires citySlug; national news forbids it")
        if self.image and not (self.imageAltRu and self.imageAltKz):
            raise ValueError("news image requires RU/KZ alt text")
        return self


class NewsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    version: Literal[1] = 1
    generatedAt: str = Field(max_length=60)
    items: list[PublicNewsItem]


class Actor(BaseModel):
    user_id: UUID
    telegram_id: int
    roles: frozenset[Role]
    city_scopes: frozenset[UUID]
    active: bool = True

    def has_any_role(self, *roles: Role) -> bool:
        return bool(self.roles.intersection(roles))

    def can_access_city(self, city_id: UUID) -> bool:
        return Role.SUPERADMIN in self.roles or city_id in self.city_scopes


def utc_now() -> datetime:
    return datetime.now(UTC)
