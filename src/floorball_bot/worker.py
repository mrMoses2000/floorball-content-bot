from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from uuid import uuid4

import asyncpg

from floorball_bot.db import transaction
from floorball_bot.domain import ExtractedCityPatch
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
        result = await self.extractor.extract(str(job.payload["text"]), ExtractedCityPatch)
        known: list[str] = []
        if result.city_slug:
            known.append(f"город: {result.city_slug}")
        if result.description:
            known.append("описание")
        if result.history:
            known.append("история")
        if result.players_count is not None:
            known.append(f"игроков: {result.players_count}")
        summary = "Сохранено: " + (", ".join(known) if known else "сообщение без однозначных полей")
        if result.next_questions:
            summary += "\n\n" + "\n".join(result.next_questions[:2])
        async with transaction(self.pool) as connection:
            await enqueue_outbox(
                connection,
                event_type="telegram_message",
                payload={"chat_id": int(job.payload["chat_id"]), "text": summary},
                idempotency_key=stable_idempotency_key("extract-result", job.id),
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
