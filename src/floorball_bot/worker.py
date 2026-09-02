from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg

from floorball_bot.callbacks import create_callback
from floorball_bot.contact_requests import ContactDelivery, ContactMailer
from floorball_bot.context_gateway import AgentContextGateway, AgentMode, load_context_actor
from floorball_bot.db import transaction
from floorball_bot.dialogue import DialogueMode, DialogueSpecRepository
from floorball_bot.dialogue.evaluator import evaluate_gaps
from floorball_bot.dialogue.patches import (
    ExtractedDialoguePatch,
    apply_dialogue_patch,
    canonical_context,
)
from floorball_bot.errors import (
    PermanentProviderError,
    RetryableProviderError,
    ValidationBlocked,
)
from floorball_bot.health import record_heartbeat
from floorball_bot.media import MediaPipeline
from floorball_bot.projection.apply import apply_approved_trainer_draft
from floorball_bot.projection.news import apply_approved_news_draft
from floorball_bot.providers.codex import StructuredExtractor
from floorball_bot.providers.transcription import Transcriber
from floorball_bot.publisher import GitPublisher
from floorball_bot.queue import (
    ClaimedJob,
    claim_job,
    complete_job,
    enqueue_job,
    enqueue_outbox,
    fail_job,
    stable_idempotency_key,
)
from floorball_bot.readiness import scan_readiness

