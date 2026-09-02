from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import asyncpg

from floorball_bot.errors import ValidationBlocked


@dataclass(frozen=True)
class MediaConsentReconciliation:
    removed_media_ids: tuple[UUID, ...]


def _managed_derivative(path: str, media_root: Path) -> Path:
    derived_root = (media_root / "derived").resolve()
    candidate = Path(path)
    resolved = candidate.resolve(strict=False)
    if resolved == derived_root or not resolved.is_relative_to(derived_root):
        raise ValidationBlocked("media derivative is outside managed media root")
    if candidate.is_symlink() or (resolved.exists() and not resolved.is_file()):
        raise ValidationBlocked("media derivative is not a regular file")
    return resolved


def _remove_derivative(path: Path, media_root: Path) -> None:
    path.unlink(missing_ok=True)
    derived_root = (media_root / "derived").resolve()
    parent = path.parent.resolve(strict=False)
    while parent != derived_root and parent.is_relative_to(derived_root):
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


async def reconcile_withdrawn_media(
    pool: asyncpg.Pool,
    *,
    media_root: Path,
    as_of: datetime | None = None,
) -> MediaConsentReconciliation:
    """Remove managed public derivatives whose latest media consent is withdrawn.

    Originals are deliberately untouched. All paths are validated before the first unlink so an
    unexpected or escaped path blocks the whole reconciliation without partial filesystem work.
    A missing derivative is treated as already removed and its stale database pointer is cleared.
    """
    effective_at = as_of or datetime.now(UTC)
    root = media_root.expanduser().resolve()
    async with pool.acquire() as connection, connection.transaction():
        rows = await connection.fetch(
            """
            SELECT ma.id, ma.sha256, ma.derivative_path
            FROM media_assets ma
            JOIN LATERAL (
                SELECT c.status
                FROM consents c
                WHERE c.subject_type='media' AND c.subject_id=ma.id
                  AND c.scope='media_publication'
                  AND (c.valid_from IS NULL OR c.valid_from <= $1)
                ORDER BY c.updated_at DESC, c.created_at DESC, c.id DESC
                LIMIT 1
            ) latest_consent ON latest_consent.status='withdrawn'
            WHERE ma.derivative_path IS NOT NULL
            ORDER BY ma.id
            FOR UPDATE OF ma
            """,
            effective_at,
        )
        managed = [(row, _managed_derivative(row["derivative_path"], root)) for row in rows]
        removed: list[UUID] = []
        for row, derivative in managed:
            _remove_derivative(derivative, root)
            await connection.execute(
                """
                UPDATE media_assets SET derivative_path=NULL, revision=revision+1,
                    updated_at=now()
                WHERE id=$1
                """,
                row["id"],
            )
            await connection.execute(
                """
                UPDATE media_links SET selected_for_publication=FALSE
                WHERE media_id=$1 AND selected_for_publication=TRUE
                """,
                row["id"],
            )
            await connection.execute(
                """
                UPDATE news_media_items SET selected_for_publication=FALSE
                WHERE media_id=$1 AND selected_for_publication=TRUE
                """,
                row["id"],
            )
            await connection.execute(
                """
                INSERT INTO audit_log(action, entity_type, entity_id, metadata)
                VALUES ('media_derivative_withdrawn','media',$1,$2::jsonb)
                """,
                row["id"],
                {
                    "sha256": row["sha256"],
                    "consentStatus": "withdrawn",
                    "originalRetained": True,
                },
            )
            removed.append(row["id"])
    return MediaConsentReconciliation(removed_media_ids=tuple(removed))
