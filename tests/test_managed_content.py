from __future__ import annotations

import hashlib
import io
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image
from test_federation_projection import seed
from test_miniapp_api import signed_init_data
from test_trainer_projection_postgres import _seed_case

from floorball_bot.attachments import attach_session_media, reviewed_media
from floorball_bot.db import create_pool, run_migrations
from floorball_bot.dialogue.repository import DialogueSpecRepository
from floorball_bot.domain import ExtractedCityPatch
from floorball_bot.errors import ValidationBlocked
from floorball_bot.exporters import project_city_payload, project_federation_payload
from floorball_bot.media import MediaPipeline
from floorball_bot.miniapp_api import create_miniapp_app
from floorball_bot.projection.apply import apply_approved_trainer_draft
from floorball_bot.projection.federation import apply_approved_federation_draft
from floorball_bot.providers.agy import FakeExtractor
from floorball_bot.providers.transcription import FakeTranscriber
from floorball_bot.publisher import GitPublisher
from floorball_bot.queue import claim_job
from floorball_bot.worker import Worker
from floorball_bot.workflow import canonical_hash

pytestmark = pytest.mark.postgres


@pytest.fixture
async def pg_pool():
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not configured")
    pool = await create_pool(dsn)
    if not (await pool.fetchval("SELECT current_database()")).endswith("_test"):
        await pool.close()
        raise RuntimeError("refusing destructive fixture on a non-test database")
    await run_migrations(pool, Path(__file__).parents[1] / "migrations")
    await pool.execute("TRUNCATE users,cities,jobs,outbox_events RESTART IDENTITY CASCADE")
    yield pool
    await pool.close()


async def _set_fields(pool, draft, fields):
    content = await pool.fetchval("SELECT content FROM draft_revisions WHERE draft_id=$1", draft)
    content["fields"] = fields
    await pool.execute(
        "UPDATE draft_revisions SET content=$2,content_hash=$3 WHERE draft_id=$1",
        draft,
        content,
        canonical_hash(content),
    )


async def _image(pool, root, user):
    source = root / "image.png"
    source.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 30), "green").save(source)
    result = MediaPipeline(root).process_image(source, user)
    media = await pool.fetchval(
        """INSERT INTO media_assets(sha256,original_filename,detected_mime,byte_size,width,height,
           uploader_id,original_path,derivative_path)
           VALUES ($1,'test.png','image/png',$2,$3,$4,$5,$6,$7)
           RETURNING id""",
        result.sha256,
        result.bytes,
        result.width,
        result.height,
        user,
        str(result.original_path),
        str(result.derivative_path),
    )
    return media, result


