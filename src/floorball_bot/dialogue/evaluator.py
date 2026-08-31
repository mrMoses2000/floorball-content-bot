from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from floorball_bot.dialogue.models import (
    AnyOfRule,
    DialogueSpec,
    FieldSpec,
    FieldType,
    Gap,
    GapReport,
    RequirementCondition,
    RequirementLevel,
)


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _lookup(values: Mapping[str, Any], field_id: str) -> Any:
    if field_id in values:
        return values[field_id]
    current: Any = values
    for part in field_id.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _condition_matches(condition: RequirementCondition, values: Mapping[str, Any]) -> bool:
    actual = _lookup(values, condition.field_id)
    if condition.operator == "present":
        return _present(actual)
    if condition.operator == "equals":
        return actual == condition.value
    if condition.operator == "not_equals":
        return _present(actual) and actual != condition.value
    if condition.operator == "greater_than":
        return isinstance(actual, (int, float)) and actual > condition.value
    return False


def _is_required(field: FieldSpec, values: Mapping[str, Any]) -> bool:
    return not field.required_when or all(
        _condition_matches(condition, values) for condition in field.required_when
    )


def _requirement_satisfied(field: FieldSpec, value: Any) -> bool:
    if not _present(value):
        return False
    if field.type == FieldType.BOOLEAN and field.privacy == "consent":
        return value is True
    return True


def _field_gaps(
    field: FieldSpec,
    values: Mapping[str, Any],
    *,
    prefix: str = "",
    local_values: Mapping[str, Any] | None = None,
) -> list[Gap]:
    field_id = f"{prefix}.{field.id}" if prefix else field.id
    value = local_values.get(field.id) if local_values is not None else _lookup(values, field_id)
    gaps: list[Gap] = []
    active = _is_required(field, local_values or values)
    if (
        active
        and field.requirement != RequirementLevel.OPTIONAL
        and not _requirement_satisfied(field, value)
    ):
        gaps.append(
            Gap(field_id=field_id, requirement=field.requirement, question=field.question)
        )
        return gaps
    if not _present(value):
        return gaps
    if field.type == FieldType.RECORD and isinstance(value, Mapping):
        for child in field.children:
            gaps.extend(_field_gaps(child, values, prefix=field_id, local_values=value))
    elif field.type == FieldType.RECORD_LIST and isinstance(value, list):
        for index, record in enumerate(value[: field.max_items]):
            if not isinstance(record, Mapping):
                continue
            for child in field.children:
                gaps.extend(
                    _field_gaps(
                        child,
                        values,
                        prefix=f"{field_id}[{index}]",
                        local_values=record,
                    )
                )
    return gaps


def _rule_gap(rule: AnyOfRule, values: Mapping[str, Any]) -> Gap | None:
    if any(_present(_lookup(values, field_id)) for field_id in rule.field_ids):
        return None
    return Gap(field_id=rule.id, requirement=rule.requirement, question=rule.question)


def evaluate_gaps(spec: DialogueSpec, values: Mapping[str, Any]) -> GapReport:
    """Evaluate completeness without asking an LLM to decide what is missing."""
    gaps: list[Gap] = []
    for field in spec.fields:
        gaps.extend(_field_gaps(field, values))
    for rule in spec.completion_rules:
        gap = _rule_gap(rule, values)
        if gap:
            gaps.append(gap)

    grouped = {
        level: tuple(gap for gap in gaps if gap.requirement == level)
        for level in RequirementLevel
    }
    ordered = (
        *grouped[RequirementLevel.REQUIRED_TO_START],
        *grouped[RequirementLevel.REQUIRED_FOR_SUBMIT],
        *grouped[RequirementLevel.REQUIRED_FOR_PUBLISH],
        *grouped[RequirementLevel.RECOMMENDED],
    )
    return GapReport(
        required_to_start=grouped[RequirementLevel.REQUIRED_TO_START],
        required_for_submit=grouped[RequirementLevel.REQUIRED_FOR_SUBMIT],
        required_for_publish=grouped[RequirementLevel.REQUIRED_FOR_PUBLISH],
        recommended=grouped[RequirementLevel.RECOMMENDED],
        next_field_id=ordered[0].field_id if ordered else None,
    )
