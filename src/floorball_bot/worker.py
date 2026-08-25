from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg

from floorball_bot.context_gateway import AgentContextGateway, AgentMode, load_context_actor
from floorball_bot.db import transaction
from floorball_bot.dialogue import DialogueMode, DialogueSpecRepository
from floorball_bot.dialogue.evaluator import evaluate_gaps
from floorball_bot.dialogue.patches import (
    ExtractedDialoguePatch,
    apply_dialogue_patch,
    canonical_context,
)
from floorball_bot.errors import PermanentProviderError, RetryableProviderError
from floorball_bot.media import MediaPipeline
from floorball_bot.providers.codex import StructuredExtractor
from floorball_bot.providers.transcription import Transcriber
from floorball_bot.queue import (
    ClaimedJob,
    claim_job,
    complete_job,
    enqueue_outbox,
    fail_job,
    stable_idempotency_key,
)

logger = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        extractor: StructuredExtractor,
        transcriber: Transcriber,
        media_pipeline: MediaPipeline | None = None,
        lease_seconds: int = 300,
    ) -> None:
        self.pool = pool
        self.extractor = extractor
        self.transcriber = transcriber
        self.media_pipeline = media_pipeline
        self.dialogues = DialogueSpecRepository()
        self.context_gateway = AgentContextGateway(pool)
        self.lease_seconds = lease_seconds
        self.worker_id = f"worker-{uuid4()}"
        self.stop_event = asyncio.Event()

    async def stop(self) -> None:
        self.stop_event.set()

    async def run(self) -> None:
        while not self.stop_event.is_set():
            job = await claim_job(
                self.pool, worker_id=self.worker_id, lease_seconds=self.lease_seconds
            )
            if job is None:
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
            else:
                raise PermanentProviderError(f"unsupported job kind: {job.kind}")
            await complete_job(self.pool, job.id)
        except RetryableProviderError as exc:
            await fail_job(self.pool, job, type(exc).__name__, retryable=True)
            logger.warning("job_retry", extra={"job_id": str(job.id), "error": type(exc).__name__})
        except Exception as exc:
            await fail_job(self.pool, job, type(exc).__name__, retryable=False)
            logger.exception("job_dead", extra={"job_id": str(job.id), "error": type(exc).__name__})

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
            await connection.execute(
                """
                INSERT INTO media_assets(
                    sha256, original_filename, detected_mime, byte_size, width, height,
                    uploader_id, original_path, derivative_path, moderation_status
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'pending')
                ON CONFLICT (sha256) DO NOTHING
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
            await enqueue_outbox(
                connection,
                event_type="telegram_message",
                payload={
                    "chat_id": int(job.payload["chat_id"]),
                    "text": "Фото обработано без EXIF и ожидает consent/moderation approval.",
                },
                idempotency_key=stable_idempotency_key("media-result", job.id),
            )
