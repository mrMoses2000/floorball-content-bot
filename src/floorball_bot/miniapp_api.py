from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl
from uuid import UUID

import asyncpg
from aiohttp import web
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from floorball_bot.auth import get_actor_by_telegram_id
from floorball_bot.db import transaction
from floorball_bot.dialogue import DialogueMode, DialogueSpecRepository
from floorball_bot.dialogue.evaluator import evaluate_gaps
from floorball_bot.dialogue.models import FieldType
from floorball_bot.dialogue.patches import validate_field_value
from floorball_bot.dialogue.presentation import field_copy, friendly_question, hidden_from_user
from floorball_bot.news_defaults import apply_news_defaults

POOL = web.AppKey("pool", asyncpg.Pool)
BOT_TOKEN = web.AppKey("bot_token", str)
AUTH_MAX_AGE = web.AppKey("auth_max_age", int)
DIST_ROOT = web.AppKey("dist_root", Path)
DIALOGUES = web.AppKey("dialogues", DialogueSpecRepository)


class MiniAppError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


class FieldMutation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    revision: int = Field(gt=0)
    value: Any | None = None
    clear: bool = False


class LanguageMutation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: str = Field(pattern=r"^(ru|kz)$")


def validate_init_data(raw: str, token: str, max_age_seconds: int) -> dict[str, Any]:
    if not raw or len(raw) > 16_384:
        raise MiniAppError(401, "invalid_auth", "Откройте приложение из Telegram ещё раз.")
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise MiniAppError(
            401, "invalid_auth", "Не удалось проверить вход через Telegram."
        ) from exc
    data: dict[str, str] = {}
    for key, value in pairs:
        if key in data:
            raise MiniAppError(401, "invalid_auth", "Не удалось проверить вход через Telegram.")
        data[key] = value
    supplied_hash = data.pop("hash", "")
    if len(supplied_hash) != 64:
        raise MiniAppError(401, "invalid_auth", "Не удалось проверить вход через Telegram.")
    check_string = "\n".join(f"{key}={data[key]}" for key in sorted(data))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied_hash):
        raise MiniAppError(401, "invalid_auth", "Не удалось проверить вход через Telegram.")
    try:
        auth_date = int(data["auth_date"])
        user = json.loads(data["user"])
        telegram_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MiniAppError(401, "invalid_auth", "В данных входа не хватает информации.") from exc
    now = int(time.time())
    if auth_date > now + 60 or now - auth_date > max_age_seconds:
        raise MiniAppError(401, "expired_auth", "Сеанс устарел. Откройте приложение заново.")
    return {"telegram_id": telegram_id, "telegram_user": user, "auth_date": auth_date}


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        response = await handler(request)
    except MiniAppError as exc:
        response = web.json_response(
            {"error": {"code": exc.code, "message": exc.message}}, status=exc.status
        )
    except (json.JSONDecodeError, ValidationError):
        response = web.json_response(
            {"error": {"code": "invalid_request", "message": "Проверьте введённые данные."}},
            status=400,
        )
    response.headers.update(
        {
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        }
    )
    return response


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if not request.path.startswith("/api/miniapp/"):
        return await handler(request)
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("tma "):
        raise MiniAppError(401, "auth_required", "Откройте приложение из Telegram.")
    identity = validate_init_data(
        authorization[4:], request.app[BOT_TOKEN], request.app[AUTH_MAX_AGE]
    )
    async with request.app[POOL].acquire() as connection:
        actor = await get_actor_by_telegram_id(connection, identity["telegram_id"])
    if actor is None or not actor.active:
        raise MiniAppError(
            403,
            "access_pending",
            "Доступ ещё не подтверждён. Вернитесь в чат и поделитесь своим контактом.",
        )
    request["actor"] = actor
    request["telegram_user"] = identity["telegram_user"]
    return await handler(request)


def _localized(value, language: str) -> str:
    return value.kz if language == "kz" else value.ru


def _editable(field) -> bool:
    return (
        field.type
        not in {FieldType.FILE, FieldType.RECORD_LIST}
        and field.privacy != "consent"
        and not field.db_target.startswith("users")
    )


