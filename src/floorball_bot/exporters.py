from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from floorball_bot.domain import CityPayload, PublicCity
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
    payload = CityPayload(generatedAt=generated_at.astimezone(UTC).isoformat(), cities=public)
    assert_private_fields_absent(payload.model_dump())
    return payload


def deterministic_json(model: CityPayload | dict[str, Any]) -> str:
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