@pytest.mark.asyncio
async def test_miniapp_upload_is_authenticated_durable_bounded_and_applied_by_worker(
    pg_pool, tmp_path
):
    actor, draft, _ = await _seed_case(pg_pool)
    session = await pg_pool.fetchval("SELECT session_id FROM drafts WHERE id=$1", draft)
    await pg_pool.execute(
        "INSERT INTO conversation_memory(session_id,structured_memory) VALUES ($1,$2)",
        session,
        {"fields": {}},
    )
    image = io.BytesIO()
    Image.effect_noise((512, 512), 100).convert("RGB").save(image, "PNG")
    body = image.getvalue()
    assert len(body) > 65536
    request_id = str(uuid4())

    def form(content=body, revision=1):
        data = FormData()
        data.add_field("request_id", request_id)
        data.add_field("revision", str(revision))
        data.add_field("file", content, filename="../../untrusted.png", content_type="image/png")
        return data

    headers = {
        "Authorization": "tma " + signed_init_data("test-token", telegram_id=actor.telegram_id)
    }
    app = create_miniapp_app(
        pg_pool,
        bot_token="test-token",  # noqa: S106
        dist_root=tmp_path,
        media_root=tmp_path / "media",
    )
    path = f"/api/miniapp/v1/sessions/{session}/media/media.hero"
    async with TestClient(TestServer(app)) as client:
        assert (await client.post(path, data=form())).status == 401
        assert (await client.post(path, headers=headers, data=form())).status == 202
        assert (await client.post(path, headers=headers, data=form())).status == 202
        assert await pg_pool.fetchval("SELECT count(*) FROM jobs") == 1
        assert len(list((tmp_path / "media/incoming").glob("*"))) == 1
        assert (
            await client.post(path, headers=headers, data=form(b"x" * (20 * 1024 * 1024 + 1)))
        ).status == 413
        job = await claim_job(pg_pool, worker_id="media-test")
        worker = Worker(
            pg_pool,
            extractor=FakeExtractor(ExtractedCityPatch(source_language="ru")),
            transcriber=FakeTranscriber(),
            media_pipeline=MediaPipeline(tmp_path / "media"),
        )
        await worker.process(job)
        assert await pg_pool.fetchval("SELECT status FROM jobs WHERE id=$1", job.id) == "succeeded"
        memory = await pg_pool.fetchval(
            "SELECT structured_memory FROM conversation_memory WHERE session_id=$1", session
        )
        media_id = await pg_pool.fetchval(
            "SELECT media_id FROM session_media_attachments WHERE session_id=$1", session
        )
        assert memory["fields"]["media"]["hero"] == str(media_id)
        assert (await client.post(path, headers=headers, data=form(revision=1))).status == 202
        another = FormData()
        another.add_field("request_id", str(uuid4()))
        another.add_field("revision", "1")
        another.add_field("file", body, filename="photo.png")
        assert (await client.post(path, headers=headers, data=another)).status == 409
        await pg_pool.execute(
            "UPDATE conversation_sessions SET status='completed' WHERE id=$1", session
        )
        async with pg_pool.acquire() as connection:
            with pytest.raises(ValidationBlocked, match="not active"):
                await attach_session_media(
                    connection,
                    session_id=session,
                    user_id=actor.user_id,
                    media_id=media_id,
                    field_path="media.hero",
                )


