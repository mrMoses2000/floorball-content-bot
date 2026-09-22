from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from floorball_bot.dialogue import DialogueMode, DialogueSpecRepository
from floorball_bot.miniapp_api import MiniAppError, _serialize_session, validate_init_data
from floorball_bot.news_defaults import apply_news_defaults, news_slug


def signed_init_data(token: str, *, telegram_id: int = 123, auth_date: int | None = None) -> str:
    data = {
        "auth_date": str(auth_date or int(time.time())),
        "query_id": "AAE-test",
        "user": json.dumps({"id": telegram_id, "first_name": "Test"}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{key}={data[key]}" for key in sorted(data))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


def test_valid_init_data_returns_verified_identity() -> None:
    identity = validate_init_data(signed_init_data("bot-token", telegram_id=777), "bot-token", 60)
    assert identity["telegram_id"] == 777
    assert identity["telegram_user"]["first_name"] == "Test"


def test_init_data_rejects_tampering() -> None:
    raw = signed_init_data("bot-token").replace("Test", "Other")
    with pytest.raises(MiniAppError, match="проверить вход"):
        validate_init_data(raw, "bot-token", 60)


def test_init_data_rejects_expired_session() -> None:
    raw = signed_init_data("bot-token", auth_date=int(time.time()) - 120)
    with pytest.raises(MiniAppError, match="устарел"):
        validate_init_data(raw, "bot-token", 60)


def test_news_defaults_hide_technical_work_from_author() -> None:
    values = apply_news_defaults({"title_ru": "Кубок Казахстана — 2026!"})
    assert values["slug"] == "kubok-kazahstana-2026"
    assert values["published_at"].endswith("+00:00")
    assert news_slug("Әлем чемпионаты") == "alem-chempionaty"


def test_news_miniapp_uses_plain_language_and_hides_technical_fields() -> None:
    loaded = DialogueSpecRepository().load(DialogueMode.NEWS)
    session = {
        "id": "00000000-0000-0000-0000-000000000001",
        "status": "active",
        "memory_revision": 1,
    }
    payload = _serialize_session(
        loaded,
        session,
        {"fields": {"scope": "city", "city_slug": "almaty"}},
        "ru",
        [{"value": "almaty", "label": "Алматы"}],
    )
    fields = [field for section in payload["sections"] for field in section["fields"]]
    field_ids = {field["id"] for field in fields}
    assert not {
        "slug",
        "published_at",
        "title_kz",
        "title_en",
        "excerpt_kz",
        "excerpt_en",
        "body_kz",
        "body_en",
        "sources",
    }.intersection(field_ids)
    assert next(field for field in fields if field["id"] == "scope")["label"] == (
        "Где показать новость?"
    )
    city = next(field for field in fields if field["id"] == "city_slug")
    assert city["type"] == "choice"
    assert city["display_value"] == "Алматы"
    assert "ISO" not in json.dumps(payload, ensure_ascii=False)
    assert "url-slug" not in json.dumps(payload, ensure_ascii=False).casefold()