def _serialize_session(
    loaded,
    session,
    memory: dict[str, Any],
    language: str,
    city_options: list[dict[str, str]],
) -> dict[str, Any]:
    fields = dict(memory.get("fields") or {})
    gaps = evaluate_gaps(loaded.spec, fields)
    missing = {
        gap.field_id
        for gap in (
            *gaps.required_to_start,
            *gaps.required_for_submit,
            *gaps.required_for_publish,
            *gaps.recommended,
        )
    }
    sections: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for field in loaded.spec.fields:
        if (
            loaded.spec.mode == DialogueMode.NEWS
            and field.id == "city_slug"
            and fields.get("scope") != "city"
        ):
            continue
        presentation = field_copy(loaded.spec.mode, field, language)
        if presentation["hidden"]:
            continue
        current = fields.get(field.id)
        options = (
            city_options
            if loaded.spec.mode == DialogueMode.NEWS and field.id == "city_slug"
            else presentation["options"]
        )
        option_label = next(
            (option["label"] for option in options if option["value"] == current), None
        )
        sections[presentation["section"]].append(
            {
                "id": field.id,
                "label": presentation["label"],
                "question": presentation["question"],
                "type": "choice" if options else field.type.value,
                "requirement": field.requirement.value,
                "value": current,
                "display_value": option_label,
                "filled": current not in (None, "", [], {}),
                "missing": field.id in missing or any(
                    item.startswith(f"{field.id}.") for item in missing
                ),
                "editable": session is not None
                and session["status"] == "active"
                and _editable(field)
                and field.type != FieldType.RECORD,
                "options": options,
                "max_length": field.max_length,
            }
        )
    completed = sum(item["filled"] for items in sections.values() for item in items)
    total = sum(len(items) for items in sections.values())
    return {
        "mode": loaded.spec.mode.value,
        "label": _localized(loaded.spec.ui_label, language),
        "available": True,
        "session_id": str(session["id"]) if session else None,
        "status": session["status"] if session else "not_started",
        "revision": int(session["memory_revision"]) if session else 0,
        "progress": round(completed * 100 / total) if total else 0,
        "completed_fields": completed,
        "total_fields": total,
        "can_submit": gaps.can_submit,
        "next_question": next(
            (
                friendly_question(loaded.spec.mode, gap.field_id, gap.question, language)
                for gap in (
                    *gaps.required_to_start,
                    *gaps.required_for_submit,
                    *gaps.required_for_publish,
                    *gaps.recommended,
                )
                if not hidden_from_user(loaded.spec.mode, gap.field_id)
            ),
            None,
        ),
        "sections": [{"title": title, "fields": items} for title, items in sections.items()],
    }


async def _bootstrap_payload(request: web.Request) -> dict[str, Any]:
    actor = request["actor"]
    dialogues = request.app[DIALOGUES]
    roles = {role.value for role in actor.roles}
    async with request.app[POOL].acquire() as connection:
        user = await connection.fetchrow(
            "SELECT display_name, preferred_language FROM users WHERE id=$1", actor.user_id
        )
        session_rows = await connection.fetch(
            """
            SELECT DISTINCT ON (s.workflow) s.id, s.workflow, s.status,
                   COALESCE(m.structured_memory, '{}'::jsonb) AS memory,
                   COALESCE(m.revision, 1) AS memory_revision
            FROM conversation_sessions s
            LEFT JOIN conversation_memory m ON m.session_id=s.id
            WHERE s.user_id=$1 AND s.workflow=ANY($2::text[])
            ORDER BY s.workflow,
                     CASE s.status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END,
                     s.last_activity_at DESC
            """,
            actor.user_id,
            [mode.value for mode in DialogueMode],
        )
        if roles.intersection({"superadmin", "federation_editor"}):
            cities = await connection.fetch(
                """
                SELECT slug, name_ru, name_kz FROM cities
                WHERE active=TRUE AND deleted_at IS NULL ORDER BY name_ru
                """
            )
        else:
            cities = await connection.fetch(
                """
                SELECT c.slug, c.name_ru, c.name_kz FROM user_city_scopes s
                JOIN cities c ON c.id=s.city_id
                WHERE s.user_id=$1 AND s.revoked_at IS NULL AND c.active=TRUE
                    AND c.deleted_at IS NULL
                ORDER BY c.name_ru
                """,
                actor.user_id,
            )
        is_coach = await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM coaches WHERE user_id=$1 AND deleted_at IS NULL)",
            actor.user_id,
        )
        is_player = await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM players WHERE user_id=$1 AND deleted_at IS NULL)",
            actor.user_id,
        )
    language = user["preferred_language"]
    city_options = [
        {
            "value": city["slug"],
            "label": city["name_kz"] if language == "kz" else city["name_ru"],
        }
        for city in cities
    ]
    rows = {row["workflow"]: row for row in session_rows}
    workflows = []
    for loaded in dialogues.load_all():
        if not roles.intersection(loaded.spec.allowed_roles):
            continue
        row = rows.get(loaded.spec.mode.value)
        workflows.append(
            _serialize_session(
                loaded,
                row,
                dict(row["memory"]) if row else {},
                language,
                city_options,
            )
        )
    city_names = [city["name_kz"] if language == "kz" else city["name_ru"] for city in cities]
    federation = bool(roles.intersection({"federation_editor", "reviewer", "superadmin"}))
    representative = bool(roles.intersection({"city_coach", "reviewer", "superadmin"}))
    access = [
        {
            "id": "coach",
            "label": "Тренер",
            "granted": bool(is_coach or roles & {"coach_form", "city_coach", "superadmin"}),
        },
        {
            "id": "player",
            "label": "Игрок",
            "granted": bool(is_player or "player" in roles),
        },
        {
            "id": "city",
            "label": "Представитель города",
            "granted": representative,
            "details": ", ".join(city_names),
        },
        {
            "id": "federation",
            "label": "Представитель федерации",
            "granted": federation,
        },
        {
            "id": "media",
            "label": "Редактор новостей",
            "granted": bool(
                roles & {"media_editor", "city_coach", "reviewer", "superadmin"}
            ),
        },
    ]
    return {
        "user": {
            "name": user["display_name"],
            "language": language,
            "telegram_first_name": request["telegram_user"].get("first_name", ""),
        },
        "access": access,
        "workflows": workflows,
    }


