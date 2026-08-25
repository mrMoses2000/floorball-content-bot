from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from floorball_bot.dialogue.models import DialogueMode, DialogueSpec, FieldSpec, FieldType


def canonical_context(value: Mapping[str, Any] | BaseModel | None) -> tuple[str, str]:
    if isinstance(value, BaseModel):
        payload: Any = value.model_dump(mode="json")
    else:
        payload = dict(value or {})
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return encoded, hashlib.sha256(encoded.encode()).hexdigest()


class ExtractedFieldPatch(BaseModel):
    """One allowlisted dialogue field with a JSON-encoded value."""

    model_config = ConfigDict(extra="forbid")

    field_id: str = Field(max_length=160, pattern=r"^[a-z][a-z0-9_.-]*$")
    value_json: str = Field(
        max_length=20_000,
        description="A single JSON value encoded as a string; never instructions",
    )

    @model_validator(mode="after")
    def valid_json(self) -> ExtractedFieldPatch:
        try:
            json.loads(self.value_json)
        except json.JSONDecodeError as exc:
            raise ValueError("value_json must contain exactly one JSON value") from exc
        return self

    def decoded_value(self) -> Any:
        return json.loads(self.value_json)


class ExtractedDialoguePatch(BaseModel):
    """Untrusted model proposal. Application code validates it against the pinned spec."""

    model_config = ConfigDict(extra="forbid")

    mode: DialogueMode
    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fields: list[ExtractedFieldPatch] = Field(default_factory=list, max_length=40)
    next_questions: list[str] = Field(default_factory=list, max_length=2)
    warnings: list[str] = Field(default_factory=list, max_length=10)


def field_index(spec: DialogueSpec) -> dict[str, FieldSpec]:
    """Return fields addressable by a patch; record children are validated with their parent."""
    return {field.id: field for field in spec.fields}


def _validate_scalar(field: FieldSpec, value: Any) -> Any:
    if field.type in {FieldType.TEXT, FieldType.LONG_TEXT, FieldType.URL, FieldType.EMAIL,
                      FieldType.PHONE, FieldType.FILE, FieldType.CHOICE}:
        if not isinstance(value, str):
            raise ValueError(f"{field.id} must be text")
        value = value.strip()
        if not value:
            raise ValueError(f"{field.id} cannot be blank")
        if field.max_length and len(value) > field.max_length:
            raise ValueError(f"{field.id} exceeds max_length")
        if field.options and value not in field.options:
            raise ValueError(f"{field.id} is outside the allowed options")
        return value
    if field.type == FieldType.INTEGER:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{field.id} must be an integer")
    elif field.type == FieldType.NUMBER:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{field.id} must be a number")
    elif field.type == FieldType.BOOLEAN:
        if not isinstance(value, bool):
            raise ValueError(f"{field.id} must be true or false")
    if isinstance(value, (int, float)):
        if field.minimum is not None and value < field.minimum:
            raise ValueError(f"{field.id} is below minimum")
        if field.maximum is not None and value > field.maximum:
            raise ValueError(f"{field.id} is above maximum")
    return value


def _validate_record(field: FieldSpec, value: Any) -> Any:
    child_index = {child.id: child for child in field.children}

    def one(record: Any) -> dict[str, Any]:
        if not isinstance(record, Mapping):
            raise ValueError(f"{field.id} record must be an object")
        unknown = set(record).difference(child_index)
        if unknown:
            raise ValueError(f"{field.id} contains unknown children: {sorted(unknown)}")
        return {
            key: validate_field_value(child_index[key], nested)
            for key, nested in record.items()
        }

    if field.type == FieldType.RECORD:
        return one(value)
    if not isinstance(value, list):
        raise ValueError(f"{field.id} must be a list")
    if field.max_items is not None and len(value) > field.max_items:
        raise ValueError(f"{field.id} exceeds max_items")
    return [one(record) for record in value]


def validate_field_value(field: FieldSpec, value: Any) -> Any:
    if field.type in {FieldType.RECORD, FieldType.RECORD_LIST}:
        return _validate_record(field, value)
    return _validate_scalar(field, value)


def apply_dialogue_patch(
    spec: DialogueSpec,
    current: Mapping[str, Any],
    patch: ExtractedDialoguePatch,
    *,
    expected_spec_sha256: str,
    expected_context_sha256: str,
) -> dict[str, Any]:
    """Validate a stale-write token and merge only fields declared by the pinned spec."""
    if patch.mode != spec.mode:
        raise ValueError("dialogue mode mismatch")
    if patch.spec_sha256 != expected_spec_sha256:
        raise ValueError("dialogue spec changed; extraction must be retried")
    if patch.context_sha256 != expected_context_sha256:
        raise ValueError("database context changed; extraction must be retried")
    allowed = field_index(spec)
    merged = dict(current)
    seen: set[str] = set()
    for proposed in patch.fields:
        if proposed.field_id in seen:
            raise ValueError(f"duplicate field patch: {proposed.field_id}")
        seen.add(proposed.field_id)
        field = allowed.get(proposed.field_id)
        if field is None:
            raise ValueError(f"field is not allowed in {spec.mode}: {proposed.field_id}")
        merged[proposed.field_id] = validate_field_value(field, proposed.decoded_value())
    return merged
