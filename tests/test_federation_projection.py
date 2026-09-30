from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest

from floorball_bot.auth import provision_telegram_user
from floorball_bot.config import Settings
from floorball_bot.db import create_pool, run_migrations
from floorball_bot.dialogue.patches import ExtractedDialoguePatch, canonical_context
from floorball_bot.dialogue.repository import DialogueSpecRepository
from floorball_bot.domain import Actor, PublicFederation, Role
from floorball_bot.errors import AuthorizationError, RetryableProviderError
from floorball_bot.projection.apply import ProjectionRejected
from floorball_bot.projection.federation import (
    apply_approved_federation_draft,
    plan_federation_projection,
)
from floorball_bot.providers.transcription import FakeTranscriber
from floorball_bot.queue import ClaimedJob
from floorball_bot.worker import Worker
from floorball_bot.workflow import canonical_hash


def fields_for(workflow, language="ru"):
    base = {
        "respondent": {"name": "Editor", "position": "Representative", "contact": "private"},
        "source_language": language,
        "text_consent": True,
    }
    if workflow == "strategy":
        base.update(mission="Mission", vision="Vision")
    elif workflow == "history":
        base["history_entries"] = [
            {
                "period": "2020",
                "title": "Event",
                "description": "Verified fact",
                "source_url": "https://example.kz/source",
            }
        ]
    else:
        base.update(
            media_rights_consent=True,
            profiles=[
                {
                    "profile_key": "president",
                    "name": "Leader",
                    "role": "President",
                    "bio": "Verified bio",
                    "focus": "Clubs",
                    "email": "private@example.kz",
                    "public_contacts": "нет",
                    "profile_portrait_permission": "нет",
                }
            ],
        )
    return base


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_cli_restores_revoked_role_and_city_scope(federation_pool, monkeypatch):
    from floorball_bot import cli

    # The fixture verifies the actual database name before any mutations.
    monkeypatch.setattr(
        cli, "get_settings", lambda: Settings(POSTGRES_DSN=os.environ["TEST_POSTGRES_DSN"])
    )
    user = await provision_telegram_user(
        federation_pool, telegram_id=12345678, display_name="Scoped editor"
    )
    city = await federation_pool.fetchval(
        "INSERT INTO cities(slug,name_ru) VALUES ($1,'Test city') RETURNING id",
        "test-" + uuid4().hex[:12],
    )
    await federation_pool.execute(
        "INSERT INTO user_city_scopes(user_id,city_id,revoked_at) VALUES ($1,$2,now())",
        user,
        city,
    )
    await federation_pool.execute(
        "INSERT INTO user_roles(user_id,role_name,revoked_at) VALUES ($1,'city_coach',now())",
        user,
    )
    await cli.async_main(
        cli.parser().parse_args(
            [
                "scope-city",
                "--user",
                str(user),
                "--city",
                str(city),
            ]
        )
    )
    await cli.async_main(
        cli.parser().parse_args(
            [
                "grant-role",
                "--user",
                str(user),
                "--role",
                "city_coach",
            ]
        )
    )
    assert await federation_pool.fetchval(
        "SELECT revoked_at IS NULL FROM user_city_scopes WHERE user_id=$1 AND city_id=$2",
        user,
        city,
    )
    assert await federation_pool.fetchval(
        "SELECT revoked_at IS NULL FROM user_roles WHERE user_id=$1 AND role_name='city_coach'",
        user,
    )


def test_strategy_keeps_approved_other_language_and_private_fields_out():
    existing = PublicFederation().model_dump(mode="json")
    existing["mission"]["statementKz"] = "Approved KZ"
    result = plan_federation_projection("strategy", fields_for("strategy"), existing)
    assert result["mission"]["statementRu"] == "Mission"
    assert result["mission"]["statementKz"] == "Approved KZ"
    assert "private" not in str(result)
    assert existing["mission"]["statementRu"] == ""


def test_history_languages_align_without_duplicating_events():
    before = PublicFederation().model_dump(mode="json")
    ru = plan_federation_projection("history", fields_for("history"), before)
    kz_fields = fields_for("history", "kz")
    kz_fields["history_entries"][0]["title"] = "KZ Event"
    kz = plan_federation_projection("history", kz_fields, ru)
    assert len(kz["history"]) == 1
    assert kz["history"][0]["titleRu"] == "Event"
    assert kz["history"][0]["titleKz"] == "KZ Event"


def test_history_rejects_ambiguous_events_instead_of_silently_losing_one():
    fields = fields_for("history")
    fields["history_entries"].append(
        {
            **fields["history_entries"][0],
            "title": "Different event with the same identity",
        }
    )
    with pytest.raises(ProjectionRejected, match="ambiguous duplicate"):
        plan_federation_projection("history", fields, PublicFederation().model_dump(mode="json"))


def test_leadership_requires_text_consent_and_redacts_private_contact():
    before = PublicFederation().model_dump(mode="json")
    fields = fields_for("leadership")
    result = plan_federation_projection("leadership", fields, before)
    assert result["leadership"][0]["email"] == ""
    fields["text_consent"] = False
    with pytest.raises(ProjectionRejected, match="consent"):
        plan_federation_projection("leadership", fields, before)


@pytest.fixture
async def federation_pool():
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not configured")
    pool = await create_pool(dsn)
    if not (await pool.fetchval("SELECT current_database()")).endswith("_test"):
        await pool.close()
        raise RuntimeError("refusing a non-test database")
    await run_migrations(pool, Path(__file__).parents[1] / "migrations")
    await pool.execute(
        "TRUNCATE users, federation_sections, leadership_profiles RESTART IDENTITY CASCADE"
    )
    yield pool
    await pool.close()