async def bootstrap(request: web.Request) -> web.Response:
    return web.json_response(await _bootstrap_payload(request))


async def start_session(request: web.Request) -> web.Response:
    actor = request["actor"]
    try:
        mode = DialogueMode(request.match_info["mode"])
    except ValueError as exc:
        raise MiniAppError(404, "unknown_workflow", "Такой раздел не найден.") from exc
    loaded = request.app[DIALOGUES].load(mode)
    if not {role.value for role in actor.roles}.intersection(loaded.spec.allowed_roles):
        raise MiniAppError(403, "forbidden", "Этот раздел пока недоступен.")
    async with transaction(request.app[POOL]) as connection:
        row = await connection.fetchrow(
            """
            SELECT id FROM conversation_sessions
            WHERE user_id=$1 AND workflow=$2 AND status='active'
            ORDER BY last_activity_at DESC LIMIT 1 FOR UPDATE
            """,
            actor.user_id,
            mode.value,
        )
        if row is None:
            session_id = await connection.fetchval(
                """
                INSERT INTO conversation_sessions(
                    user_id, workflow, definition_version, definition_hash, current_step
                ) VALUES ($1,$2,$3,$4,$5) RETURNING id
                """,
                actor.user_id,
                mode.value,
                loaded.spec.version,
                loaded.sha256,
                evaluate_gaps(loaded.spec, {}).next_field_id or "ready_to_submit",
            )
            initial_fields = (
                apply_news_defaults({}) if mode == DialogueMode.NEWS else {}
            )
            await connection.execute(
                """
                INSERT INTO conversation_memory(session_id, structured_memory)
                VALUES ($1, $2::jsonb)
                """,
                session_id,
                {"fields": initial_fields, "skipped": []},
            )
    return web.json_response(await _bootstrap_payload(request), status=201)


def _find_editable_field(spec, field_path: str):
    if "." in field_path:
        parent_id, child_id = field_path.split(".", 1)
        parent = next((item for item in spec.fields if item.id == parent_id), None)
        if parent is None or parent.type != FieldType.RECORD or not _editable(parent):
            return None, None
        child = next((item for item in parent.children if item.id == child_id), None)
        return (parent, child) if child and _editable(child) else (None, None)
    field = next((item for item in spec.fields if item.id == field_path), None)
    if field is None or not _editable(field) or field.type == FieldType.RECORD:
        return None, None
    return field, None


