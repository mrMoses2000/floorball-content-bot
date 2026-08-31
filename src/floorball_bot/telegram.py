from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
from pathlib import Path
from uuid import uuid4

import asyncpg
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.methods import DeleteWebhook, GetUpdates, GetWebhookInfo
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, Update

from floorball_bot.auth import (
    bind_self_contact,
    get_actor_by_telegram_id,
    require_active,
    require_roles,
)
from floorball_bot.callbacks import consume_callback, create_callback
from floorball_bot.db import transaction
from floorball_bot.dialogue import DialogueMode, DialogueSpecRepository
from floorball_bot.dialogue.evaluator import evaluate_gaps
from floorball_bot.domain import Actor, DraftStatus, Role
from floorball_bot.errors import AuthorizationError
from floorball_bot.health import record_heartbeat
from floorball_bot.queue import accept_update, enqueue_job, enqueue_outbox, stable_idempotency_key
from floorball_bot.workflow import add_revision, transition_draft

logger = logging.getLogger(__name__)

DIALOGUE_WORKFLOWS = tuple(mode.value for mode in DialogueMode)


ROLE_COMMANDS: dict[str, tuple[Role, ...]] = {
    "/review": (Role.REVIEWER, Role.SUPERADMIN),
    "/publish": (Role.SUPERADMIN,),
    "/users": (Role.SUPERADMIN,),
    "/revert": (Role.SUPERADMIN,),
    "/readiness": (Role.SUPERADMIN,),
    "/city": (Role.CITY_COACH, Role.REVIEWER, Role.SUPERADMIN),
    "/players": (Role.CITY_COACH, Role.REVIEWER, Role.SUPERADMIN),
    "/gallery": (Role.CITY_COACH, Role.MEDIA_EDITOR, Role.REVIEWER, Role.SUPERADMIN),
}

COACH_CRITICAL_STEPS = (
    "full_name",
    "city_region",
    "club",
    "experience",
    "contact_preference",
)
COACH_OPTIONAL_STEPS = (
    "qualification",
    "age_groups",
    "availability",
    "public_contact_consent",
)
COACH_FIELD_LABELS = {
    "full_name": "ФИО",
    "city_region": "город и область",
    "club": "клуб или организация",
    "experience": "тренерский опыт",
    "contact_preference": "предпочтительный способ связи",
    "qualification": "квалификация",
    "age_groups": "возрастные группы",
    "availability": "доступность или расписание",
    "public_contact_consent": "согласие на публичный контакт",
}
COACH_QUESTIONS = {
    "full_name": "Как к вам обращаться? Укажите ФИО.",
    "city_region": "В каком городе и области вы работаете?",
    "club": "С каким клубом или организацией вы связаны? Если клуба нет, напишите «нет клуба».",
    "experience": "Расскажите кратко о тренерском опыте или статусе.",
    "contact_preference": "Какой способ связи для вас удобнее: Telegram, звонок или e-mail?",
    "qualification": "Можно добавить квалификацию, сертификаты или образование.",
    "age_groups": "Можно добавить возрастные группы, с которыми вы работаете.",
    "availability": "Можно добавить доступность, расписание или площадку.",
    "public_contact_consent": (
        "Можно указать, разрешаете ли публиковать рабочий контакт. "
        "По умолчанию он остаётся закрытым."
    ),
}
COACH_ALIASES = {
    "фио": "full_name",
    "имя": "full_name",
    "город": "city_region",
    "область": "city_region",
    "регион": "city_region",
    "клуб": "club",
    "организация": "club",
    "стаж": "experience",
    "опыт": "experience",
    "статус": "experience",
    "связь": "contact_preference",
    "контакт": "contact_preference",
    "квалификация": "qualification",
    "сертификаты": "qualification",
    "возрастные группы": "age_groups",
    "группы": "age_groups",
    "расписание": "availability",
    "доступность": "availability",
    "согласие": "public_contact_consent",
    "публичный контакт": "public_contact_consent",
}