async def seed(pool, workflow):
    telegram_id = uuid4().int % 10**9 + 1
    user_id = await pool.fetchval(
        "INSERT INTO users(telegram_id,display_name) VALUES ($1,'Editor') RETURNING id", telegram_id
    )
    await pool.execute(
        "INSERT INTO user_roles(user_id,role_name) VALUES ($1,'superadmin')", user_id
    )
    loaded = DialogueSpecRepository().load(workflow)
    session = await pool.fetchval(
        """
        INSERT INTO conversation_sessions(
            user_id,workflow,definition_version,definition_hash,context_hash
        )
        VALUES ($1,$2,$3,$4,$5) RETURNING id
        """,
        user_id,
        workflow,
        loaded.spec.version,
        loaded.sha256,
        "a" * 64,
    )
    draft = await pool.fetchval(
        """
        INSERT INTO drafts(session_id,entity_type,status,approved_revision,created_by,updated_by)
        VALUES ($1,'federation','approved',1,$2,$2) RETURNING id
        """,
        session,
        user_id,
    )
    content = {
        "dialogue_mode": workflow,
        "definition_version": loaded.spec.version,
        "definition_hash": loaded.sha256,
        "context_hash": "a" * 64,
        "fields": fields_for(workflow),
    }
    await pool.execute(
        """
        INSERT INTO draft_revisions(draft_id,revision,content,content_hash,created_by)
        VALUES ($1,1,$2::jsonb,$3,$4)
        """,
        draft,
        content,
        canonical_hash(content),
        user_id,
    )
    return draft, Actor(
        user_id=user_id,
        telegram_id=telegram_id,
        roles=frozenset({Role.SUPERADMIN}),
        city_scopes=frozenset(),
    )


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("workflow", ["strategy", "history", "leadership"])
async def test_approved_federation_projection_is_atomic_and_idempotent(federation_pool, workflow):
    draft, actor = await seed(federation_pool, workflow)
    results = await asyncio.gather(
        *[
            apply_approved_federation_draft(federation_pool, draft_id=draft, actor=actor)
            for _ in range(2)
        ]
    )
    assert sorted(r.applied for r in results) == [False, True]
    assert results[0].application_id == results[1].application_id
    assert (
        await federation_pool.fetchval("SELECT count(*) FROM federation_projection_applications")
        == 1
    )
    if workflow == "leadership":
        row = await federation_pool.fetchrow("SELECT * FROM leadership_profiles")
        assert row["name_ru"] == "Leader"
        assert row["contacts_are_public"] is False
        assert row["email_private"] == "private@example.kz"
    else:
        assert await federation_pool.fetchval("SELECT count(*) FROM federation_sections") > 0


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_federation_projection_rejects_changed_revision_without_writes(federation_pool):
    draft, actor = await seed(federation_pool, "strategy")
    await federation_pool.execute("UPDATE draft_revisions SET content_hash=$1", "b" * 64)
    with pytest.raises(ProjectionRejected, match="pins"):
        await apply_approved_federation_draft(federation_pool, draft_id=draft, actor=actor)
    assert await federation_pool.fetchval("SELECT count(*) FROM federation_sections") == 0


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_extractor_does_not_overwrite_concurrent_miniapp_edit(federation_pool):
    draft, actor = await seed(federation_pool, "strategy")
    session = await federation_pool.fetchval("SELECT session_id FROM drafts WHERE id=$1", draft)
    await federation_pool.execute(
        "INSERT INTO conversation_memory(session_id,structured_memory) VALUES ($1,$2::jsonb)",
        session,
        {"fields": {"mission": "Original"}},
    )

    class ConcurrentEditExtractor:
        async def extract(
            self, text, output_model, *, mode, context, known_fields, spec_sha256=None
        ):
            await federation_pool.execute(
                "UPDATE conversation_memory SET structured_memory=$2::jsonb,revision=revision+1 "
                "WHERE session_id=$1",
                session,
                {"fields": {"mission": "Edited in Mini App"}},
            )
            return ExtractedDialoguePatch(
                mode=mode,
                spec_sha256=DialogueSpecRepository().load(mode).sha256,
                context_sha256=canonical_context(context)[1],
                fields=[],
            )

    worker = Worker(
        federation_pool, extractor=ConcurrentEditExtractor(), transcriber=FakeTranscriber()
    )
    job = ClaimedJob(
        id=uuid4(),
        kind="extract",
        attempts=1,
        max_attempts=5,
        idempotency_key="race-test",
        payload={"session_id": str(session), "mode": "strategy", "text": "Text", "chat_id": 1},
    )
    with pytest.raises(RetryableProviderError, match="edited during extraction"):
        await worker._extract_dialogue(job)
    memory = await federation_pool.fetchval(
        "SELECT structured_memory FROM conversation_memory WHERE session_id=$1", session
    )
    assert memory["fields"]["mission"] == "Edited in Mini App"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_telegram_provisioning_grants_no_roles_and_preserves_disabled_account(
    federation_pool,
):
    async with federation_pool.acquire() as connection:
        user = await provision_telegram_user(connection, telegram_id=123456, display_name="Editor")
        again = await provision_telegram_user(
            connection, telegram_id=123456, display_name="Editor 2"
        )
        assert user == again
        assert await connection.fetchval("SELECT count(*) FROM user_roles") == 0
        await connection.execute("UPDATE users SET active=FALSE WHERE id=$1", user)
        with pytest.raises(AuthorizationError, match="inactive"):
            await provision_telegram_user(connection, telegram_id=123456, display_name="Reactivate")
