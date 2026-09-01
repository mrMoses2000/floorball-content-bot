from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from floorball_bot.dialogue.evaluator import evaluate_gaps
from floorball_bot.dialogue.repository import DialogueSpecRepository
from floorball_bot.exporters import build_news_payload, deterministic_json
from floorball_bot.projection.news import NewsDraftInput


def complete_fields():
    return {
        "scope": "national",
        "city_slug": "",
        "slug": "kazakhstan-cup-2026",
        "published_at": "2026-09-01T10:00:00Z",
        "title_ru": "Кубок Казахстана",
        "title_kz": "Қазақстан кубогы",
        "title_en": "",
        "excerpt_ru": "Подтверждённый анонс.",
        "excerpt_kz": "Расталған аңдатпа.",
        "excerpt_en": "",
        "body_ru": [{"type": "paragraph", "text": "Текст новости."}],
        "body_kz": [{"type": "paragraph", "text": "Жаңалық мәтіні."}],
        "body_en": [],
        "sources": [{"label": "Федерация", "url": "https://floorball.kz/documents"}],
        "media": None,
        "video_url": "",
        "publication_permission": True,
    }


def test_news_dialogue_has_deterministic_publish_gate():
    spec = DialogueSpecRepository().load("news").spec
    incomplete = evaluate_gaps(spec, {"scope": "city"})
    assert "city_slug" in {gap.field_id for gap in incomplete.required_to_start}
    complete = evaluate_gaps(spec, complete_fields())
    assert complete.can_submit
    assert complete.can_publish


def test_news_input_rejects_scope_mismatch_and_non_https_sources():
    with pytest.raises(ValidationError):
        NewsDraftInput.model_validate({**complete_fields(), "scope": "city"})
    bad = complete_fields()
    bad["sources"] = [{"label": "bad", "url": "javascript:alert(1)"}]
    with pytest.raises(ValidationError):
        NewsDraftInput.model_validate(bad)


def test_news_public_contract_keeps_body_as_typed_text_not_html():
    fields = complete_fields()
    fields["body_ru"] = [{"type": "paragraph", "text": "<script>alert(1)</script>"}]
    payload = build_news_payload(
        [{
            "slug": fields["slug"],
            "scope": fields["scope"],
            "citySlug": "",
            "publishedAt": fields["published_at"],
            "titleRu": fields["title_ru"],
            "titleKz": fields["title_kz"],
            "excerptRu": fields["excerpt_ru"],
            "excerptKz": fields["excerpt_kz"],
            "bodyRu": fields["body_ru"],
            "bodyKz": fields["body_kz"],
            "sources": fields["sources"],
        }],
        generated_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    encoded = deterministic_json(payload)
    assert "<script>" in encoded
    assert "bodyRu" in encoded
