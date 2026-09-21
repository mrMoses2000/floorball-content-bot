from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import asyncpg

from floorball_bot.db import transaction
from floorball_bot.queue import enqueue_outbox, stable_idempotency_key

MAX_OFFICIAL_DOCUMENT_BYTES = 20 * 1024 * 1024
_FORBIDDEN_PDF_MARKERS = (
    b"/javascript",
    b"/js",
    b"/launch",
    b"/embeddedfile",
    b"/richmedia",
    b"/openaction",
)


class InvalidOfficialDocument(ValueError):
    """Raised when an uploaded file is not a safe, bounded PDF."""


@dataclass(frozen=True)
class StoredOfficialDocument:
    sha256: str
    byte_size: int
    original_path: Path
    detected_mime: str = "application/pdf"


def safe_original_filename(value: str | None) -> str:
    name = Path(value or "document.pdf").name
    name = "".join(character for character in name if character.isprintable()).strip()
    return (name or "document.pdf")[:255]


def store_official_pdf(
    source: Path,
    media_root: Path,
    uploader_id: UUID,
    *,
    max_bytes: int = MAX_OFFICIAL_DOCUMENT_BYTES,
) -> StoredOfficialDocument:
    size = source.stat().st_size
    if size < 1 or size > max_bytes:
        raise InvalidOfficialDocument("PDF должен быть размером от 1 байта до 20 МБ.")

    digest = hashlib.sha256()
    content = bytearray()
    with source.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            content.extend(chunk)
    lowered = bytes(content).lower()
    if not lowered.startswith(b"%pdf-") or b"%%eof" not in lowered[-8192:]:
        raise InvalidOfficialDocument("Файл не является корректным PDF.")
    if any(marker in lowered for marker in _FORBIDDEN_PDF_MARKERS):
        raise InvalidOfficialDocument(
            "PDF содержит активное или вложенное содержимое и не может быть принят."
        )

    sha256 = digest.hexdigest()
    destination_dir = media_root.resolve() / "official_documents" / str(uploader_id)
    destination_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination_dir.chmod(0o700)
    destination = destination_dir / f"{sha256}.pdf"
    if not destination.exists():
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "wb") as target, source.open("rb") as origin:
                shutil.copyfileobj(origin, target)
    destination.chmod(0o600)
    return StoredOfficialDocument(sha256=sha256, byte_size=size, original_path=destination)


async def official_document_rows(connection: asyncpg.Connection) -> list[asyncpg.Record]:
    return await connection.fetch(
        """
        SELECT r.code, r.title_ru, r.required, r.sort_order,
               d.id AS document_id, d.document_number, d.issued_on, d.valid_until,
               d.status, d.publication_allowed, d.created_at
        FROM official_document_requirements r
        LEFT JOIN LATERAL (
            SELECT id, document_number, issued_on, valid_until, status,
                   publication_allowed, created_at
            FROM official_documents
            WHERE requirement_code=r.code
              AND status IN ('received','verified')
              AND (valid_until IS NULL OR valid_until >= CURRENT_DATE)
            ORDER BY (status='verified') DESC, created_at DESC
            LIMIT 1
        ) d ON TRUE
        WHERE r.active
        ORDER BY r.sort_order, r.code
        """
    )


def format_official_document_status(rows: list[asyncpg.Record]) -> str:
    lines = ["Официальные документы:"]
    for row in rows:
        required = "обязательный" if row["required"] else "дополнительный"
        if row["document_id"]:
            validity = f", до {row['valid_until'].isoformat()}" if row["valid_until"] else ""
            number = f" № {row['document_number']}" if row["document_number"] else ""
            state = "проверен" if row["status"] == "verified" else "получен"
            lines.append(
                f"✅ {row['title_ru']} (`{row['code']}`) — {state}{number}{validity}"
            )
        else:
            icon = "❗" if row["required"] else "▫️"
            lines.append(f"{icon} {row['title_ru']} (`{row['code']}`) — нет, {required}")
    lines.extend(
        [
            "",
            "Загрузка: /document <код>",
            "Пример: /document federation_charter",
            "Отмена текущей загрузки: /document_cancel",
        ]
    )
    return "\n".join(lines)


async def send_missing_document_reminders(pool: asyncpg.Pool) -> int:
    now = datetime.now(UTC)
    week_bucket = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
    async with transaction(pool) as connection:
        missing = await connection.fetch(
            """
            SELECT r.code, r.title_ru
            FROM official_document_requirements r
            WHERE r.active AND r.required AND NOT EXISTS (
                SELECT 1 FROM official_documents d
                WHERE d.requirement_code=r.code
                  AND d.status IN ('received','verified')
                  AND (d.valid_until IS NULL OR d.valid_until >= CURRENT_DATE)
            )
            ORDER BY r.sort_order, r.code
            """
        )
        expiring = await connection.fetch(
            """
            SELECT DISTINCT ON (d.requirement_code)
                   d.requirement_code AS code, r.title_ru, d.valid_until
            FROM official_documents d
            JOIN official_document_requirements r ON r.code=d.requirement_code
            WHERE r.active AND d.status IN ('received','verified')
              AND d.valid_until BETWEEN CURRENT_DATE AND CURRENT_DATE + 30
            ORDER BY d.requirement_code, d.created_at DESC
            """
        )
        if not missing and not expiring:
            return 0
        recipients = await connection.fetch(
            """
            SELECT DISTINCT u.id, u.telegram_id
            FROM users u
            JOIN user_roles ur ON ur.user_id=u.id
            WHERE u.active AND u.deleted_at IS NULL AND u.telegram_id IS NOT NULL
              AND ur.revoked_at IS NULL
              AND ur.role_name IN ('federation_editor','superadmin')
            """
        )
        lines = ["Напоминание по официальным документам."]
        if missing:
            lines.append("Не предоставлены:")
            lines.extend(f"• {row['title_ru']} (`{row['code']}`)" for row in missing)
        if expiring:
            lines.append("Истекают в ближайшие 30 дней:")
            lines.extend(
                f"• {row['title_ru']} — {row['valid_until'].isoformat()}"
                for row in expiring
            )
        lines.append("Откройте /documents и загрузите файл командой /document <код>.")
        text = "\n".join(lines)
        codes = [row["code"] for row in missing] + [row["code"] for row in expiring]
        sent = 0
        for recipient in recipients:
            created = await enqueue_outbox(
                connection,
                event_type="telegram_message",
                payload={"chat_id": recipient["telegram_id"], "text": text},
                idempotency_key=stable_idempotency_key(
                    "official-document-reminder", recipient["id"], week_bucket, codes
                ),
            )
            if created is None:
                continue
            sent += 1
            for row in [*missing, *expiring]:
                await connection.execute(
                    """
                    INSERT INTO official_document_events(
                        requirement_code, actor_id, action, details
                    ) VALUES ($1,$2,'reminder_sent',$3::jsonb)
                    """,
                    row["code"],
                    recipient["id"],
                    {"week": week_bucket},
                )
        return sent
