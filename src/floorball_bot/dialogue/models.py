from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DialogueMode(StrEnum):
    TRAINER = "trainer"
    STRATEGY = "strategy"
    HISTORY = "history"
    LEADERSHIP = "leadership"
    NEWS = "news"


class RequirementLevel(StrEnum):
    REQUIRED_TO_START = "required_to_start"
    REQUIRED_FOR_SUBMIT = "required_for_submit"
    REQUIRED_FOR_PUBLISH = "required_for_publish"
    RECOMMENDED = "recommended"
    OPTIONAL = "optional"


class FieldType(StrEnum):
    TEXT = "text"
    LONG_TEXT = "long_text"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    CHOICE = "choice"
    URL = "url"
    EMAIL = "email"
    PHONE = "phone"
    FILE = "file"
    RECORD = "record"
    RECORD_LIST = "record_list"


class LocalizedQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ru: str = Field(min_length=1, max_length=1200)
    kz: str = Field(min_length=1, max_length=1200)


class RequirementCondition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field_id: str = Field(min_length=1, max_length=160)
    operator: Literal["present", "equals", "not_equals", "greater_than"]
    value: Any | None = None


class FieldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$", max_length=160)
    section: str = Field(min_length=1, max_length=120)
    type: FieldType
    requirement: RequirementLevel = RequirementLevel.OPTIONAL
    question: LocalizedQuestion
    source_keys: tuple[str, ...] = ()
    db_target: str = Field(min_length=1, max_length=240)
    site_target: str | None = Field(default=None, max_length=240)
    privacy: Literal[
        "public_after_approval",
        "private",
        "internal",
        "consent",
        "media_private_until_approved",
    ]
    max_length: int | None = Field(default=None, gt=0)
    minimum: float | None = None
    maximum: float | None = None
    options: tuple[str, ...] = ()
    max_items: int | None = Field(default=None, gt=0)
    children: tuple[FieldSpec, ...] = ()
    required_when: tuple[RequirementCondition, ...] = ()

    @model_validator(mode="after")
    def validate_shape(self) -> FieldSpec:
        if self.type in {FieldType.RECORD, FieldType.RECORD_LIST} and not self.children:
            raise ValueError("record fields require children")
        if self.type not in {FieldType.RECORD, FieldType.RECORD_LIST} and self.children:
            raise ValueError("only record fields may define children")
        if self.type == FieldType.RECORD_LIST and self.max_items is None:
            raise ValueError("record_list fields require max_items")
        if self.type == FieldType.CHOICE and not self.options:
            raise ValueError("choice fields require options")
        return self


class AnyOfRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$", max_length=160)
    requirement: RequirementLevel
    field_ids: tuple[str, ...] = Field(min_length=1)
    question: LocalizedQuestion


class DialogueSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    mode: DialogueMode
    version: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    ui_label: LocalizedQuestion
    target_scope: Literal["city", "federation"]
    allowed_roles: tuple[str, ...] = Field(min_length=1)
    fields: tuple[FieldSpec, ...] = Field(min_length=1)
    completion_rules: tuple[AnyOfRule, ...] = ()

    @model_validator(mode="after")
    def unique_ids_and_valid_references(self) -> DialogueSpec:
        ids: set[str] = set()

        def visit(field: FieldSpec, prefix: str = "") -> None:
            full_id = f"{prefix}.{field.id}" if prefix else field.id
            if full_id in ids:
                raise ValueError(f"duplicate field id: {full_id}")
            ids.add(full_id)
            for child in field.children:
                visit(child, full_id)

        for field in self.fields:
            visit(field)
        rule_ids: set[str] = set()
        for rule in self.completion_rules:
            if rule.id in ids or rule.id in rule_ids:
                raise ValueError(f"duplicate completion rule id: {rule.id}")
            rule_ids.add(rule.id)
            unknown = set(rule.field_ids).difference(ids)
            if unknown:
                raise ValueError(f"completion rule {rule.id} references unknown fields: {unknown}")
        return self


class Gap(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field_id: str
    requirement: RequirementLevel
    question: LocalizedQuestion


class GapReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    required_to_start: tuple[Gap, ...] = ()
    required_for_submit: tuple[Gap, ...] = ()
    required_for_publish: tuple[Gap, ...] = ()
    recommended: tuple[Gap, ...] = ()
    next_field_id: str | None = None

    @property
    def can_submit(self) -> bool:
        return not self.required_to_start and not self.required_for_submit

    @property
    def can_publish(self) -> bool:
        return self.can_submit and not self.required_for_publish