@pytest.mark.asyncio
async def test_reviewed_players_images_consent_and_repeat_projection(pg_pool, tmp_path):
    actor, draft, city = await _seed_case(pg_pool)
    session = await pg_pool.fetchval("SELECT session_id FROM drafts WHERE id=$1", draft)
    fields = (
        await pg_pool.fetchval("SELECT content FROM draft_revisions WHERE draft_id=$1", draft)
    )["fields"]
    media_id, result = await _image(pg_pool, tmp_path / "media", actor.user_id)
    fields["players"] = [
        {
            "profile_key": "player-one",
            "minor": False,
            "name": "Player",
            "bio": "Approved biography",
            "publish_permission": "да",
            "photo": str(media_id),
        }
    ]
    fields["media"] = {
        "hero": str(media_id),
        "gallery": str(media_id),
        "permission": "да",
        "minors_permission": "да",
    }
    await _set_fields(pg_pool, draft, fields)
    # Guessing an asset ID does not authorize it, and all other writes roll back.
    with pytest.raises(ValidationBlocked, match="not attached"):
        await apply_approved_trainer_draft(pg_pool, draft_id=draft, actor=actor)
    assert await pg_pool.fetchval("SELECT count(*) FROM players") == 0
    for path in ("media.hero", "media.gallery", "players.0.photo"):
        await pg_pool.execute(
            "INSERT INTO session_media_attachments(session_id,field_path,media_id) "
            "VALUES ($1,$2,$3)",
            session,
            path,
            media_id,
        )
    assert (await apply_approved_trainer_draft(pg_pool, draft_id=draft, actor=actor)).applied
    assert not (await apply_approved_trainer_draft(pg_pool, draft_id=draft, actor=actor)).applied
    payload = (await project_city_payload(pg_pool, generated_at=datetime.now(UTC))).model_dump(
        mode="json"
    )
    projected = payload["cities"][0]
    assert projected["hero"] == f"/assets/content/{result.sha256}.webp"
    assert projected["players_list"][0]["id"] == "player-one"
    assert len(projected["gallery"]) == 1
    publisher = GitPublisher(
        pg_pool, tmp_path, tmp_path / "worktrees", media_root=tmp_path / "media"
    )
    bundle_root = tmp_path / "app/src/data/generated"
    bundle_root.mkdir(parents=True)
    (bundle_root / "federation-content.json").write_text('{"federation":{}}')
    (bundle_root / "news-content.json").write_text('{"items":[]}')
    await publisher._materialize_content_assets(tmp_path, payload)
    assert (
        tmp_path / "app/public" / projected["hero"].lstrip("/")
    ).read_bytes() == result.derivative_path.read_bytes()
    player = await pg_pool.fetchval("SELECT id FROM players")
    await pg_pool.execute(
        "INSERT INTO consents(subject_type,subject_id,scope,status) "
        "VALUES ('player',$1,'name_bio','withdrawn')",
        player,
    )
    await pg_pool.execute(
        "INSERT INTO consents(subject_type,subject_id,scope,status) "
        "VALUES ('media',$1,'media_publication','withdrawn')",
        media_id,
    )
    revoked = (await project_city_payload(pg_pool, generated_at=datetime.now(UTC))).model_dump(
        mode="json"
    )
    assert revoked["cities"][0]["players_list"] == []
    assert revoked["cities"][0]["gallery"] == []
    assert revoked["cities"][0]["hero"] == "/assets/heroes/clubs.png"
    with pytest.raises(ValidationBlocked, match="lost approval"):
        await publisher._materialize_content_assets(tmp_path, payload)
    await publisher._materialize_content_assets(tmp_path, revoked)
    assert not (tmp_path / "app/public" / projected["hero"].lstrip("/")).exists()


@pytest.mark.asyncio
async def test_leadership_portrait_uses_reviewed_attachment_and_preserves_translation(
    pg_pool, tmp_path
):
    draft, actor = await seed(pg_pool, "leadership")
    session = await pg_pool.fetchval("SELECT session_id FROM drafts WHERE id=$1", draft)
    media_id, result = await _image(pg_pool, tmp_path, actor.user_id)
    fields = (
        await pg_pool.fetchval("SELECT content FROM draft_revisions WHERE draft_id=$1", draft)
    )["fields"]
    profile = fields["profiles"][0]
    profile.update(
        portrait=str(media_id), portrait_rightsholder="Author", profile_portrait_permission="да"
    )
    await _set_fields(pg_pool, draft, fields)
    await pg_pool.execute(
        "INSERT INTO session_media_attachments(session_id,field_path,media_id) VALUES ($1,$2,$3)",
        session,
        "profiles.0.portrait",
        media_id,
    )
    await apply_approved_federation_draft(pg_pool, draft_id=draft, actor=actor)
    public = await project_federation_payload(pg_pool, generated_at=datetime.now(UTC))
    assert public.federation.leadership[0].photo == f"/assets/content/{result.sha256}.webp"
    assert public.federation.leadership[0].email == ""
    async with pg_pool.acquire() as connection, connection.transaction():
        with pytest.raises(ValidationBlocked, match="not attached"):
            await reviewed_media(
                connection,
                session_id=session,
                field_path="profiles.1.portrait",
                value=str(media_id),
                granted=True,
                actor_id=actor.user_id,
                author_id=actor.user_id,
                draft_id=draft,
            )