async def update_field(request: web.Request) -> web.Response:
    actor = request["actor"]
    session_id = UUID(request.match_info["session_id"])
    field_path = request.match_info["field_path"]
    mutation = FieldMutation.model_validate(await request.json())
    payload_hash = hashlib.sha256(
        mutation.model_dump_json(exclude_none=False).encode()
    ).hexdigest()
    async with transaction(request.app[POOL]) as connection:
        duplicate = await connection.fetchrow(
            "SELECT payload_hash FROM miniapp_mutations WHERE request_id=$1", mutation.request_id
        )
        if duplicate:
            if duplicate["payload_hash"] != payload_hash:
                raise MiniAppError(409, "request_conflict", "Изменение уже было отправлено иначе.")
            return web.json_response(await _bootstrap_payload(request))
        session = await connection.fetchrow(
            """
            SELECT s.workflow, s.status, s.definition_hash, m.structured_memory, m.revision
            FROM conversation_sessions s JOIN conversation_memory m ON m.session_id=s.id
            WHERE s.id=$1 AND s.user_id=$2 FOR UPDATE OF s, m
            """,
            session_id,
            actor.user_id,
        )
        if not session:
            raise MiniAppError(404, "not_found", "Анкета не найдена.")
        if session["status"] != "active":
            raise MiniAppError(409, "read_only", "Отправленную анкету нельзя изменить здесь.")
        if session["revision"] != mutation.revision:
            raise MiniAppError(409, "stale", "Данные изменились. Обновите экран и повторите.")
        loaded = request.app[DIALOGUES].load(session["workflow"])
        if loaded.sha256 != session["definition_hash"]:
            raise MiniAppError(409, "version_changed", "Анкета обновилась. Откройте её заново.")
        parent, child = _find_editable_field(loaded.spec, field_path)
        if parent is None:
            raise MiniAppError(403, "chat_required", "Это поле можно изменить в чате с ботом.")
        fields = dict((session["structured_memory"] or {}).get("fields") or {})
        if child is None:
            if mutation.clear:
                fields.pop(parent.id, None)
            else:
                fields[parent.id] = validate_field_value(parent, mutation.value)
        else:
            record = dict(fields.get(parent.id) or {})
            if mutation.clear:
                record.pop(child.id, None)
            else:
                record[child.id] = validate_field_value(child, mutation.value)
            if record:
                fields[parent.id] = record
            else:
                fields.pop(parent.id, None)
        if loaded.spec.mode == DialogueMode.NEWS:
            fields = apply_news_defaults(fields)
        gaps = evaluate_gaps(loaded.spec, fields)
        memory = dict(session["structured_memory"] or {})
        memory.update(
            {
                "fields": fields,
                "critical_missing": [
                    gap.field_id for gap in (*gaps.required_to_start, *gaps.required_for_submit)
                ],
                "publish_missing": [gap.field_id for gap in gaps.required_for_publish],
                "optional_missing": [gap.field_id for gap in gaps.recommended],
                "last_question_ids": [gaps.next_field_id] if gaps.next_field_id else [],
            }
        )
        new_revision = session["revision"] + 1
        await connection.execute(
            """
            UPDATE conversation_memory
            SET structured_memory=$2::jsonb, revision=$3, updated_at=now()
            WHERE session_id=$1
            """,
            session_id,
            memory,
            new_revision,
        )
        await connection.execute(
            """
            UPDATE conversation_sessions SET current_step=$2, revision=revision+1,
                updated_at=now(), last_activity_at=now() WHERE id=$1
            """,
            session_id,
            gaps.next_field_id or "ready_to_submit",
        )
        await connection.execute(
            """
            INSERT INTO miniapp_mutations(
                request_id,user_id,session_id,action,payload_hash,result_revision
            )
            VALUES ($1,$2,$3,'field_updated',$4,$5)
            """,
            mutation.request_id,
            actor.user_id,
            session_id,
            payload_hash,
            new_revision,
        )
    return web.json_response(await _bootstrap_payload(request))


async def update_language(request: web.Request) -> web.Response:
    mutation = LanguageMutation.model_validate(await request.json())
    await request.app[POOL].execute(
        "UPDATE users SET preferred_language=$2, revision=revision+1, updated_at=now() WHERE id=$1",
        request["actor"].user_id,
        mutation.language,
    )
    return web.json_response(await _bootstrap_payload(request))


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def frontend(request: web.Request) -> web.StreamResponse:
    root = request.app[DIST_ROOT]
    requested = request.match_info.get("path", "")
    candidate = (root / requested).resolve() if requested else root / "index.html"
    if candidate != root and root not in candidate.parents:
        raise web.HTTPNotFound()
    if requested and candidate.is_file():
        response = web.FileResponse(candidate)
    else:
        index = root / "index.html"
        if not index.is_file():
            raise web.HTTPServiceUnavailable(text="Mini App is not built")
        response = web.FileResponse(index)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors https://web.telegram.org "
        "https://*.telegram.org; base-uri 'none'; form-action 'self'"
    )
    return response


def create_miniapp_app(
    pool: asyncpg.Pool,
    *,
    bot_token: str,
    dist_root: Path,
    auth_max_age_seconds: int = 86_400,
) -> web.Application:
    app = web.Application(
        middlewares=[error_middleware, auth_middleware], client_max_size=64 * 1024
    )
    app[POOL] = pool
    app[BOT_TOKEN] = bot_token
    app[AUTH_MAX_AGE] = auth_max_age_seconds
    app[DIST_ROOT] = dist_root.resolve()
    app[DIALOGUES] = DialogueSpecRepository()
    app.router.add_get("/healthz", health)
    app.router.add_get("/api/miniapp/v1/bootstrap", bootstrap)
    app.router.add_post("/api/miniapp/v1/sessions/{mode}", start_session)
    app.router.add_patch(
        "/api/miniapp/v1/sessions/{session_id}/fields/{field_path}", update_field
    )
    app.router.add_patch("/api/miniapp/v1/preferences/language", update_language)
    app.router.add_get("/{path:.*}", frontend)
    return app
