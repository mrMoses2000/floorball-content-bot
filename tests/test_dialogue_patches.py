import json

import pytest

from floorball_bot.dialogue.patches import (
    ExtractedDialoguePatch,
    ExtractedFieldPatch,
    apply_dialogue_patch,
    canonical_context,
)
from floorball_bot.dialogue.repository import DialogueSpecRepository


def _patch(field_id: str, value, *, spec_hash: str, context_hash: str):
    return ExtractedDialoguePatch(
        mode="trainer",
        spec_sha256=spec_hash,
        context_sha256=context_hash,
        fields=[
            ExtractedFieldPatch(
                field_id=field_id,
                value_json=json.dumps(value, ensure_ascii=False),
            )
        ],
    )


def test_validated_patch_merges_only_declared_typed_fields():
    loaded = DialogueSpecRepository().load("trainer")
    _, context_hash = canonical_context({"cities": [{"slug": "almaty"}]})
    proposed = _patch(
        "city",
        {
            "name": "Алматы",
            "status": "есть регулярные тренировки",
            "summary": "Регулярные тренировки.",
            "history": "Краткая подтверждённая история.",
        },
        spec_hash=loaded.sha256,
        context_hash=context_hash,
    )

    merged = apply_dialogue_patch(
        loaded.spec,
        {},
        proposed,
        expected_spec_sha256=loaded.sha256,
        expected_context_sha256=context_hash,
    )

    assert merged["city"]["name"] == "Алматы"


@pytest.mark.parametrize("changed", ["spec", "context"])
def test_stale_hashes_are_rejected(changed):
    loaded = DialogueSpecRepository().load("trainer")
    _, context_hash = canonical_context({})
    proposed = _patch(
        "city",
        {"name": "Алматы"},
        spec_hash="0" * 64 if changed == "spec" else loaded.sha256,
        context_hash="1" * 64 if changed == "context" else context_hash,
    )

    with pytest.raises(ValueError, match="changed"):
        apply_dialogue_patch(
            loaded.spec,
            {},
            proposed,
            expected_spec_sha256=loaded.sha256,
            expected_context_sha256=context_hash,
        )


def test_unknown_field_is_rejected_before_memory_write():
    loaded = DialogueSpecRepository().load("trainer")
    _, context_hash = canonical_context({})
    proposed = _patch(
        "publish_now",
        True,
        spec_hash=loaded.sha256,
        context_hash=context_hash,
    )

    with pytest.raises(ValueError, match="not allowed"):
        apply_dialogue_patch(
            loaded.spec,
            {},
            proposed,
            expected_spec_sha256=loaded.sha256,
            expected_context_sha256=context_hash,
        )