@pytest.mark.asyncio
async def test_only_verified_public_current_pdfs_are_exported_and_bytes_are_checked(
    pg_pool, tmp_path
):
    actor, _, _ = await _seed_case(pg_pool)
    root = tmp_path / "media/official_documents"
    root.mkdir(parents=True)
    for index, (status, allowed, expiry) in enumerate(
        [
            ("verified", True, None),
            ("received", True, None),
            ("verified", False, None),
            ("verified", True, "2020-01-01"),
        ]
    ):
        body = f"%PDF-1.4\nDocument {index}\n%%EOF".encode()
        digest = hashlib.sha256(body).hexdigest()
        path = root / f"{digest}.pdf"
        path.write_bytes(body)
        await pg_pool.execute(
            """INSERT INTO official_documents(requirement_code,title_ru,original_filename,
               byte_size,sha256,
               original_path,publication_allowed,status,uploaded_by,reviewed_by,reviewed_at,valid_until)
               VALUES ('game_rules','Правила','rules.pdf',
                       $1,$2,$3,$4,$5,$6,$6,now(),$7::text::date)""",
            len(body),
            digest,
            str(path),
            allowed,
            status,
            actor.user_id,
            expiry,
        )
    payload = (
        await project_federation_payload(pg_pool, generated_at=datetime.now(UTC))
    ).model_dump(mode="json")
    assert len(payload["federation"]["documents"]) == 1
    document = payload["federation"]["documents"][0]
    publisher = GitPublisher(
        pg_pool, tmp_path, tmp_path / "worktrees", media_root=tmp_path / "media"
    )
    generated = tmp_path / "app/src/data/generated"
    generated.mkdir(parents=True)
    (generated / "city-content.json").write_text('{"cities":[]}')
    (generated / "news-content.json").write_text('{"items":[]}')
    await publisher._materialize_content_assets(tmp_path, payload)
    target = tmp_path / "app/public" / document["url"].lstrip("/")
    assert hashlib.sha256(target.read_bytes()).hexdigest() == document["sha256"]
    (root / f"{document['sha256']}.pdf").write_bytes(b"tampered")
    with pytest.raises(ValidationBlocked, match="bytes changed"):
        await publisher._materialize_content_assets(tmp_path, payload)


def test_old_dialogue_pins_still_load_after_new_versions_are_selected():
    repository = DialogueSpecRepository()
    for workflow in ("trainer", "leadership"):
        old = repository._load_path(workflow, repository.root / f"{workflow}.v1.json")
        assert repository.load(workflow, sha256=old.sha256) == old
        assert repository.load(workflow).sha256 != old.sha256


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow", ["trainer", "leadership"])
async def test_miniapp_new_version_pauses_old_session_and_preserves_answers(
    pg_pool, tmp_path, workflow
):
    repository = DialogueSpecRepository()
    old = repository._load_path(workflow, repository.root / f"{workflow}.v1.json")
    current = repository.load(workflow)
    user = await pg_pool.fetchval(
        "INSERT INTO users(telegram_id,display_name) VALUES (98765,'Editor') RETURNING id"
    )
    await pg_pool.execute(
        "INSERT INTO user_roles(user_id,role_name) VALUES ($1,'superadmin')", user
    )
    old_session = await pg_pool.fetchval(
        "INSERT INTO conversation_sessions(user_id,workflow,definition_version,definition_hash) "
        "VALUES ($1,$2,$3,$4) RETURNING id",
        user,
        workflow,
        old.spec.version,
        old.sha256,
    )
    memory = {"fields": {}, "skipped": [], "note": "preserve old answers"}
    await pg_pool.execute(
        "INSERT INTO conversation_memory(session_id,structured_memory) VALUES ($1,$2)",
        old_session,
        memory,
    )
    app = create_miniapp_app(pg_pool, bot_token="test-token", dist_root=tmp_path)  # noqa: S106
    headers = {"Authorization": "tma " + signed_init_data("test-token", telegram_id=98765)}
    async with TestClient(TestServer(app)) as client:
        response = await client.post(f"/api/miniapp/v1/sessions/{workflow}", headers=headers)
        assert response.status == 201
        assert (
            await pg_pool.fetchval(
                "SELECT status FROM conversation_sessions WHERE id=$1", old_session
            )
            == "paused"
        )
        assert (
            await pg_pool.fetchval(
                "SELECT structured_memory FROM conversation_memory WHERE session_id=$1", old_session
            )
            == memory
        )
        active = await pg_pool.fetch(
            "SELECT definition_hash FROM conversation_sessions "
            "WHERE user_id=$1 AND status='active'",
            user,
        )
        assert len(active) == 1
        assert active[0]["definition_hash"] == current.sha256
        assert (
            await client.post(f"/api/miniapp/v1/sessions/{workflow}", headers=headers)
        ).status == 201
        assert (
            await pg_pool.fetchval(
                "SELECT count(*) FROM conversation_sessions WHERE user_id=$1", user
            )
            == 2
        )


