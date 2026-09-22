from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

_TRANSLITERATION = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "shch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
        "ә": "a", "ғ": "g", "қ": "q", "ң": "n", "ө": "o", "ұ": "u", "ү": "u",
        "һ": "h", "і": "i",
    }
)


def news_slug(title: str) -> str:
    transliterated = title.casefold().translate(_TRANSLITERATION)
    base = re.sub(r"[^a-z0-9]+", "-", transliterated).strip("-")[:92].rstrip("-")
    return base or "floorball-news"


def apply_news_defaults(fields: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(fields)
    enriched.setdefault("published_at", datetime.now(UTC).isoformat(timespec="seconds"))
    title = str(enriched.get("title_ru") or enriched.get("title_kz") or "").strip()
    if title and not enriched.get("slug"):
        enriched["slug"] = news_slug(title)
    return enriched
