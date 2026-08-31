from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from floorball_bot.dialogue.evaluator import evaluate_gaps
from floorball_bot.dialogue.patches import validate_field_value
from floorball_bot.dialogue.repository import DialogueSpecRepository


class CityDirectoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    slug: str = Field(pattern=r"^[a-z0-9-]+$", max_length=80)
    name_ru: str = Field(max_length=120)
    name_kz: str = Field(max_length=120)
    name_en: str = Field(max_length=120)


class ClubProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=180)
    age_groups: tuple[str, ...] = ()
    notes: str = Field(default="", max_length=600)
    contact_name: str = Field(default="", max_length=180)
    contact_phone_private: str = Field(default="", max_length=80)
    contact_is_public: bool = False

    def public_value(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ageGroups": list(self.age_groups),
            "notes": self.notes,
            "contactName": self.contact_name if self.contact_is_public else "",
            "contactPhone": self.contact_phone_private if self.contact_is_public else "",
        }


class ScheduleProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    day: Literal[
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
    ]
    time: str = Field(min_length=1, max_length=80)
    venue: str = Field(min_length=1, max_length=240)
    address: str = Field(default="", max_length=300)
    group: str = Field(default="", max_length=180)

    def public_value(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class TrainerProjectionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    city_slug: str | None = Field(default=None, max_length=80)
    proposed_city_name: str = Field(default="", max_length=120)
    preferred_language: Literal["ru", "kz"]
    description_ru: str | None = Field(default=None, max_length=1200)
    description_kz: str | None = Field(default=None, max_length=1200)
    history_ru: str | None = Field(default=None, max_length=2400)
    history_kz: str | None = Field(default=None, max_length=2400)
    players_estimate: int | None = Field(default=None, ge=0, le=1_000_000)
    coaches_estimate: int | None = Field(default=None, ge=0, le=100_000)
    clubs_estimate: int | None = Field(default=None, ge=0, le=100_000)
    clubs: tuple[ClubProjection, ...] = ()
    schedules: tuple[ScheduleProjection, ...] = ()
    blockers: tuple[str, ...] = ()
    publish_missing: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return not self.blockers and self.city_slug is not None

    def public_preview(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "slug": self.city_slug,
            "players": self.players_estimate,
            "coaches": self.coaches_estimate,
            "clubs": self.clubs_estimate,
            "clubs_list": [club.public_value() for club in self.clubs],
            "schedule": [schedule.public_value() for schedule in self.schedules],
        }
        if self.description_ru is not None:
            value["descRu"] = self.description_ru
        if self.description_kz is not None:
            value["descKz"] = self.description_kz
        if self.history_ru is not None:
            value["historyRu"] = self.history_ru
        if self.history_kz is not None:
            value["historyKz"] = self.history_kz
        return value


def _validated_fields(fields: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    spec = DialogueSpecRepository().load("trainer").spec
    definitions = {field.id: field for field in spec.fields}
    validated: dict[str, Any] = {}
    blockers: list[str] = []
    for field_id, value in fields.items():
        definition = definitions.get(field_id)
        if definition is None:
            blockers.append(f"unknown_field:{field_id}")
            continue
        try:
            validated[field_id] = validate_field_value(definition, value)
        except ValueError:
            blockers.append(f"invalid_field:{field_id}")
    return validated, blockers


def _resolve_city(
    city: Mapping[str, Any], directory: Sequence[CityDirectoryEntry]
) -> tuple[str | None, str, str | None]:
    selected = str(city.get("name") or "").strip()
    proposed = str(city.get("other_name") or "").strip()
    if selected.casefold() == "другой":
        return None, proposed, "new_city_application_required"
    normalized = selected.casefold()
    matches = [
        entry
        for entry in directory
        if normalized
        in {
            entry.slug.casefold(),
            entry.name_ru.casefold(),
            entry.name_kz.casefold(),
            entry.name_en.casefold(),
        }
    ]
    if len(matches) == 1:
        return matches[0].slug, "", None
    if len(matches) > 1:
        return None, proposed or selected, "city_selection_ambiguous"
    return None, proposed or selected, "city_not_found"


def _age_groups(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str):
        return ()
    return tuple(part.strip() for part in re.split(r"[,;\n]+", value) if part.strip())


def plan_trainer_projection(
    fields: Mapping[str, Any],
    *,
    preferred_language: str,
    city_directory: Sequence[CityDirectoryEntry],
) -> TrainerProjectionPlan:
    """Build a deterministic, non-mutating canonical projection plan.

    The plan is suitable for reviewer dry-run output. It never invents a city slug and its
    public preview redacts contacts without explicit per-club publication permission.
    """
    language = preferred_language if preferred_language in {"ru", "kz"} else "ru"
    validated, blockers = _validated_fields(fields)
    spec = DialogueSpecRepository().load("trainer").spec
    gaps = evaluate_gaps(spec, validated)
    blockers.extend(
        gap.field_id for gap in (*gaps.required_to_start, *gaps.required_for_submit)
    )

    city_value = validated.get("city")
    city_slug: str | None = None
    proposed_city_name = ""
    if isinstance(city_value, Mapping):
        city_slug, proposed_city_name, city_blocker = _resolve_city(
            city_value, city_directory
        )
        if city_blocker:
            blockers.append(city_blocker)
    else:
        blockers.append("city_not_resolved")

    metrics = validated.get("metrics") if isinstance(validated.get("metrics"), Mapping) else {}
    description = str(city_value.get("summary") or "") if isinstance(city_value, Mapping) else ""
    history = str(city_value.get("history") or "") if isinstance(city_value, Mapping) else ""

    clubs: list[ClubProjection] = []
    for club in validated.get("clubs", []):
        if not isinstance(club, Mapping) or not club.get("name"):
            continue
        clubs.append(
            ClubProjection(
                name=str(club["name"]),
                age_groups=_age_groups(club.get("age_groups")),
                notes=str(club.get("notes") or ""),
                contact_name=str(club.get("contact_name") or ""),
                contact_phone_private=str(club.get("contact_phone") or ""),
                contact_is_public=club.get("public_contact") == "да",
            )
        )

    warnings: list[str] = []
    schedules: list[ScheduleProjection] = []
    for index, schedule in enumerate(validated.get("schedule", [])):
        if not isinstance(schedule, Mapping):
            continue
        if schedule.get("public_permission") != "да":
            warnings.append(f"schedule[{index}].not_public")
            continue
        schedules.append(
            ScheduleProjection(
                day=schedule["day"],
                time=str(schedule["time"]),
                venue=str(schedule["venue"]),
                address=str(schedule.get("address") or ""),
                group=str(schedule.get("group") or ""),
            )
        )
    if validated.get("players"):
        warnings.append("players.require_separate_consent_projection")
    media = validated.get("media")
    if isinstance(media, Mapping) and (media.get("hero") or media.get("gallery")):
        warnings.append("media.require_moderation_projection")

    return TrainerProjectionPlan(
        city_slug=city_slug,
        proposed_city_name=proposed_city_name,
        preferred_language=language,
        description_ru=description if language == "ru" else None,
        description_kz=description if language == "kz" else None,
        history_ru=history if language == "ru" else None,
        history_kz=history if language == "kz" else None,
        players_estimate=metrics.get("players_total"),
        coaches_estimate=metrics.get("coaches_total"),
        clubs_estimate=metrics.get("clubs_total"),
        clubs=tuple(clubs),
        schedules=tuple(schedules),
        blockers=tuple(dict.fromkeys(blockers)),
        publish_missing=tuple(gap.field_id for gap in gaps.required_for_publish),
        warnings=tuple(warnings),
    )