@pytest.mark.asyncio
async def test_new_drafts_update_stable_players_clubs_and_require_guardian_consent(pg_pool):
    actor, draft, city = await _seed_case(pg_pool)
    session = await pg_pool.fetchval("SELECT session_id FROM drafts WHERE id=$1", draft)
    fields = (
        await pg_pool.fetchval("SELECT content FROM draft_revisions WHERE draft_id=$1", draft)
    )["fields"]
    fields["players"] = [
        {
            "profile_key": "stable-player",
            "name": "Name",
            "bio": "Biography",
            "minor": True,
            "guardian_permission": "нет",
            "publish_permission": "да",
        }
    ]
    await _set_fields(pg_pool, draft, fields)
    await apply_approved_trainer_draft(pg_pool, draft_id=draft, actor=actor)
    first_player = await pg_pool.fetchval("SELECT id FROM players")
    public = await project_city_payload(pg_pool, generated_at=datetime.now(UTC))
    assert public.cities[0].players_list == []
    # A new approved draft in the same city replaces bot-owned records, rather
    # than creating a second club/schedule and a renamed duplicate person.
    next_draft = await pg_pool.fetchval(
        """INSERT INTO drafts(session_id,entity_type,city_id,status,
                           approved_revision,created_by,updated_by)
           VALUES ($1,'city',$2,'approved',1,$3,$3) RETURNING id""",
        session,
        city,
        actor.user_id,
    )
    fields["players"][0].update(name="Corrected name", guardian_permission="да")
    fields["clubs"][0]["name"] = "Renamed club"
    content = await pg_pool.fetchval("SELECT content FROM draft_revisions WHERE draft_id=$1", draft)
    content["fields"] = fields
    await pg_pool.execute(
        "INSERT INTO draft_revisions(draft_id,revision,content,content_hash,created_by) "
        "VALUES ($1,1,$2,$3,$4)",
        next_draft,
        content,
        canonical_hash(content),
        actor.user_id,
    )
    await apply_approved_trainer_draft(pg_pool, draft_id=next_draft, actor=actor)
    assert await pg_pool.fetchval("SELECT count(*) FROM players") == 1
    assert await pg_pool.fetchval("SELECT id FROM players") == first_player
    assert await pg_pool.fetchval("SELECT count(*) FROM clubs WHERE status='active'") == 1
    assert await pg_pool.fetchval("SELECT count(*) FROM training_schedules WHERE active") == 1
    public = await project_city_payload(pg_pool, generated_at=datetime.now(UTC))
    assert public.cities[0].players_list[0].nameRu == "Corrected name"
    latest = await pg_pool.fetchrow(
        "SELECT minor,guardian_confirmed FROM consents WHERE scope='name_bio' "
        "ORDER BY updated_at DESC LIMIT 1"
    )
    assert latest["minor"] and latest["guardian_confirmed"]