logger = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        extractor: StructuredExtractor,
        transcriber: Transcriber,
        media_pipeline: MediaPipeline | None = None,
        publisher: GitPublisher | None = None,
        contact_mailer: ContactMailer | None = None,
        lease_seconds: int = 300,
        readiness_interval_seconds: int = 60,
    ) -> None:
        self.pool = pool
        self.extractor = extractor
        self.transcriber = transcriber
        self.media_pipeline = media_pipeline
        self.publisher = publisher
        self.contact_mailer = contact_mailer
        self.dialogues = DialogueSpecRepository()
        self.context_gateway = AgentContextGateway(pool)
        self.lease_seconds = lease_seconds
        self.readiness_interval_seconds = readiness_interval_seconds
        self._last_readiness_scan = 0.0
        self._last_publication_reconcile = 0.0
        self._last_heartbeat = 0.0
        self.worker_id = f"worker-{uuid4()}"
        self.stop_event = asyncio.Event()

    async def _scan_readiness(self) -> None:
        await scan_readiness(
            self.pool,
            media_root=self.media_pipeline.root if self.media_pipeline is not None else None,
        )

    async def stop(self) -> None:
        self.stop_event.set()

    async def run(self) -> None:
        while not self.stop_event.is_set():
            now = time.monotonic()
            if now - self._last_heartbeat >= 30:
                await record_heartbeat(self.pool, "worker", {"state": "running"})
                self._last_heartbeat = now
            job = await claim_job(
                self.pool, worker_id=self.worker_id, lease_seconds=self.lease_seconds
            )
            if job is None:
                now = time.monotonic()
                if (
                    self.publisher is not None
                    and now - self._last_publication_reconcile >= 30
                ):
                    self._last_publication_reconcile = now
                    try:
                        await self._reconcile_one_stale_publication()
                    except Exception:
                        logger.exception("publication_reconcile_scan_failed")
                if now - self._last_readiness_scan >= self.readiness_interval_seconds:
                    self._last_readiness_scan = now
                    try:
                        await self._scan_readiness()
                    except Exception:
                        logger.exception("readiness_scan_failed")
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=1)
                except TimeoutError:
                    continue
                continue
            await self.process(job)

    async def process(self, job: ClaimedJob) -> None:
        try:
            if job.kind == "transcribe":
                await self._transcribe(job)
            elif job.kind == "extract":
                await self._extract(job)
            elif job.kind == "media":
                await self._media(job)
            elif job.kind == "readiness_scan":
                await self._scan_readiness()
            elif job.kind == "apply_projection":
                await self._apply_projection(job)
            elif job.kind == "contact_delivery":
                await self._contact_delivery(job)
            elif job.kind == "publish_preview":
                await self._publish_preview(job)
            elif job.kind == "publish_confirm":
                await self._publish_confirm(job)
            elif job.kind == "publish_reconcile":
                await self._publish_reconcile(job)
            else:
                raise PermanentProviderError(f"unsupported job kind: {job.kind}")
            await complete_job(self.pool, job.id)
        except RetryableProviderError as exc:
            await fail_job(self.pool, job, type(exc).__name__, retryable=True)
            logger.warning("job_retry", extra={"job_id": str(job.id), "error": type(exc).__name__})
        except Exception as exc:
            await fail_job(self.pool, job, type(exc).__name__, retryable=False)
            logger.exception("job_dead", extra={"job_id": str(job.id), "error": type(exc).__name__})

    def _require_publisher(self) -> GitPublisher:
        if self.publisher is None:
            raise PermanentProviderError("publisher is not configured")
        return self.publisher

    async def _apply_projection(self, job: ClaimedJob) -> None:
        draft_id = UUID(str(job.payload["draft_id"]))
        actor_id = UUID(str(job.payload["actor_id"]))
        chat_id = int(job.payload["chat_id"])
        async with self.pool.acquire() as connection:
            actor = await load_context_actor(connection, actor_id)
            workflow = await connection.fetchval(
                """
                SELECT s.workflow FROM drafts d
                JOIN conversation_sessions s ON s.id=d.session_id
                WHERE d.id=$1
                """,
                draft_id,
            )
        if actor is None or not actor.active:
            raise PermanentProviderError("projection actor no longer exists")
        if workflow == "news":
            result = await apply_approved_news_draft(
                self.pool, draft_id=draft_id, actor=actor
            )
        elif workflow == "trainer":
            result = await apply_approved_trainer_draft(
                self.pool, draft_id=draft_id, actor=actor
            )
        else:
            raise PermanentProviderError("approved dialogue has no canonical projector")
        async with transaction(self.pool) as connection:
            await enqueue_job(
                connection,
                kind="readiness_scan",
                payload={
                    "actor_id": str(actor_id),
                    "chat_id": chat_id,
                    "application_id": str(result.application_id),
                },
                idempotency_key=stable_idempotency_key(
                    "readiness-after-projection", result.application_id
                ),
            )
            state = "перенесены" if result.applied else "уже были перенесены"
            await enqueue_outbox(
                connection,
                event_type="telegram_message",
                payload={
                    "chat_id": chat_id,
                    "text": (
                        f"Разрешённые данные ревизии {result.revision} {state} в основную БД. "
                        "Запущена проверка готовности; если публичный набор полон, бот "
                        "отдельно предложит собрать preview."
                    ),
                },
                idempotency_key=stable_idempotency_key(
                    "projection-applied", result.application_id, chat_id
                ),
            )

    async def _contact_delivery(self, job: ClaimedJob) -> None:
        request_id = UUID(str(job.payload["request_id"]))
        async with transaction(self.pool) as connection:
            row = await connection.fetchrow(
                """
                UPDATE contact_requests SET status='sending', attempts=attempts+1,
                    locked_at=now(), locked_by=$2, updated_at=now()
                WHERE id=$1 AND status IN ('pending','retry','sending')
                RETURNING id AS request_id, locale, name, reply_to, subject,
                          message, recipient, attempts
                """,
                request_id,
                self.worker_id,
            )
            if not row:
                status = await connection.fetchval(
                    "SELECT status FROM contact_requests WHERE id=$1", request_id
                )
                if status == "sent":
                    return
                raise PermanentProviderError("contact request is not deliverable")
        if self.contact_mailer is None:
            await self.pool.execute(
                """
                UPDATE contact_requests SET status='dead', last_error='mailer_not_configured',
                    locked_at=NULL, locked_by=NULL, updated_at=now() WHERE id=$1
                """,
                request_id,
            )
            await self._notify_contact_failure(request_id, "mailer_not_configured")
            raise PermanentProviderError("contact mailer is not configured")
        delivery_data = dict(row)
        delivery_data.pop("attempts")
        delivery = ContactDelivery.model_validate(delivery_data)
        try:
            external_id = await self.contact_mailer.send(delivery)
        except Exception as exc:
            terminal = row["attempts"] >= job.max_attempts
            await self.pool.execute(
                """
                UPDATE contact_requests SET status=$2,
                    available_at=now()+interval '30 seconds',
                    last_error=$3, locked_at=NULL, locked_by=NULL, updated_at=now()
                WHERE id=$1
                """,
                request_id,
                "dead" if terminal else "retry",
                type(exc).__name__,
            )
            if terminal:
                await self._notify_contact_failure(request_id, type(exc).__name__)
                raise PermanentProviderError("contact delivery exhausted retries") from exc
            raise RetryableProviderError("contact delivery failed") from exc
        await self.pool.execute(
            """
            UPDATE contact_requests SET status='sent', external_id=$2, sent_at=now(),
                last_error='', locked_at=NULL, locked_by=NULL, updated_at=now()
            WHERE id=$1
            """,
            request_id,
            external_id[:500],
        )

    async def _notify_contact_failure(self, request_id: UUID, error_class: str) -> None:
        recipients = await self.pool.fetch(
            """
            SELECT DISTINCT u.id, u.telegram_id
            FROM users u
            JOIN user_roles ur ON ur.user_id=u.id AND ur.role_name='superadmin'
                              AND ur.revoked_at IS NULL
            WHERE u.active=TRUE AND u.deleted_at IS NULL AND u.telegram_id IS NOT NULL
            """
        )
        async with transaction(self.pool) as connection:
            for recipient in recipients:
                await enqueue_outbox(
                    connection,
                    event_type="telegram_message",
                    payload={
                        "chat_id": recipient["telegram_id"],
                        "text": (
                            "Заявка с сайта не доставлена после повторных попыток. "
                            f"Request ID: {request_id}. Ошибка: {error_class}. "
                            "Данные сохранены в contact_requests; нужна ручная проверка SMTP."
                        ),
                    },
                    idempotency_key=stable_idempotency_key(
                        "contact-delivery-dead", request_id, recipient["id"]
                    ),
                )

    async def _publication_recipients(self, fallback_chat_id: int | None) -> set[int]:
        rows = await self.pool.fetch(
            """
            SELECT DISTINCT u.telegram_id
            FROM notification_subscriptions s
            JOIN users u ON u.id=s.user_id AND u.active=TRUE AND u.deleted_at IS NULL
            JOIN user_roles ur ON ur.user_id=u.id AND ur.role_name='superadmin'
                              AND ur.revoked_at IS NULL
            WHERE s.event_type='publication_status' AND s.enabled=TRUE
              AND u.telegram_id IS NOT NULL
            """
        )
        recipients = {int(row["telegram_id"]) for row in rows}
        if fallback_chat_id is not None:
            recipients.add(fallback_chat_id)
        return recipients

    async def _publish_preview(self, job: ClaimedJob) -> None:
        publisher = self._require_publisher()
        draft_id = UUID(str(job.payload["draft_id"]))
        actor_id = UUID(str(job.payload["actor_id"]))
        chat_id = int(job.payload["chat_id"])
        async with transaction(self.pool) as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext('floorball-preview-create'))"
            )
            row = await connection.fetchrow(
                """
                SELECT d.approved_revision, r.content, r.content_hash
                FROM drafts d
                JOIN draft_revisions r
                  ON r.draft_id=d.id AND r.revision=d.approved_revision
                WHERE d.id=$1 AND d.status='approved'
                FOR UPDATE OF d
                """,
                draft_id,
            )
            if not row:
                raise PermanentProviderError("draft must have an approved revision")
            active = await connection.fetchrow(
                """
                SELECT id, status FROM publication_jobs
                WHERE draft_id=$1 AND revision=$2
                  AND status NOT IN ('failed','cancelled')
                ORDER BY created_at DESC LIMIT 1
                """,
                draft_id,
                row["approved_revision"],
            )
            if active:
                if active["status"] in {"preview_ready", "published"}:
                    await enqueue_outbox(
                        connection,
                        event_type="telegram_message",
                        payload={
                            "chat_id": chat_id,
                            "text": "Для этой ревизии preview уже создан или публикация завершена.",
                        },
                        idempotency_key=stable_idempotency_key(
                            "preview-already-active", job.id, active["id"]
                        ),
                    )
                    return
                raise RetryableProviderError("publication preview is already building")
            publication_id = await connection.fetchval(
                """
                INSERT INTO publication_jobs(
                    draft_id, revision, revision_hash, requested_by
                ) VALUES ($1,$2,$3,$4) RETURNING id
                """,
                draft_id,
                row["approved_revision"],
                row["content_hash"],
                actor_id,
            )
            content = row["content"]
        try:
            preview = await publisher.build_preview(publication_id, content)
        except Exception as exc:
            await self.pool.execute(
                """
                UPDATE publication_jobs SET status='failed', updated_at=now(),
                    check_output=$2 WHERE id=$1
                """,
                publication_id,
                f"{type(exc).__name__}: {str(exc)[-3000:]}",
            )
            async with transaction(self.pool) as connection:
                actor = await load_context_actor(connection, actor_id)
                retry_markup = None
                if actor is not None:
                    retry = await create_callback(
                        connection,
                        actor=actor,
                        action="approve_preview",
                        target_id=draft_id,
                        ttl_seconds=7 * 24 * 60 * 60,
                    )
                    retry_markup = {
                        "inline_keyboard": [[{
                            "text": "Повторить сборку preview",
                            "callback_data": retry.callback_data,
                        }]]
                    }
                await enqueue_outbox(
                    connection,
                    event_type="telegram_message",
                    payload={
                        "chat_id": chat_id,
                        "text": (
                            "Preview не собран; commit/push не выполнялись. "
                            f"Причина: {type(exc).__name__}. Проверьте журнал и повторите."
                        ),
                        "reply_markup": retry_markup,
                    },
                    idempotency_key=stable_idempotency_key(
                        "preview-failed", publication_id, job.id
                    ),
                )
            raise
        async with transaction(self.pool) as connection:
            actor = await load_context_actor(connection, actor_id)
            if actor is None:
                raise PermanentProviderError("publishing actor no longer exists")
            callback = await create_callback(
                connection,
                actor=actor,
                action=f"confirm_publish|{preview.nonce}",
                target_id=publication_id,
                ttl_seconds=30 * 60,
                revision_hash=preview.revision_hash,
                manifest_hash=preview.screenshot_manifest_hash,
            )
            needs_changes = await create_callback(
                connection,
                actor=actor,
                action="preview_needs_changes",
                target_id=publication_id,
                ttl_seconds=30 * 60,
                revision_hash=preview.revision_hash,
                manifest_hash=preview.screenshot_manifest_hash,
            )
            cancel = await create_callback(
                connection,
                actor=actor,
                action="preview_cancel",
                target_id=publication_id,
                ttl_seconds=30 * 60,
                revision_hash=preview.revision_hash,
                manifest_hash=preview.screenshot_manifest_hash,
            )
            for batch_index in range(0, len(preview.artifacts), 10):
                batch = preview.artifacts[batch_index : batch_index + 10]
                await enqueue_outbox(
                    connection,
                    event_type="telegram_media_group",
                    payload={
                        "chat_id": chat_id,
                        "paths": [str(path) for path in batch],
                        "caption": (
                            f"Preview {preview.screenshot_manifest_hash[:12]} · "
                            f"кадры {batch_index + 1}–{batch_index + len(batch)}"
                        ),
                    },
                    idempotency_key=stable_idempotency_key(
                        "preview-media-group", publication_id, batch_index // 10
                    ),
                )
            await enqueue_outbox(
                connection,
                event_type="telegram_message",
                payload={
                    "chat_id": chat_id,
                    "text": (
                        "Preview готов. Тесты и сборка сайта прошли.\n\n"
                        f"Изменения:\n{preview.diff_summary or 'контентные файлы обновлены'}\n"
                        f"Базовый commit: {preview.base_commit[:12]}\n"
                        f"Ревизия: {preview.revision_hash[:12]}\n\n"
                        f"Screenshot manifest: {preview.screenshot_manifest_hash[:12]}\n\n"
                        "Проверьте данные. Только кнопка ниже выполнит commit и atomic push."
                    ),
                    "reply_markup": {
                        "inline_keyboard": [
                            [{
                                "text": "Даю добро: commit и push",
                                "callback_data": callback.callback_data,
                            }],
                            [
                                {
                                    "text": "Нужны изменения",
                                    "callback_data": needs_changes.callback_data,
                                },
                                {
                                    "text": "Отменить",
                                    "callback_data": cancel.callback_data,
                                },
                            ],
                        ]
                    },
                },
                idempotency_key=stable_idempotency_key(
                    "preview-ready", publication_id, actor_id
                ),
            )

    async def _publish_confirm(self, job: ClaimedJob) -> None:
        publisher = self._require_publisher()
        publication_id = UUID(str(job.payload["publication_id"]))
        actor_id = UUID(str(job.payload["actor_id"]))
        chat_id = int(job.payload["chat_id"])
        draft_id = await self.pool.fetchval(
            "SELECT draft_id FROM publication_jobs WHERE id=$1", publication_id
        )
        try:
            main_commit, static_commit = await publisher.confirm_and_push(
                publication_id,
                str(job.payload["nonce"]),
                actor_id,
                str(job.payload["manifest_hash"]),
                chat_id=chat_id,
            )
        except RetryableProviderError:
            raise
        except Exception as exc:
            await self.pool.execute(
                """
                UPDATE publication_jobs SET status='failed', reconciliation_error=$2,
                    publish_lease_owner='', publish_lease_expires_at=NULL, updated_at=now()
                WHERE id=$1 AND status NOT IN ('failed','cancelled','published')
                """,
                publication_id,
                f"{type(exc).__name__}: {str(exc)[-3000:]}",
            )
            publication_state = await self.pool.fetchrow(
                "SELECT status,reconciliation_error FROM publication_jobs WHERE id=$1",
                publication_id,
            )
            manual_recovery = bool(
                publication_state
                and "remote ref mismatch" in publication_state["reconciliation_error"]
            )
            async with transaction(self.pool) as connection:
                actor = await load_context_actor(connection, actor_id)
                retry_markup = None
                if actor is not None and draft_id is not None and not manual_recovery:
                    retry = await create_callback(
                        connection,
                        actor=actor,
                        action="approve_preview",
                        target_id=draft_id,
                        ttl_seconds=7 * 24 * 60 * 60,
                    )
                    retry_markup = {
                        "inline_keyboard": [[{
                            "text": "Собрать новый preview",
                            "callback_data": retry.callback_data,
                        }]]
                    }
                await enqueue_outbox(
                    connection,
                    event_type="telegram_message",
                    payload={
                        "chat_id": chat_id,
                        "text": (
                            "Публикация не завершена. Автоматическое развёртывание не запускайте. "
                            f"Причина: {type(exc).__name__}. "
                            + (
                                "Сначала оператор должен сверить удалённые ветки вручную."
                                if manual_recovery
                                else "Нужно собрать новый preview."
                            )
                        ),
                        "reply_markup": retry_markup,
                    },
                    idempotency_key=stable_idempotency_key(
                        "publish-failed", publication_id, job.id
                    ),
                )
            raise
        await self._finalize_publication(
            publication_id,
            main_commit=main_commit,
            static_commit=static_commit,
            fallback_chat_id=chat_id,
        )

    async def _finalize_publication(
        self,
        publication_id: UUID,
        *,
        main_commit: str,
        static_commit: str,
        fallback_chat_id: int | None,
    ) -> None:
        row = await self.pool.fetchrow(
            """
            SELECT draft_id, confirmed_by, confirmation_chat_id
            FROM publication_jobs WHERE id=$1 AND status='published'
              AND main_commit=$2 AND static_commit=$3
            """,
            publication_id,
            main_commit,
            static_commit,
        )
        if not row or row["confirmed_by"] is None:
            raise RetryableProviderError("published state is not ready for final notification")
        await self.pool.execute(
            """
            UPDATE drafts SET status='published', updated_by=$2, updated_at=now()
            WHERE id=$1 AND status='approved'
            """,
            row["draft_id"],
            row["confirmed_by"],
        )
        confirmed_chat = row["confirmation_chat_id"]
        recipients = await self._publication_recipients(
            int(confirmed_chat) if confirmed_chat is not None else fallback_chat_id
        )
        async with transaction(self.pool) as connection:
            for recipient in recipients:
                await enqueue_outbox(
                    connection,
                    event_type="telegram_message",
                    payload={
                        "chat_id": recipient,
                        "text": (
                            "Commit и push завершены и проверены.\n\n"
                            f"main: {main_commit}\nplesk-static: {static_commit}\n\n"
                            "Заходите в панель сайта/Plesk и запускайте развёртывание репозитория."
                        ),
                    },
                    idempotency_key=stable_idempotency_key(
                        "publish-succeeded", publication_id, recipient
                    ),
                )

    async def _publish_reconcile(self, job: ClaimedJob) -> None:
        publisher = self._require_publisher()
        publication_id = UUID(str(job.payload["publication_id"]))
        terminal = await self.pool.fetchval(
            "SELECT status FROM publication_jobs WHERE id=$1", publication_id
        )
        if terminal in {"failed", "cancelled"}:
            await self._notify_reconciliation_failure(publication_id)
            return
        try:
            main_commit, static_commit = await publisher.reconcile_publication(publication_id)
        except ValidationBlocked:
            await self._notify_reconciliation_failure(publication_id)
            return
        await self._finalize_publication(
            publication_id,
            main_commit=main_commit,
            static_commit=static_commit,
            fallback_chat_id=None,
        )

    async def _reconcile_one_stale_publication(self) -> None:
        publisher = self._require_publisher()
        stale = await publisher.stale_publications(limit=1)
        if not stale:
            return
        publication_id = stale[0]
        try:
            main_commit, static_commit = await publisher.reconcile_publication(publication_id)
        except ValidationBlocked:
            await self._notify_reconciliation_failure(publication_id)
            return
        await self._finalize_publication(
            publication_id,
            main_commit=main_commit,
            static_commit=static_commit,
            fallback_chat_id=None,
        )

    async def _notify_reconciliation_failure(self, publication_id: UUID) -> None:
        row = await self.pool.fetchrow(
            """
            SELECT confirmation_chat_id, reconciliation_error
            FROM publication_jobs WHERE id=$1
            """,
            publication_id,
        )
        if not row or row["confirmation_chat_id"] is None:
            return
        async with transaction(self.pool) as connection:
            await enqueue_outbox(
                connection,
                event_type="telegram_message",
                payload={
                    "chat_id": int(row["confirmation_chat_id"]),
                    "text": (
                        "Reconciler не подтвердил обе удалённые ветки публикации. "
                        "Не запускайте развёртывание и не собирайте новый preview, пока оператор "
                        "не сверит main/plesk-static. "
                        f"Причина: {row['reconciliation_error'] or 'state mismatch'}."
                    ),
                },
                idempotency_key=stable_idempotency_key(
                    "publish-reconcile-failed", publication_id
                ),
            )

    async def _transcribe(self, job: ClaimedJob) -> None:
        path = Path(job.payload["path"])
        result = await self.transcriber.transcribe(path, str(job.payload.get("language") or "ru"))
        chat_id = int(job.payload["chat_id"])
        async with transaction(self.pool) as connection:
            message_id = await connection.fetchval(
                """
                SELECT id FROM messages
                WHERE telegram_chat_id=$1 AND message_type IN ('voice','audio')
                ORDER BY created_at DESC LIMIT 1
                """,
                chat_id,
            )
            if message_id:
                await connection.execute(
                    """
                    UPDATE messages SET normalized_text=$2, source_language=$3,
                        provider_metadata=$4::jsonb, transcript_confirmed=FALSE
                    WHERE id=$1
                    """,
                    message_id,
                    result.text,
                    result.language if result.language in {"ru", "kz", "en"} else "unknown",
                    {
                        "provider": "assemblyai",
                        "provider_id": result.provider_id,
                        "confidence": result.confidence,
                        "duration_seconds": result.duration_seconds,
                        "billing": result.billing_metadata or {},
                    },
                )
            await enqueue_outbox(
                connection,
                event_type="telegram_message",
                payload={
                    "chat_id": chat_id,
                    "text": (
                        f"Распознанный текст:\n\n{result.text}\n\n"
                        "Подтвердите текст или пришлите исправление. "
                        "До подтверждения он не попадёт в extractor."
                    ),
                },
                idempotency_key=stable_idempotency_key("transcript-preview", job.id),
            )

    async def _extract(self, job: ClaimedJob) -> None:
        if job.payload.get("mode") in {mode.value for mode in DialogueMode}:
            await self._extract_dialogue(job)
            return
        raise PermanentProviderError("extract jobs require a pinned dialogue mode and session")

    async def _extract_dialogue(self, job: ClaimedJob) -> None:
        session_id = UUID(str(job.payload["session_id"]))
        mode = DialogueMode(str(job.payload["mode"]))
        loaded = self.dialogues.load(mode)
        async with self.pool.acquire() as connection:
            session = await connection.fetchrow(
                """
                SELECT user_id, definition_hash, status
                FROM conversation_sessions WHERE id=$1
                """,
                session_id,
            )
            if not session or session["status"] != "active":
                raise PermanentProviderError("dialogue session is not active")
            if session["definition_hash"] != loaded.sha256:
                raise PermanentProviderError("dialogue definition changed for an active session")
            memory = await connection.fetchval(
                "SELECT structured_memory FROM conversation_memory WHERE session_id=$1",
                session_id,
            ) or {}
            actor = await load_context_actor(connection, session["user_id"])
            language = await connection.fetchval(
                "SELECT preferred_language FROM users WHERE id=$1", session["user_id"]
            ) or "ru"
        if actor is None:
            raise PermanentProviderError("dialogue actor no longer exists")
        fields = dict(memory.get("fields", {}))
        city_slug = None
        if mode == DialogueMode.TRAINER:
            directory = await self.context_gateway.snapshot(
                actor=actor, mode=AgentMode.TRAINER
            )
            city_value = fields.get("city")
            if isinstance(city_value, dict):
                supplied = str(city_value.get("name") or city_value.get("other_name") or "")
                supplied_folded = supplied.casefold()
                for city in directory.cities:
                    names = {city.slug, city.name.ru, city.name.kz, city.name.en}
                    if supplied_folded in {name.casefold() for name in names if name}:
                        city_slug = city.slug
                        break
            context = (
                await self.context_gateway.snapshot(
                    actor=actor, mode=AgentMode.TRAINER, city_slug=city_slug
                )
                if city_slug
                else directory
            )
        else:
            context = await self.context_gateway.snapshot(actor=actor, mode=AgentMode(mode.value))
        _, context_sha256 = canonical_context(context)
        async with transaction(self.pool) as connection:
            await connection.execute(
                """
                INSERT INTO agent_context_snapshots(
                    session_id, mode, definition_hash, context_hash, context
                ) VALUES ($1,$2,$3,$4,$5::jsonb)
                ON CONFLICT (session_id, context_hash) DO NOTHING
                """,
                session_id,
                mode.value,
                loaded.sha256,
                context_sha256,
                context.model_dump(mode="json"),
            )
            await connection.execute(
                """
                UPDATE conversation_sessions
                SET context_hash=$2, subject_key=$3, updated_at=now()
                WHERE id=$1
                """,
                session_id,
                context_sha256,
                city_slug or ("federation" if mode != DialogueMode.TRAINER else ""),
            )
        result = await self.extractor.extract(
            str(job.payload["text"]),
            ExtractedDialoguePatch,
            mode=mode,
            context=context,
            known_fields=fields,
        )
        merged = apply_dialogue_patch(
            loaded.spec,
            fields,
            result,
            expected_spec_sha256=loaded.sha256,
            expected_context_sha256=context_sha256,
        )
        gaps = evaluate_gaps(loaded.spec, merged)
        critical = len(gaps.required_to_start) + len(gaps.required_for_submit)
        later = len(gaps.required_for_publish) + len(gaps.recommended)
        lines = [
            f"Заполнено разделов: {len(merged)}.",
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
            lines.append(question)
        if gaps.can_submit:
            lines.append("Черновик можно отправить командой /submit.")
        async with transaction(self.pool) as connection:
            await connection.execute(
                """
                INSERT INTO conversation_memory(session_id, structured_memory)
                VALUES ($1,$2::jsonb)
                ON CONFLICT (session_id) DO UPDATE
                SET structured_memory=EXCLUDED.structured_memory,
                    revision=conversation_memory.revision+1, updated_at=now()
                """,
                session_id,
                {
                    "fields": merged,
                    "critical_missing": [
                        gap.field_id
                        for gap in (*gaps.required_to_start, *gaps.required_for_submit)
                    ],
                    "publish_missing": [gap.field_id for gap in gaps.required_for_publish],
                    "optional_missing": [gap.field_id for gap in gaps.recommended],
                    "last_question_ids": [gaps.next_field_id] if gaps.next_field_id else [],
                },
            )
            await connection.execute(
                """
                UPDATE conversation_sessions
                SET current_step=$2, updated_at=now(), last_activity_at=now()
                WHERE id=$1
                """,
                session_id,
                gaps.next_field_id or "ready_to_submit",
            )
            await enqueue_outbox(
                connection,
                event_type="telegram_message",
                payload={"chat_id": int(job.payload["chat_id"]), "text": "\n\n".join(lines)},
                idempotency_key=stable_idempotency_key("dialogue-extract-result", job.id),
            )

    async def _media(self, job: ClaimedJob) -> None:
        if self.media_pipeline is None:
            raise PermanentProviderError("media pipeline is not configured")
        from uuid import UUID

        uploader_id = UUID(str(job.payload["user_id"]))
        result = await asyncio.to_thread(
            self.media_pipeline.process_image, Path(job.payload["path"]), uploader_id
        )
        async with transaction(self.pool) as connection:
            media_id = await connection.fetchval(
                """
                INSERT INTO media_assets(
                    sha256, original_filename, detected_mime, byte_size, width, height,
                    uploader_id, original_path, derivative_path, moderation_status
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'pending')
                ON CONFLICT (sha256) DO UPDATE SET updated_at=media_assets.updated_at
                RETURNING id
                """,
                result.sha256,
                str(job.payload.get("filename") or "telegram-image")[:255],
                result.detected_mime,
                result.bytes,
                result.width,
                result.height,
                uploader_id,
                str(result.original_path),
                str(result.derivative_path),
            )
            message_id = job.payload.get("message_id")
            if message_id:
                await connection.execute(
                    "UPDATE messages SET media_id=$2 WHERE id=$1",
                    UUID(str(message_id)),
                    media_id,
                )
            news_photo_count = 0
            session_id = job.payload.get("session_id")
            if session_id and job.payload.get("session_workflow") == "news":
                news_session_id = UUID(str(session_id))
                session = await connection.fetchrow(
                    """
                    SELECT id FROM conversation_sessions
                    WHERE id=$1 AND user_id=$2 AND workflow='news' AND status='active'
                    FOR UPDATE
                    """,
                    news_session_id,
                    uploader_id,
                )
                if not session:
                    raise ValidationBlocked("news media session is no longer active")
                existing = await connection.fetchval(
                    """
                    SELECT EXISTS(
                        SELECT 1 FROM news_session_media
                        WHERE session_id=$1 AND media_id=$2
                    )
                    """,
                    news_session_id,
                    media_id,
                )
                news_photo_count = await connection.fetchval(
                    "SELECT count(*) FROM news_session_media WHERE session_id=$1",
                    news_session_id,
                )
                if not existing and news_photo_count >= 10:
                    raise ValidationBlocked("a news gallery is limited to ten photos")
                await connection.execute(
                    """
                    INSERT INTO news_session_media(
                        session_id, media_id, message_id, telegram_message_id,
                        telegram_media_group_id, caption
                    ) VALUES ($1,$2,$3,$4,$5,$6)
                    ON CONFLICT (session_id, media_id) DO UPDATE SET
                        message_id=COALESCE(news_session_media.message_id, EXCLUDED.message_id),
                        telegram_message_id=LEAST(
                            news_session_media.telegram_message_id,
                            EXCLUDED.telegram_message_id
                        ),
                        caption=CASE WHEN news_session_media.caption=''
                            THEN EXCLUDED.caption ELSE news_session_media.caption END
                    """,
                    news_session_id,
                    media_id,
                    UUID(str(message_id)) if message_id else None,
                    int(job.payload["telegram_message_id"]),
                    str(job.payload.get("telegram_media_group_id") or "")[:128],
                    str(job.payload.get("caption") or "")[:600],
                )
                news_photo_count = await connection.fetchval(
                    "SELECT count(*) FROM news_session_media WHERE session_id=$1",
                    news_session_id,
                )
            await enqueue_outbox(
                connection,
                event_type="telegram_message",
                payload={
                    "chat_id": int(job.payload["chat_id"]),
                    "text": (
                        f"Фото обработано без EXIF и добавлено в галерею новости "
                        f"({news_photo_count}/10). Когда закончите загрузку, отправьте "
                        "/photos-ready."
                        if news_photo_count
                        else "Фото обработано без EXIF и ожидает consent/moderation approval."
                    ),
                },
                idempotency_key=stable_idempotency_key("media-result", job.id),
            )
