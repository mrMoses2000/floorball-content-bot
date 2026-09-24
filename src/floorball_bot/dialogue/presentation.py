from __future__ import annotations

from typing import Any

from floorball_bot.dialogue.models import (
    DialogueMode,
    FieldSpec,
    LocalizedQuestion,
)

NEWS_COPY: dict[str, dict[str, Any]] = {
    "scope": {
        "section_ru": "Публикация",
        "section_kz": "Жариялау",
        "label_ru": "Где показать новость?",
        "label_kz": "Жаңалықты қайда көрсетеміз?",
        "question_ru": "Выберите: на сайте федерации или только в разделе города.",
        "question_kz": "Таңдаңыз: федерация сайтында немесе тек қала бөлімінде.",
        "options": {
            "national": {"ru": "На сайте федерации", "kz": "Федерация сайтында"},
            "city": {"ru": "В разделе города", "kz": "Қала бөлімінде"},
        },
    },
    "city_slug": {
        "section_ru": "Публикация",
        "section_kz": "Жариялау",
        "label_ru": "Город",
        "label_kz": "Қала",
        "question_ru": "О каком городе эта новость?",
        "question_kz": "Бұл жаңалық қай қала туралы?",
    },
    "slug": {
        "label_ru": "Адрес страницы",
        "label_kz": "Бет мекенжайы",
        "question_ru": "Адрес страницы будет создан автоматически из заголовка.",
        "question_kz": "Бет мекенжайы тақырыптан автоматты түрде жасалады.",
        "hidden": True,
    },
    "published_at": {
        "label_ru": "Дата публикации",
        "label_kz": "Жариялау күні",
        "question_ru": "По умолчанию будет использована сегодняшняя дата.",
        "question_kz": "Әдепкі бойынша бүгінгі күн қолданылады.",
        "hidden": True,
    },
    "title_ru": {
        "section_ru": "О новости",
        "section_kz": "Жаңалық туралы",
        "label_ru": "Заголовок",
        "label_kz": "Тақырып",
        "question_ru": "Как коротко назвать событие?",
        "question_kz": "Оқиғаны қысқаша қалай атаймыз?",
    },
    "title_kz": {"hidden": True},
    "title_en": {"hidden": True},
    "excerpt_ru": {
        "section_ru": "О новости",
        "section_kz": "Жаңалық туралы",
        "label_ru": "Коротко о событии",
        "label_kz": "Оқиға туралы қысқаша",
        "question_ru": "Что произошло? Достаточно двух–трёх предложений.",
        "question_kz": "Не болды? Екі-үш сөйлем жеткілікті.",
    },
    "excerpt_kz": {"hidden": True},
    "excerpt_en": {"hidden": True},
    "body_ru": {
        "section_ru": "О новости",
        "section_kz": "Жаңалық туралы",
        "label_ru": "Подробности",
        "label_kz": "Толығырақ",
        "question_ru": "Расскажите подробнее обычным сообщением или голосом.",
        "question_kz": "Толығырақ кәдімгі хабарламамен немесе дауыспен айтыңыз.",
    },
    "body_kz": {"hidden": True},
    "body_en": {"hidden": True},
    "sources": {"hidden": True},
    "media": {
        "section_ru": "Фото и видео",
        "section_kz": "Фото және видео",
        "label_ru": "Фотографии",
        "label_kz": "Фотосуреттер",
        "question_ru": "Отправьте фотографии прямо в чат с ботом.",
        "question_kz": "Фотосуреттерді ботпен чатқа жіберіңіз.",
    },
    "video_url": {
        "section_ru": "Фото и видео",
        "section_kz": "Фото және видео",
        "label_ru": "Видео, если есть",
        "label_kz": "Видео болса",
        "question_ru": "Добавьте ссылку на видео, если оно есть.",
        "question_kz": "Видео болса, сілтемесін қосыңыз.",
    },
    "publication_permission": {
        "label_ru": "Разрешение на публикацию",
        "label_kz": "Жариялауға рұқсат",
        "section_ru": "Подтверждение",
        "section_kz": "Растау",
        "question_ru": "Подтвердите, что новость и фотографии можно опубликовать.",
        "question_kz": "Жаңалық пен фотосуреттерді жариялауға болатынын растаңыз.",
    },
}

SECTION_KZ = {
    "Агрегированные цифры": "Жиынтық сандар",
    "Город": "Қала",
    "Игроки": "Ойыншылар",
    "Клубы": "Клубтар",
    "Медиа": "Медиа",
    "Ответственный": "Жауапты тұлға",
    "Ответственный представитель": "Жауапты өкіл",
    "Подтверждение": "Растау",
    "Расписание": "Кесте",
    "События": "Оқиғалар",
    "Миссия и видение": "Миссия және көзқарас",
    "Направления развития": "Даму бағыттары",
    "Стратегические цели": "Стратегиялық мақсаттар",
    "Ценности": "Құндылықтар",
    "Язык": "Тіл",
    "Дорожная карта": "Жол картасы",
    "Достижения": "Жетістіктер",
    "История": "Тарих",
    "Права на изображения": "Суреттерге құқықтар",
    "Профили руководства": "Басшылық туралы мәліметтер",
}


def field_copy(mode: DialogueMode, field: FieldSpec, language: str) -> dict[str, Any]:
    override = NEWS_COPY.get(field.id, {}) if mode == DialogueMode.NEWS else {}
    suffix = "kz" if language == "kz" else "ru"
    question = override.get(f"question_{suffix}") or getattr(field.question, suffix)
    label = override.get(f"label_{suffix}") or question.rstrip(" ?.")
    options = [
        {
            "value": value,
            "label": override.get("options", {}).get(value, {}).get(suffix, value),
        }
        for value in field.options
    ]
    return {
        "section": override.get(f"section_{suffix}") or (
            SECTION_KZ.get(field.section, field.section) if language == "kz" else field.section
        ),
        "label": label,
        "question": question,
        "options": options,
        "hidden": bool(override.get("hidden")),
    }


def friendly_question(
    mode: DialogueMode, field_id: str, fallback: LocalizedQuestion, language: str
) -> str:
    top_level = field_id.split(".", 1)[0].split("[", 1)[0]
    override = NEWS_COPY.get(top_level, {}) if mode == DialogueMode.NEWS else {}
    suffix = "kz" if language == "kz" else "ru"
    return override.get(f"question_{suffix}") or getattr(fallback, suffix)


def hidden_from_user(mode: DialogueMode, field_id: str) -> bool:
    top_level = field_id.split(".", 1)[0].split("[", 1)[0]
    return bool(mode == DialogueMode.NEWS and NEWS_COPY.get(top_level, {}).get("hidden"))