@pytest.mark.asyncio
async def test_async_photo_cannot_follow_a_different_reordered_profile(pg_pool, tmp_path):
    actor, draft, _ = await _seed_case(pg_pool)
    session = await pg_pool.fetchval("SELECT session_id FROM drafts WHERE id=$1", draft)
    await pg_pool.execute(
        "INSERT INTO conversation_memory(session_id,structured_memory) VALUES ($1,$2)",
        session,
        {"fields": {"players": [{"profile_key": "second", "name": "Second"}]}},
    )
    media, _ = await _image(pg_pool, tmp_path, actor.user_id)
    async with pg_pool.acquire() as connection, connection.transaction():
        with pytest.raises(ValidationBlocked, match="profile changed"):
            await attach_session_media(
                connection,
                session_id=session,
                user_id=actor.user_id,
                media_id=media,
                field_path="players.0.photo",
                expected_record={"profile_key": "first", "name": "First", "full_name": None},
            )
    assert await pg_pool.fetchval("SELECT count(*) FROM session_media_attachments") == 0


@pytest.mark.asyncio
async def test_document_can_be_privately_viewed_verified_and_withdrawn_from_telegram(
    pg_pool, tmp_path
):
    import asyncio
    from types import SimpleNamespace

    from aiogram.types import Chat, Message, Update, User

    from floorball_bot.official_documents import store_official_pdf
    from floorball_bot.telegram import TelegramIngress, run_outbox

    actor, _, _ = await _seed_case(pg_pool)
    media_root = tmp_path / "media"
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.4\nReviewed rules\n%%EOF")
    stored = store_official_pdf(source, media_root, actor.user_id)
    document = await pg_pool.fetchval(
        """INSERT INTO official_documents(requirement_code,title_ru,original_filename,byte_size,
           sha256,original_path,publication_allowed,status,uploaded_by)
           VALUES ('game_rules','Правила','rules.pdf',$1,$2,$3,TRUE,'received',$4) RETURNING id""",
        stored.byte_size,
        stored.sha256,
        str(stored.original_path),
        actor.user_id,
    )
    ingress = TelegramIngress(object(), pg_pool, download_root=media_root / "incoming")

    async def command(text):
        update_id = 1_000_000_000 + uuid4().int % 1_000_000_000
        return await ingress.accept(
            Update(
                update_id=update_id,
                message=Message(
                    message_id=update_id,
                    date=datetime.now(UTC),
                    chat=Chat(id=actor.telegram_id, type="private"),
                    from_user=User(id=actor.telegram_id, first_name="Editor", is_bot=False),
                    text=text,
                ),
            )
        )

    assert await command(f"/document_view {document}")
    event = await pg_pool.fetchrow("SELECT * FROM outbox_events WHERE payload ? 'document_id'")
    assert event["payload"]["document_id"] == str(document)
    await pg_pool.execute("DELETE FROM outbox_events WHERE NOT (payload ? 'document_id')")
    stop = asyncio.Event()

    class DocumentBot:
        async def send_document(self, *, chat_id, document):
            assert chat_id == actor.telegram_id
            assert Path(document.path).read_bytes() == source.read_bytes()
            stop.set()
            return SimpleNamespace(message_id=999)

    await run_outbox(DocumentBot(), pg_pool, "document-view-test", stop, media_root=media_root)
    assert (
        await pg_pool.fetchval("SELECT status FROM outbox_events WHERE id=$1", event["id"])
        == "sent"
    )
    assert await command(f"/document_verify {document} wrong-checksum")
    assert (
        await pg_pool.fetchval("SELECT status FROM official_documents WHERE id=$1", document)
        == "received"
    )
    assert await command(f"/document_verify {document} {stored.sha256}")
    public = await project_federation_payload(pg_pool, generated_at=datetime.now(UTC))
    assert len(public.federation.documents) == 1
    assert await command(f"/document_verify {document} {stored.sha256}")
    assert (
        await pg_pool.fetchval(
            "SELECT count(*) FROM official_document_events WHERE action='verified'"
        )
        == 1
    )
    assert await command(f"/document_reject {document} {stored.sha256}")
    public = await project_federation_payload(pg_pool, generated_at=datetime.now(UTC))
    assert public.federation.documents == []