class TelegramIngress:
    def __init__(
        self,
        bot: Bot,
        pool: asyncpg.Pool,
        *,
        poll_timeout: int = 30,
        download_root: Path = Path("./var/media/incoming"),
        max_download_bytes: int = 20 * 1024 * 1024,
    ) -> None:
        self.bot = bot
        self.pool = pool
        self.poll_timeout = poll_timeout
        self.download_root = download_root.resolve()
        self.max_download_bytes = max_download_bytes
        self.dialogues = DialogueSpecRepository()
        self.offset: int | None = None
        self._stop = asyncio.Event()

    async def prepare_long_polling(self) -> None:
        info = await self.bot(GetWebhookInfo())
        if info.url:
            await self.bot(DeleteWebhook(drop_pending_updates=False))
            logger.warning("telegram_webhook_removed_for_long_polling")

    async def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        await self.prepare_long_polling()
        await record_heartbeat(self.pool, "telegram_ingress", {"state": "started"})
        retry_attempt = 0
        while not self._stop.is_set():
            try:
                updates = await self.bot(
                    GetUpdates(
                        offset=self.offset,
                        timeout=self.poll_timeout,
                        allowed_updates=["message", "edited_message", "callback_query"],
                    )
                )
                retry_attempt = 0
            except TelegramRetryAfter as exc:
                retry_attempt += 1
                await self._wait_polling_retry(float(exc.retry_after), type(exc).__name__)
                continue
            except (TelegramNetworkError, TelegramServerError) as exc:
                retry_attempt += 1
                delay = min(30.0, 2 ** min(retry_attempt, 5))
                delay += random.uniform(0, 1)  # noqa: S311 - retry jitter, not security
                await self._wait_polling_retry(delay, type(exc).__name__)
                continue
            await record_heartbeat(
                self.pool,
                "telegram_ingress",
                {"state": "polling", "updates_received": len(updates)},
            )
            for update in updates:
                await self.accept(update)
                self.offset = update.update_id + 1

    async def _wait_polling_retry(self, delay: float, error_class: str) -> None:
        logger.warning(
            "telegram_poll_retry",
            extra={"error": error_class, "delay_seconds": round(delay, 2)},
        )
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(0.1, delay))
        except TimeoutError:
            pass

    async def accept(self, update: Update) -> bool:
        async with transaction(self.pool) as connection:
            if not await accept_update(connection, update.update_id):
                return False
            await self._route(connection, update)
            await connection.execute(
                """
                UPDATE processed_updates
                SET status='completed', completed_at=now(), error_class=''
                WHERE update_id=$1
                """,
                update.update_id,
            )
        return True

    async def _route(self, connection: asyncpg.Connection, update: Update) -> None:
        message = update.edited_message or update.message
        if update.callback_query:
            actor = require_active(
                await get_actor_by_telegram_id(connection, update.callback_query.from_user.id)
            )
            callback_markup = None
            try:
                action, target_id = await consume_callback(
                    connection,
                    actor=actor,
                    callback_data=update.callback_query.data or "",
                )
                if action == "review_start":
                    require_roles(actor, Role.REVIEWER, Role.SUPERADMIN)
                    draft = await connection.fetchrow(
                        """
                        SELECT d.status, d.current_revision, r.content, r.content_hash
                        FROM drafts d
                        JOIN draft_revisions r
                          ON r.draft_id=d.id AND r.revision=d.current_revision
                        WHERE d.id=$1 FOR UPDATE OF d
                        """,
                        target_id,
                    )
                    if not draft or draft["status"] not in {"submitted", "under_review"}:
                        raise AuthorizationError("draft is no longer reviewable")
                    if draft["status"] == "submitted":
                        await transition_draft(
                            connection,
                            draft_id=target_id,
                            actor=actor,
                            target=DraftStatus.UNDER_REVIEW,
                        )
                    approve = await create_callback(
                        connection,
                        actor=actor,
                        action="review_approve",
                        target_id=target_id,
                        ttl_seconds=7 * 24 * 60 * 60,
                    )
                    changes = await create_callback(
                        connection,
                        actor=actor,
                        action="review_changes",
                        target_id=target_id,
                        ttl_seconds=7 * 24 * 60 * 60,
                    )
                    callback_text = self._trainer_review_summary(
                        draft["content"], draft["current_revision"], draft["content_hash"]
                    )
                    callback_markup = {
                        "inline_keyboard": [[
                            {
                                "text": "Одобрить данные",
                                "callback_data": approve.callback_data,
                            },
                            {
                                "text": "Нужны изменения",
                                "callback_data": changes.callback_data,
                            },
                        ]]
                    }
                elif action == "review_approve":
                    require_roles(actor, Role.REVIEWER, Role.SUPERADMIN)
                    await transition_draft(
                        connection,
                        draft_id=target_id,
                        actor=actor,
                        target=DraftStatus.APPROVED,
                        reason="Approved from Telegram review",
                    )
                    revision = await connection.fetchval(
                        "SELECT approved_revision FROM drafts WHERE id=$1", target_id
                    )
                    await enqueue_job(
                        connection,
                        kind="apply_projection",
                        payload={
                            "draft_id": str(target_id),
                            "actor_id": str(actor.user_id),
                            "chat_id": update.callback_query.from_user.id,
                        },
                        idempotency_key=stable_idempotency_key(
                            "apply-projection", target_id, revision
                        ),
                    )
                    callback_text = (
                        "Ревизия одобрена. Переношу разрешённые данные в основную БД. "
                        "После проверки готовности бот отдельно предложит собрать preview."
                    )
                elif action == "review_changes":
                    require_roles(actor, Role.REVIEWER, Role.SUPERADMIN)
                    await transition_draft(
                        connection,
                        draft_id=target_id,
                        actor=actor,
                        target=DraftStatus.CHANGES_REQUESTED,
                        reason="Changes requested from Telegram review",
                    )
                    revision = await connection.fetchval(
                        "SELECT current_revision FROM drafts WHERE id=$1", target_id
                    )
                    author = await connection.fetchrow(
                        """
                        UPDATE conversation_sessions s
                        SET status='active', current_step='changes_requested',
                            updated_at=now(), last_activity_at=now()
                        FROM drafts d, users u
                        WHERE d.id=$1 AND s.id=d.session_id AND u.id=s.user_id
                        RETURNING u.telegram_id
                        """,
                        target_id,
                    )
                    if author and author["telegram_id"] is not None:
                        await enqueue_outbox(
                            connection,
                            event_type="telegram_message",
                            payload={
                                "chat_id": author["telegram_id"],
                                "text": (
                                    "Редактор запросил изменения в анкете. Ответы сохранены; "
                                    "используйте /resume, дополните их и снова отправьте /submit."
                                ),
                            },
                            idempotency_key=stable_idempotency_key(
                                "review-changes-author", target_id, revision
                            ),
                        )
                    callback_text = "Запрос изменений отправлен автору; прежнее одобрение снято."
                elif action == "approve_preview":
                    require_roles(actor, Role.SUPERADMIN)
                    draft = await connection.fetchrow(
                        """
                        SELECT status, current_revision FROM drafts
                        WHERE id=$1 FOR UPDATE
                        """,
                        target_id,
                    )
                    if not draft or draft["status"] not in {"under_review", "approved"}:
                        raise AuthorizationError("snapshot is no longer reviewable")
                    if draft["status"] == "under_review":
                        await connection.execute(
                            """
                            UPDATE drafts SET status='approved', approved_revision=current_revision,
                                updated_by=$2, updated_at=now()
                            WHERE id=$1
                            """,
                            target_id,
                            actor.user_id,
                        )
                        await connection.execute(
                            """
                            INSERT INTO approval_events(
                                draft_id, revision, actor_id, action, reason
                            ) VALUES ($1,$2,$3,'approved','Approved from readiness notification')
                            """,
                            target_id,
                            draft["current_revision"],
                            actor.user_id,
                        )
                    await enqueue_job(
                        connection,
                        kind="publish_preview",
                        payload={
                            "draft_id": str(target_id),
                            "actor_id": str(actor.user_id),
                            "chat_id": update.callback_query.from_user.id,
                        },
                        idempotency_key=stable_idempotency_key(
                            "publish-preview",
                            target_id,
                            draft["current_revision"],
                            update.update_id,
                        ),
                    )
                    callback_text = (
                        "Одобрение принято. Собираю preview, запускаю тесты сайта и покажу diff. "
                        "Commit/push пока не выполняются."
                    )
                elif action.startswith("confirm_publish|"):
                    require_roles(actor, Role.SUPERADMIN)
                    preview_nonce = action.partition("|")[2]
                    if not preview_nonce:
                        raise AuthorizationError("publication nonce is missing")
                    await enqueue_job(
                        connection,
                        kind="publish_confirm",
                        payload={
                            "publication_id": str(target_id),
                            "nonce": preview_nonce,
                            "actor_id": str(actor.user_id),
                            "chat_id": update.callback_query.from_user.id,
                        },
                        idempotency_key=stable_idempotency_key(
                            "publish-confirm", target_id, actor.user_id, update.update_id
                        ),
                    )
                    callback_text = (
                        "Добро принято для конкретного preview. Выполняю commit, push и "
                        "проверку удалённых веток."
                    )
                else:
                    raise AuthorizationError("unsupported callback action")
            except (AuthorizationError, ValueError):
                callback_text = (
                    "Кнопка недействительна, устарела или принадлежит другому пользователю."
                )
            await enqueue_outbox(
                connection,
                event_type="telegram_message",
                payload={
                    "chat_id": update.callback_query.from_user.id,
                    "text": callback_text,
                    "reply_markup": callback_markup,
                },
                idempotency_key=stable_idempotency_key(
                    "callback-ack", update.update_id, actor.user_id
                ),
            )
            return
        if message is None or message.from_user is None:
            return
        sender_id = message.from_user.id
        actor = await get_actor_by_telegram_id(connection, sender_id)
        if message.contact:
            try:
                actor = await bind_self_contact(
                    connection,
                    sender_id=sender_id,
                    contact_user_id=message.contact.user_id,
                    raw_phone=message.contact.phone_number,
                )
                text = "Авторизация завершена. Используйте /profile и /help."
            except (AuthorizationError, ValueError):
                text = "Контакт не совпал с заранее разрешённой активной записью."
            await self._reply(connection, update.update_id, message.chat.id, text)
            return
        if message.text and message.text.startswith("/start"):
            if actor and actor.active:
                keyboard = self._dialogue_keyboard(actor)
                await self._reply(
                    connection,
                    update.update_id,
                    message.chat.id,
                    "Вы авторизованы. Выберите, о чём продолжить разговор.",
                    reply_markup=keyboard,
                )
            else:
                keyboard = ReplyKeyboardMarkup(
                    keyboard=[
                        [KeyboardButton(text="Поделиться своим контактом", request_contact=True)]
                    ],
                    resize_keyboard=True,
                    one_time_keyboard=True,
                )
                await self._reply(
                    connection,
                    update.update_id,
                    message.chat.id,
                    "Для входа поделитесь своим контактом кнопкой ниже. "
                    "Текстовый номер не принимается.",
                    reply_markup=keyboard.model_dump(exclude_none=True),
                )
            return
        try:
            actor = require_active(actor)
        except AuthorizationError:
            await self._reply(
                connection,
                update.update_id,
                message.chat.id,
                "Доступ запрещён. Используйте /start для авторизации.",
            )
            return

        selected_mode = self._selected_dialogue_mode(message.text or "", actor)
        if selected_mode is not None:
            await self._start_dialogue(
                connection,
                actor,
                selected_mode,
                update.update_id,
                message.chat.id,
            )
            return

        dialogue_session = await connection.fetchrow(
            """
            SELECT id, workflow, status, definition_version, definition_hash,
                   context_hash, current_step
            FROM conversation_sessions
            WHERE user_id=$1 AND workflow=ANY($2::text[]) AND status='active'
            ORDER BY last_activity_at DESC LIMIT 1
            """,
            actor.user_id,
            list(DIALOGUE_WORKFLOWS),
        )
        if actor.roles == frozenset({Role.COACH_FORM}) and dialogue_session is None:
            await self._reply(
                connection,
                update.update_id,
                message.chat.id,
                "Вам доступна анкета тренера. Нажмите «Продолжить как тренер» или отправьте "
                "/coach-form.",
                reply_markup=self._dialogue_keyboard(actor),
            )
            return
        needs_dialogue = bool(message.voice or message.audio) or bool(
            message.text and not message.text.startswith("/")
        )
        if dialogue_session is None and needs_dialogue:
            await self._reply(
                connection,
                update.update_id,
                message.chat.id,
                "Сначала выберите, в каком качестве продолжить разговор. Это нужно, чтобы "
                "вопросы и сохранение данных соответствовали выбранному разделу.",
                reply_markup=self._dialogue_keyboard(actor),
            )
            return
        await connection.execute(
            """
            INSERT INTO messages(
                session_id, user_id, telegram_chat_id, telegram_message_id,
                telegram_update_id, direction, message_type, original_text
            )
            VALUES ($1,$2,$3,$4,$5,'inbound',$6,$7)
            """,
            dialogue_session["id"] if dialogue_session else None,
            actor.user_id,
            message.chat.id,
            message.message_id,
            update.update_id,
            self._message_type(message),
            message.text or message.caption or "",
        )
        if message.text and message.text.startswith("/"):
            if dialogue_session and await self._handle_dialogue_command(
                connection,
                actor,
                dialogue_session,
                update.update_id,
                message.chat.id,
                message.text,
            ):
                return
            await self._handle_command(
                connection, actor, update.update_id, message.chat.id, message.text
            )
        elif message.voice or message.audio:
            preferred_language = await connection.fetchval(
                "SELECT preferred_language FROM users WHERE id=$1", actor.user_id
            )
            media_path = await self._download_file(
                message.voice or message.audio,
                ".ogg" if message.voice else ".audio",
            )
            await enqueue_job(
                connection,
                kind="transcribe",
                payload={
                    "path": str(media_path),
                    "chat_id": message.chat.id,
                    "language": preferred_language or "ru",
                    "session_id": str(dialogue_session["id"]) if dialogue_session else None,
                    "mode": dialogue_session["workflow"] if dialogue_session else None,
                },
                idempotency_key=stable_idempotency_key("transcribe", update.update_id),
            )
            await self._reply(
                connection,
                update.update_id,
                message.chat.id,
                "Голосовое сообщение принято. После распознавания "
                "я покажу текст для подтверждения.",
            )
        elif message.photo or (
            message.document and (message.document.mime_type or "").startswith("image/")
        ):
            media = message.photo[-1] if message.photo else message.document
            media_path = await self._download_file(media, ".image")
            await enqueue_job(
                connection,
                kind="media",
                payload={
                    "path": str(media_path),
                    "chat_id": message.chat.id,
                    "user_id": str(actor.user_id),
                    "filename": getattr(media, "file_name", None) or "telegram-image",
                },
                idempotency_key=stable_idempotency_key("media", update.update_id),
            )
            await self._reply(
                connection,
                update.update_id,
                message.chat.id,
                "Изображение принято в закрытое хранилище и отправлено на проверку.",
            )
        elif message.text:
            pending_transcript = await connection.fetchrow(
                """
                SELECT m.id, m.normalized_text, m.session_id, s.workflow
                FROM messages
                AS m
                LEFT JOIN conversation_sessions s ON s.id=m.session_id
                WHERE m.user_id=$1 AND m.telegram_chat_id=$2
                  AND m.message_type IN ('voice','audio')
                  AND m.transcript_confirmed=FALSE
                  AND m.normalized_text <> ''
                ORDER BY m.created_at DESC
                LIMIT 1
                FOR UPDATE OF m
                """,
                actor.user_id,
                message.chat.id,
            )
            if pending_transcript:
                confirmations = {
                    "подтверждаю",
                    "подтвердить",
                    "верно",
                    "да",
                    "растаймын",
                    "дұрыс",
                    "иә",
                }
                supplied_text = message.text.strip()
                transcript_text = (
                    pending_transcript["normalized_text"]
                    if supplied_text.casefold() in confirmations
                    else supplied_text
                )
                await connection.execute(
                    """
                    UPDATE messages
                    SET normalized_text=$2, transcript_confirmed=TRUE
                    WHERE id=$1
                    """,
                    pending_transcript["id"],
                    transcript_text,
                )
                await enqueue_job(
                    connection,
                    kind="extract",
                    payload={
                        "text": transcript_text,
                        "chat_id": message.chat.id,
                        "user_id": str(actor.user_id),
                        "session_id": str(pending_transcript["session_id"])
                        if pending_transcript["session_id"]
                        else None,
                        "mode": pending_transcript["workflow"],
                    },
                    idempotency_key=stable_idempotency_key(
                        "confirmed-transcript", pending_transcript["id"]
                    ),
                )
                await self._reply(
                    connection,
                    update.update_id,
                    message.chat.id,
                    "Расшифровка подтверждена и добавлена в черновик.",
                )
                return
            await enqueue_job(
                connection,
                kind="extract",
                payload={
                    "text": message.text,
                    "chat_id": message.chat.id,
                    "user_id": str(actor.user_id),
                    "session_id": str(dialogue_session["id"]) if dialogue_session else None,
                    "mode": dialogue_session["workflow"] if dialogue_session else None,
                },
                idempotency_key=stable_idempotency_key("extract", update.update_id),
            )
            await self._reply(
                connection, update.update_id, message.chat.id, "Сообщение добавлено в черновик."
            )
        else:
            await self._reply(
                connection,
                update.update_id,
                message.chat.id,
                "Этот тип сообщения пока нельзя добавить в черновик.",
            )

    async def _handle_coach_form(
        self,
        connection: asyncpg.Connection,
        actor: Actor,
        update_id: int,
        message,
    ) -> None:
        """Run the narrow, resumable coach questionnaire without content privileges."""
        if not message.text:
            await self._reply(
                connection,
                update_id,
                message.chat.id,
                "Анкета тренера принимает только текст. Используйте /coach-form.",
            )
            return
        command = message.text.split()[0].split("@")[0] if message.text.startswith("/") else ""
        session = await connection.fetchrow(
            """
            SELECT id, status, current_step FROM conversation_sessions
            WHERE user_id=$1 AND workflow='coach_form' AND status='active'
            ORDER BY created_at DESC
            LIMIT 1
            FOR UPDATE
            """,
            actor.user_id,
        )
        if command == "/coach-form":
            created = session is None
            if created:
                session_id = await connection.fetchval(
                    """
                    INSERT INTO conversation_sessions(user_id, workflow, current_step)
                    VALUES ($1, 'coach_form', 'full_name')
                    RETURNING id
                    """,
                    actor.user_id,
                )
                await self._save_coach_memory(connection, session_id, {}, set())
                session = {"id": session_id, "current_step": "full_name"}
            fields, skipped = await self._coach_memory(connection, session["id"])
            response = self._coach_progress_text(fields, skipped)
            if created:
                response = (
                    "Начнём анкету тренера. Сначала нужны критически важные данные; "
                    "дополнительные поля можно пропустить и заполнить позже.\n\n" + response
                )
            await self._reply(connection, update_id, message.chat.id, response)
            return
        if command == "/cancel":
            await connection.execute(
                """
                UPDATE conversation_sessions
                SET status='cancelled', updated_at=now(), last_activity_at=now()
                WHERE user_id=$1 AND workflow='coach_form' AND status='active'
                """,
                actor.user_id,
            )
            await self._reply(connection, update_id, message.chat.id, "Анкета отменена.")
            return
        if command in {"/status", "/resume"}:
            if session is None:
                response = "Незавершённой анкеты нет. Используйте /coach-form."
            else:
                fields, skipped = await self._coach_memory(connection, session["id"])
                response = self._coach_progress_text(fields, skipped)
            await self._reply(connection, update_id, message.chat.id, response)
            return
        if command == "/submit":
            if session is None:
                await self._reply(
                    connection, update_id, message.chat.id, "Нет анкеты для отправки."
                )
                return
            fields, skipped = await self._coach_memory(connection, session["id"])
            missing = [field for field in COACH_CRITICAL_STEPS if not fields.get(field)]
            if missing:
                await self._reply(
                    connection,
                    update_id,
                    message.chat.id,
                    self._coach_progress_text(fields, skipped),
                )
                return
            await connection.execute(
                """
                UPDATE conversation_sessions
                SET current_step='submitted', status='completed', updated_at=now(),
                    last_activity_at=now()
                WHERE id=$1
                """,
                session["id"],
            )
            await self._reply(
                connection,
                update_id,
                message.chat.id,
                "Анкета отправлена на рассмотрение. Она не публикуется автоматически.",
            )
            return
        if command and command != "/skip":
            await self._reply(
                connection,
                update_id,
                message.chat.id,
                "Доступна только анкета тренера: /coach-form.",
            )
            return
        if session is None:
            await self._reply(
                connection,
                update_id,
                message.chat.id,
                "Сначала откройте анкету командой /coach-form.",
            )
            return
        fields, skipped = await self._coach_memory(connection, session["id"])
        current_step = self._next_coach_step(fields, skipped)
        if message.text.strip().casefold() in {"/skip", "пропустить", "өткізу"}:
            if current_step not in COACH_OPTIONAL_STEPS:
                await self._reply(
                    connection,
                    update_id,
                    message.chat.id,
                    "Сейчас пропустить нельзя: сначала нужны критически важные данные.\n\n"
                    + self._coach_progress_text(fields, skipped),
                )
                return
            skipped.add(current_step)
        else:
            provided = self._coach_labeled_fields(message.text)
            if not provided and current_step:
                provided = {current_step: message.text.strip()}
            for field, value in provided.items():
                if value and len(value) <= 500:
                    fields[field] = value
        await connection.execute(
            """
            INSERT INTO messages(
                session_id, user_id, telegram_chat_id, telegram_message_id, telegram_update_id,
                direction, message_type, original_text
            ) VALUES ($1,$2,$3,$4,$5,'inbound','text',$6)
            """,
            session["id"],
            actor.user_id,
            message.chat.id,
            message.message_id,
            update_id,
            message.text,
        )
        next_step = self._next_coach_step(fields, skipped)
        await self._save_coach_memory(connection, session["id"], fields, skipped)
        await connection.execute(
            """
            UPDATE conversation_sessions
            SET current_step=$2, updated_at=now(), last_activity_at=now()
            WHERE id=$1
            """,
            session["id"],
            next_step or "ready_to_submit",
        )
        await self._reply(
            connection,
            update_id,
            message.chat.id,
            self._coach_progress_text(fields, skipped),
        )

    def _available_dialogues(self, actor: Actor):
        roles = {role.value for role in actor.roles}
        return tuple(
            loaded
            for loaded in self.dialogues.load_all()
            if roles.intersection(loaded.spec.allowed_roles)
        )

    def _dialogue_keyboard(self, actor: Actor) -> dict | None:
        available = self._available_dialogues(actor)
        if not available:
            return None
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text=loaded.spec.ui_label.ru)] for loaded in available
            ],
            resize_keyboard=True,
        )
        return keyboard.model_dump(exclude_none=True)

    def _selected_dialogue_mode(self, text: str, actor: Actor) -> DialogueMode | None:
        normalized = text.strip().casefold()
        if normalized.split("@", 1)[0] == "/coach-form":
            requested = DialogueMode.TRAINER
            return requested if any(
                loaded.spec.mode == requested for loaded in self._available_dialogues(actor)
            ) else None
        for loaded in self._available_dialogues(actor):
            if normalized in {
                loaded.spec.ui_label.ru.casefold(),
                loaded.spec.ui_label.kz.casefold(),
            }:
                return loaded.spec.mode
        return None

    async def _start_dialogue(
        self,
        connection: asyncpg.Connection,
        actor: Actor,
        mode: DialogueMode,
        update_id: int,
        chat_id: int,
    ) -> None:
        loaded = self.dialogues.load(mode)
        if not {role.value for role in actor.roles}.intersection(loaded.spec.allowed_roles):
            await self._reply(connection, update_id, chat_id, "Этот вариант вам недоступен.")
            return
        session = await connection.fetchrow(
            """
            SELECT id FROM conversation_sessions
            WHERE user_id=$1 AND workflow=$2 AND status='active'
            ORDER BY last_activity_at DESC LIMIT 1 FOR UPDATE
            """,
            actor.user_id,
            mode.value,
        )
        if session is None:
            await connection.execute(
                """
                UPDATE conversation_sessions SET status='paused', updated_at=now()
                WHERE user_id=$1 AND workflow=ANY($2::text[]) AND status='active'
                """,
                actor.user_id,
                list(DIALOGUE_WORKFLOWS),
            )
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
            await connection.execute(
                """
                INSERT INTO conversation_memory(session_id, structured_memory)
                VALUES ($1, '{"fields":{},"skipped":[]}'::jsonb)
                """,
                session_id,
            )
            fields: dict = {}
            intro = f"Начинаем: {loaded.spec.ui_label.ru.lower()}.\n\n"
        else:
            memory = await connection.fetchval(
                "SELECT structured_memory FROM conversation_memory WHERE session_id=$1",
                session["id"],
            ) or {}
            fields = dict(memory.get("fields", {}))
            intro = "Продолжаем с сохранённого места.\n\n"
        language = await connection.fetchval(
            "SELECT preferred_language FROM users WHERE id=$1", actor.user_id
        )
        await self._reply(
            connection,
            update_id,
            chat_id,
            intro + self._dialogue_progress(loaded.spec, fields, language or "ru"),
            reply_markup=self._dialogue_keyboard(actor),
        )

    @staticmethod
    def _dialogue_progress(spec, fields: dict, language: str) -> str:
        gaps = evaluate_gaps(spec, fields)
        critical = len(gaps.required_to_start) + len(gaps.required_for_submit)
        later = len(gaps.required_for_publish) + len(gaps.recommended)
        lines = [
            f"Заполнено разделов: {len(fields)}.",
            "Критично до отправки: " + (str(critical) if critical else "всё заполнено"),
            "Можно дозаполнить позже: " + (str(later) if later else "нет"),
        ]
        ordered = (
            *gaps.required_to_start,
            *gaps.required_for_submit,
            *gaps.required_for_publish,
            *gaps.recommended,
        )
        if ordered:
            question = ordered[0].question.kz if language == "kz" else ordered[0].question.ru
            prefix = "Важно до отправки. " if critical else "Можно добавить сейчас. "
            lines.append(prefix + question)
        if gaps.can_submit:
            lines.append("Черновик уже можно отправить командой /submit.")
        return "\n\n".join(lines)

    async def _handle_dialogue_command(
        self,
        connection: asyncpg.Connection,
        actor: Actor,
        session,
        update_id: int,
        chat_id: int,
        text: str,
    ) -> bool:
        command = text.split()[0].split("@")[0]
        if command not in {"/status", "/resume", "/cancel", "/submit", "/skip"}:
            return False
        if command == "/cancel":
            await connection.execute(
                """
                UPDATE conversation_sessions
                SET status='cancelled', updated_at=now(), last_activity_at=now()
                WHERE id=$1
                """,
                session["id"],
            )
            await self._reply(connection, update_id, chat_id, "Разговор отменён.")
            return True
        loaded = self.dialogues.load(session["workflow"])
        if loaded.sha256 != session["definition_hash"]:
            await self._reply(
                connection,
                update_id,
                chat_id,
                "Определение вопросов обновилось. Сохранённые ответы не потеряны; "
                "администратор должен перенести сессию на новую версию.",
            )
            return True
        memory = await connection.fetchval(
            "SELECT structured_memory FROM conversation_memory WHERE session_id=$1",
            session["id"],
        ) or {}
        fields = dict(memory.get("fields", {}))
        language = await connection.fetchval(
            "SELECT preferred_language FROM users WHERE id=$1", actor.user_id
        ) or "ru"
        if command in {"/status", "/resume", "/skip"}:
            await self._reply(
                connection,
                update_id,
                chat_id,
                self._dialogue_progress(loaded.spec, fields, language),
            )
            return True
        gaps = evaluate_gaps(loaded.spec, fields)
        if not gaps.can_submit:
            await self._reply(
                connection,
                update_id,
                chat_id,
                "Пока не хватает критически важных ответов.\n\n"
                + self._dialogue_progress(loaded.spec, fields, language),
            )
            return True
        content = {
            "dialogue_mode": session["workflow"],
            "definition_version": session["definition_version"],
            "definition_hash": session["definition_hash"],
            "context_hash": session["context_hash"],
            "fields": fields,
        }
        canonical = json.dumps(
            content, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        entity_type = (
            "city"
            if session["workflow"] == DialogueMode.TRAINER
            else "leadership"
            if session["workflow"] == DialogueMode.LEADERSHIP
            else "federation"
        )
        existing = await connection.fetchrow(
            """
            SELECT id, status FROM drafts
            WHERE session_id=$1 AND status='changes_requested'
            ORDER BY updated_at DESC LIMIT 1 FOR UPDATE
            """,
            session["id"],
        )
        if existing:
            draft_id = existing["id"]
            await add_revision(
                connection,
                draft_id=draft_id,
                actor=actor,
                content=content,
            )
            await connection.execute(
                """
                UPDATE callback_actions SET consumed_at=now()
                WHERE target_id=$1 AND consumed_at IS NULL
                """,
                draft_id,
            )
            await transition_draft(
                connection,
                draft_id=draft_id,
                actor=actor,
                target=DraftStatus.SUBMITTED,
                reason="Corrected dialogue revision submitted",
            )
        else:
            draft_id = await connection.fetchval(
                """
                INSERT INTO drafts(
                    session_id, entity_type, status, created_by, updated_by
                ) VALUES ($1,$2,'submitted',$3,$3) RETURNING id
                """,
                session["id"],
                entity_type,
                actor.user_id,
            )
            await connection.execute(
                """
                INSERT INTO draft_revisions(
                    draft_id, revision, content, content_hash, created_by
                ) VALUES ($1,1,$2::jsonb,$3,$4)
                """,
                draft_id,
                content,
                hashlib.sha256(canonical.encode()).hexdigest(),
                actor.user_id,
            )
            await connection.execute(
                """
                INSERT INTO approval_events(draft_id, revision, actor_id, action)
                VALUES ($1,1,$2,'submitted')
                """,
                draft_id,
                actor.user_id,
            )
        await connection.execute(
            """
            UPDATE conversation_sessions
            SET status='completed', current_step='submitted', updated_at=now(),
                last_activity_at=now()
            WHERE id=$1
            """,
            session["id"],
        )
        await self._reply(
            connection,
            update_id,
            chat_id,
            "Черновик отправлен на редакторскую проверку. Автоматической публикации нет.",
        )
        return True

    @staticmethod
    def _coach_labeled_fields(text: str) -> dict[str, str]:
        provided: dict[str, str] = {}
        for part in re.split(r"[\n;]+", text):
            label, separator, value = part.partition(":")
            if not separator:
                continue
            field = COACH_ALIASES.get(label.strip().casefold())
            if field and value.strip():
                provided[field] = value.strip()
        return provided

    @staticmethod
    def _next_coach_step(fields: dict[str, str], skipped: set[str]) -> str | None:
        for field in (*COACH_CRITICAL_STEPS, *COACH_OPTIONAL_STEPS):
            if not fields.get(field) and field not in skipped:
                return field
        return None

    async def _coach_memory(
        self, connection: asyncpg.Connection, session_id
    ) -> tuple[dict[str, str], set[str]]:
        memory = await connection.fetchval(
            "SELECT structured_memory FROM conversation_memory WHERE session_id=$1", session_id
        ) or {}
        fields = {
            field: str(value)
            for field, value in dict(memory.get("fields", {})).items()
            if field in COACH_FIELD_LABELS and value
        }
        skipped = {field for field in memory.get("skipped", []) if field in COACH_OPTIONAL_STEPS}
        return fields, skipped

    async def _save_coach_memory(
        self,
        connection: asyncpg.Connection,
        session_id,
        fields: dict[str, str],
        skipped: set[str],
    ) -> None:
        memory = {"fields": fields, "skipped": sorted(skipped)}
        await connection.execute(
            """
            INSERT INTO conversation_memory(session_id, structured_memory)
            VALUES ($1, $2::jsonb)
            ON CONFLICT (session_id) DO UPDATE
            SET structured_memory=EXCLUDED.structured_memory,
                revision=conversation_memory.revision+1, updated_at=now()
            """,
            session_id,
            memory,
        )

    @staticmethod
    def _coach_progress_text(fields: dict[str, str], skipped: set[str]) -> str:
        completed = [
            COACH_FIELD_LABELS[field] for field in COACH_CRITICAL_STEPS if fields.get(field)
        ]
        missing = [
            COACH_FIELD_LABELS[field] for field in COACH_CRITICAL_STEPS if not fields.get(field)
        ]
        optional = [
            COACH_FIELD_LABELS[field]
            for field in COACH_OPTIONAL_STEPS
            if not fields.get(field) and field not in skipped
        ]
        lines = [
            "Заполнено: " + (", ".join(completed) if completed else "пока нет"),
            "Критично до отправки: " + (", ".join(missing) if missing else "всё заполнено"),
            "Можно заполнить позже: " + (", ".join(optional) if optional else "нет"),
        ]
        next_step = TelegramIngress._next_coach_step(fields, skipped)
        if next_step:
            prefix = "Критично. " if next_step in COACH_CRITICAL_STEPS else "Необязательно. "
            suffix = "" if next_step in COACH_CRITICAL_STEPS else " Можно написать /skip."
            lines.append(prefix + COACH_QUESTIONS[next_step] + suffix)
        else:
            lines.append("Проверьте данные и подтвердите отправку командой /submit.")
        return "\n\n".join(lines)

    async def _download_file(self, media, suffix: str) -> Path:
        self.download_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.download_root / f"{uuid4()}{suffix}"
        file = await self.bot.get_file(media.file_id)
        await self.bot.download_file(file.file_path, destination=path)
        path.chmod(0o600)
        if path.stat().st_size > self.max_download_bytes:
            path.unlink(missing_ok=True)
            raise ValueError("Telegram file exceeds maximum size")
        return path

    async def _handle_command(
        self,
        connection: asyncpg.Connection,
        actor: Actor,
        update_id: int,
        chat_id: int,
        text: str,
    ) -> None:
        command = text.split()[0].split("@")[0]
        allowed = ROLE_COMMANDS.get(command)
        if allowed:
            try:
                require_roles(actor, *allowed)
            except AuthorizationError:
                await self._reply(
                    connection, update_id, chat_id, "Недостаточно прав для этой команды."
                )
                return
        if command == "/profile":
            roles = ", ".join(sorted(role.value for role in actor.roles)) or "нет"
            response = f"Роли: {roles}. Назначенных городов: {len(actor.city_scopes)}."
        elif command == "/help":
            response = (
                "Команды: /status /resume /cancel /profile /city /players /gallery "
                "/submit /history. Проверяющим: /review. Администратору: "
                "/readiness /publish /users /revert."
            )
        elif command == "/readiness":
            await enqueue_job(
                connection,
                kind="readiness_scan",
                payload={"actor_id": str(actor.user_id), "chat_id": chat_id},
                idempotency_key=stable_idempotency_key("readiness-scan", update_id),
            )
            response = "Проверка готовности запущена. Новые готовые ревизии придут отдельно."
        elif command in {"/status", "/resume", "/cancel", "/submit", "/history"}:
            response = (
                f"Команда {command} принята. Текущий workflow будет загружен из сохранённой сессии."
            )
        elif command == "/review":
            rows = await connection.fetch(
                """
                SELECT d.id, d.current_revision, r.content, r.content_hash, d.created_at
                FROM drafts d
                JOIN conversation_sessions s ON s.id=d.session_id AND s.workflow='trainer'
                JOIN draft_revisions r
                  ON r.draft_id=d.id AND r.revision=d.current_revision
                WHERE d.status IN ('submitted','under_review')
                ORDER BY d.created_at LIMIT 10
                """
            )
            if not rows:
                response = "Новых анкет на проверку нет."
            else:
                lines = ["Анкеты на проверку:"]
                keyboard = []
                for row in rows:
                    fields = row["content"].get("fields", {})
                    city = fields.get("city", {}) if isinstance(fields, dict) else {}
                    city_name = city.get("other_name") or city.get("name") or "город не указан"
                    lines.append(
                        f"• {city_name} · ревизия {row['current_revision']} · "
                        f"{row['content_hash'][:12]}"
                    )
                    callback = await create_callback(
                        connection,
                        actor=actor,
                        action="review_start",
                        target_id=row["id"],
                        ttl_seconds=7 * 24 * 60 * 60,
                    )
                    keyboard.append([{
                        "text": f"Проверить: {city_name}",
                        "callback_data": callback.callback_data,
                    }])
                await self._reply(
                    connection,
                    update_id,
                    chat_id,
                    "\n".join(lines),
                    reply_markup={"inline_keyboard": keyboard},
                )
                return
        elif command in ROLE_COMMANDS:
            response = f"Раздел {command} доступен. Выберите запись в следующем меню."
        else:
            response = "Неизвестная команда. Используйте /help."
        await self._reply(connection, update_id, chat_id, response)

    @staticmethod
    def _trainer_review_summary(content: dict, revision: int, content_hash: str) -> str:
        fields = content.get("fields", {}) if isinstance(content, dict) else {}
        city = fields.get("city", {}) if isinstance(fields, dict) else {}
        metrics = fields.get("metrics", {}) if isinstance(fields, dict) else {}
        city_name = city.get("other_name") or city.get("name") or "не указан"
        return (
            "Анкета тренера готова к решению.\n\n"
            f"Город: {city_name}\n"
            f"Статус: {city.get('status') or 'не указан'}\n"
            f"Описание: {city.get('summary') or 'не заполнено'}\n"
            f"История: {city.get('history') or 'не заполнена'}\n"
            f"Игроков: {metrics.get('players_total', 'не указано')}\n"
            f"Тренеров: {metrics.get('coaches_total', 'не указано')}\n"
            f"Клубов: {metrics.get('clubs_total', 'не указано')}\n\n"
            f"Ревизия: {revision} · {content_hash[:12]}"
        )

    async def _reply(
        self,
        connection: asyncpg.Connection,
        update_id: int,
        chat_id: int,
        text: str,
        reply_markup: dict | None = None,
    ) -> None:
        await enqueue_outbox(
            connection,
            event_type="telegram_message",
            payload={"chat_id": chat_id, "text": text, "reply_markup": reply_markup},
            idempotency_key=stable_idempotency_key("reply", update_id, text),
        )

    @staticmethod
    def _message_type(message) -> str:
        if message.voice:
            return "voice"
        if message.audio:
            return "audio"
        if message.photo:
            return "photo"
        if message.document:
            return "document"
        if message.contact:
            return "contact"
        return "text"


async def run_outbox(bot: Bot, pool: asyncpg.Pool, worker_id: str, stop: asyncio.Event) -> None:
    while not stop.is_set():
        event = None
        async with transaction(pool) as connection:
            event = await connection.fetchrow(
                """
                WITH candidate AS (
                    SELECT id FROM outbox_events
                    WHERE status IN ('pending','retry') AND available_at <= now()
                    ORDER BY available_at, created_at
                    FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE outbox_events o SET status='sending', locked_at=now(), locked_by=$1,
                    attempts=attempts+1
                FROM candidate WHERE o.id=candidate.id RETURNING o.*
                """,
                worker_id,
            )
        if not event:
            try:
                await asyncio.wait_for(stop.wait(), timeout=1)
            except TimeoutError:
                continue
            continue
        try:
            payload = event["payload"]
            sent = await bot.send_message(
                chat_id=payload["chat_id"],
                text=payload["text"],
                reply_markup=payload.get("reply_markup"),
            )
            await pool.execute(
                """
                UPDATE outbox_events SET status='sent', sent_at=now(), external_id=$2,
                    locked_at=NULL, locked_by=NULL WHERE id=$1
                """,
                event["id"],
                str(sent.message_id),
            )
        except Exception as exc:
            terminal = event["attempts"] >= 5
            await pool.execute(
                """
                UPDATE outbox_events SET status=$2, available_at=now() + interval '30 seconds',
                    locked_at=NULL, locked_by=NULL, last_error=$3 WHERE id=$1
                """,
                event["id"],
                "dead" if terminal else "retry",
                str(exc)[-2000:],
            )
