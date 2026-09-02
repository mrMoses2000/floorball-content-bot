from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from floorball_bot.db import create_pool, run_migrations
from floorball_bot.errors import ValidationBlocked
from floorball_bot.media_consent import reconcile_withdrawn_media
from floorball_bot.readiness import scan_readiness

pytestmark = pytest.mark.postgres


@pytest.fixture
async def media_pool():
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not configured")
    pool = await create_pool(dsn)
    await run_migrations(pool, Path(__file__).parents[1] / "migrations")
    await pool.execute("TRUNCATE users, media_assets, consents, audit_log RESTART IDENTITY CASCADE")
    yield pool
    await pool.close()


async def create_media(media_pool, media_root: Path, *, derivative_path: Path | None = None):
    uploader_id = await media_pool.fetchval(
        """
        INSERT INTO users(phone_e164, display_name)
        VALUES ('+77070000031','Media uploader') RETURNING id
        """
    )
    original = media_root / "originals" / str(uploader_id) / ("a" * 64)
    derivative = derivative_path or media_root / "derived" / "aa" / f"{'a' * 64}.webp"
    original.parent.mkdir(parents=True, exist_ok=True)
    derivative.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(b"private-original")
    derivative.write_bytes(b"public-derivative")
    media_id = await media_pool.fetchval(
        """
        INSERT INTO media_assets(
            sha256, original_filename, detected_mime, byte_size, width, height,
            uploader_id, original_path, derivative_path, moderation_status
        ) VALUES ($1,'photo.png','image/png',16,10,10,$2,$3,$4,'approved')
        RETURNING id
        """,
        "a" * 64,
        uploader_id,
        str(original),
        str(derivative),
    )
    city_id = await media_pool.fetchval(
        """
        INSERT INTO cities(slug, name_ru, name_kz, name_en)
        VALUES ('media-city','Медиа','Медиа','Media') RETURNING id
        """
    )
    await media_pool.execute(
        """
        INSERT INTO media_links(
            media_id, entity_type, entity_id, purpose, selected_for_publication
        ) VALUES ($1,'city',$2,'gallery',TRUE)
        """,
        media_id,
        city_id,
    )
    return media_id, original, derivative


async def add_consent(media_pool, media_id, status: str, when: datetime) -> None:
    await media_pool.execute(
        """
        INSERT INTO consents(
            subject_type, subject_id, scope, status, valid_from, created_at, updated_at
        ) VALUES ('media',$1,'media_publication',$2,$3,$3,$3)
        """,
        media_id,
        status,
        when,
    )


@pytest.mark.asyncio
async def test_next_readiness_projection_removes_withdrawn_public_derivative(media_pool, tmp_path):
    media_root = tmp_path / "media"
    media_id, original, derivative = await create_media(media_pool, media_root)
    await add_consent(media_pool, media_id, "withdrawn", datetime(2026, 9, 1, tzinfo=UTC))

    await scan_readiness(media_pool, media_root=media_root)

    assert original.read_bytes() == b"private-original"
    assert not derivative.exists()
    assert await media_pool.fetchval(
        "SELECT derivative_path IS NULL FROM media_assets WHERE id=$1", media_id
    )
    assert not await media_pool.fetchval(
        "SELECT selected_for_publication FROM media_links WHERE media_id=$1", media_id
    )
    assert (
        await media_pool.fetchval(
            "SELECT count(*) FROM audit_log WHERE action='media_derivative_withdrawn'"
        )
        == 1
    )

    result = await reconcile_withdrawn_media(media_pool, media_root=media_root)
    assert result.removed_media_ids == ()
    assert (
        await media_pool.fetchval(
            "SELECT count(*) FROM audit_log WHERE action='media_derivative_withdrawn'"
        )
        == 1
    )


@pytest.mark.asyncio
async def test_latest_regrant_keeps_derivative_until_consent_is_withdrawn_again(
    media_pool, tmp_path
):
    media_root = tmp_path / "media"
    media_id, _original, derivative = await create_media(media_pool, media_root)
    now = datetime.now(UTC)
    await add_consent(media_pool, media_id, "withdrawn", now - timedelta(minutes=2))
    await add_consent(media_pool, media_id, "granted", now - timedelta(minutes=1))

    result = await reconcile_withdrawn_media(media_pool, media_root=media_root, as_of=now)

    assert result.removed_media_ids == ()
    assert derivative.is_file()
    assert await media_pool.fetchval(
        "SELECT derivative_path IS NOT NULL FROM media_assets WHERE id=$1", media_id
    )


@pytest.mark.asyncio
async def test_withdrawal_refuses_derivative_outside_managed_root(media_pool, tmp_path):
    media_root = tmp_path / "media"
    outside = tmp_path / "outside" / "public.webp"
    media_id, original, derivative = await create_media(
        media_pool, media_root, derivative_path=outside
    )
    await add_consent(media_pool, media_id, "withdrawn", datetime.now(UTC))

    with pytest.raises(ValidationBlocked, match="outside managed media root"):
        await reconcile_withdrawn_media(media_pool, media_root=media_root)

    assert original.is_file()
    assert derivative.is_file()
    assert await media_pool.fetchval(
        "SELECT derivative_path FROM media_assets WHERE id=$1", media_id
    ) == str(derivative)
    assert (
        await media_pool.fetchval(
            "SELECT count(*) FROM audit_log WHERE action='media_derivative_withdrawn'"
        )
        == 0
    )


@pytest.mark.asyncio
async def test_withdrawal_refuses_symlink_escape_before_any_deletion(media_pool, tmp_path):
    media_root = tmp_path / "media"
    media_id, original, derivative = await create_media(media_pool, media_root)
    outside = tmp_path / "outside.webp"
    outside.write_bytes(b"outside-public-file")
    derivative.unlink()
    derivative.symlink_to(outside)
    await add_consent(media_pool, media_id, "withdrawn", datetime.now(UTC))

    with pytest.raises(ValidationBlocked, match="outside managed media root"):
        await reconcile_withdrawn_media(media_pool, media_root=media_root)

    assert original.is_file()
    assert derivative.is_symlink()
    assert outside.read_bytes() == b"outside-public-file"
    assert await media_pool.fetchval(
        "SELECT derivative_path FROM media_assets WHERE id=$1", media_id
    ) == str(derivative)
