from __future__ import annotations

import argparse
import asyncio
import json
import signal
from collections.abc import Coroutine
from pathlib import Path
from typing import Any
from uuid import UUID

from aiogram import Bot
from aiohttp import web

from floorball_bot.city_applications import initialize_city_application
from floorball_bot.config import get_settings
from floorball_bot.contact_api import create_contact_app
from floorball_bot.context_gateway import (
    AgentContextGateway,
    AgentMode,
    context_schema_catalog,
    load_context_actor,
)
from floorball_bot.db import create_pool, run_migrations
from floorball_bot.health import health_report
from floorball_bot.importers import (
    apply_city_import,
    apply_federation_import,
    latest_import_hashes,
    read_city_bundle,
    read_federation_bundle,
    reconcile,
)
from floorball_bot.logging import configure_logging
from floorball_bot.media import MediaPipeline
from floorball_bot.media_consent import reconcile_withdrawn_media
from floorball_bot.projection.apply import (
    apply_approved_trainer_draft,
    inspect_trainer_draft,
)
from floorball_bot.providers.codex import CodexExtractor
from floorball_bot.providers.transcription import (
    AssemblyAIBatchTranscriber,
    AssemblyAIWhisperStreamingTranscriber,
    RoutedAssemblyAITranscriber,
)
from floorball_bot.publisher import GitPublisher
from floorball_bot.smtp_mailer import SmtpContactMailer
from floorball_bot.telegram import TelegramIngress, run_outbox
from floorball_bot.worker import Worker


async def supervise_bot_tasks(
    *,
    ingress: TelegramIngress,
    outbox_coro: Coroutine[Any, Any, None],
    stop: asyncio.Event,
) -> None:
    """Fail the process if a critical bot loop stops unexpectedly."""
    ingress_task = asyncio.create_task(ingress.run(), name="telegram-ingress")
    outbox_task = asyncio.create_task(outbox_coro, name="telegram-outbox")
    stop_task = asyncio.create_task(stop.wait(), name="telegram-stop")
    critical = {ingress_task, outbox_task}
    tasks = critical | {stop_task}
    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if stop_task in done:
            await ingress.stop()
            await asyncio.gather(*critical)
            return
        failed = next(task for task in done if task in critical)
        if failed.cancelled():
            raise RuntimeError(f"critical task {failed.get_name()} was cancelled")
        error = failed.exception()
        if error is not None:
            raise RuntimeError(f"critical task {failed.get_name()} failed") from error
        raise RuntimeError(f"critical task {failed.get_name()} stopped unexpectedly")
    finally:
        stop.set()
        await ingress.stop()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="floorball-bot")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    commands.add_parser("bot")
    commands.add_parser("worker")
    commands.add_parser("contact-api")
    commands.add_parser("health")
    commands.add_parser("media-consent-reconcile")
    context = commands.add_parser("agent-context")
    context.add_argument("--actor", required=True, type=UUID)
    context.add_argument("--mode", required=True, choices=[mode.value for mode in AgentMode])
    context.add_argument("--city")
    commands.add_parser("context-schema")
    report = commands.add_parser("import-report")
    report.add_argument("path", type=Path)
    report.add_argument("--existing", type=Path)
    city_import = commands.add_parser("import-city")
    city_import.add_argument("path", type=Path)
    city_import.add_argument("--apply", action="store_true")
    federation_import = commands.add_parser("import-federation")
    federation_import.add_argument("path", type=Path)
    federation_import.add_argument("--apply", action="store_true")
    user = commands.add_parser("create-user")
    user.add_argument("--phone", required=True)
    user.add_argument("--name", required=True)
    role = commands.add_parser("grant-role")
    role.add_argument("--user", required=True, type=UUID)
    role.add_argument("--role", required=True)
    scope = commands.add_parser("scope-city")
    scope.add_argument("--user", required=True, type=UUID)
    scope.add_argument("--city", required=True, type=UUID)
    city_initialize = commands.add_parser("city-initialize")
    city_initialize.add_argument("--application", required=True, type=UUID)
    city_initialize.add_argument("--actor", required=True, type=UUID)
    city_initialize.add_argument("--apply", action="store_true")
    project_trainer = commands.add_parser("project-trainer")
    project_trainer.add_argument("--draft", required=True, type=UUID)
    project_trainer.add_argument("--actor", required=True, type=UUID)
    project_trainer.add_argument("--apply", action="store_true")
    preview = commands.add_parser("publish-preview")
    preview.add_argument("--draft", required=True, type=UUID)
    preview.add_argument("--actor", required=True, type=UUID)
    confirm = commands.add_parser("publish-confirm")
    confirm.add_argument("--publication", required=True, type=UUID)
    confirm.add_argument("--nonce", required=True)
    confirm.add_argument("--actor", required=True, type=UUID)
    reconcile = commands.add_parser("publish-reconcile")
    reconcile.add_argument("--publication", required=True, type=UUID)
    return root


async def async_main(args: argparse.Namespace) -> None:
    if args.command == "context-schema":
        print(
            json.dumps(
                context_schema_catalog().model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    settings = get_settings()
    pool = await create_pool(settings.postgres_dsn.get_secret_value())
    try:
        if args.command == "migrate":
            applied = await run_migrations(pool, Path(__file__).resolve().parents[2] / "migrations")
            print(json.dumps({"applied": applied}, ensure_ascii=False))
        elif args.command == "health":
            report = await health_report(pool, settings.media_root, settings.backup_root)
            print(
                json.dumps(report, ensure_ascii=False, default=str)
            )
            if not report["ok"]:
                raise SystemExit(1)
        elif args.command == "media-consent-reconcile":
            result = await reconcile_withdrawn_media(pool, media_root=settings.media_root)
            print(
                json.dumps(
                    {
                        "removed_count": len(result.removed_media_ids),
                        "removed_media_ids": [
                            str(media_id) for media_id in result.removed_media_ids
                        ],
                    }
                )
            )
        elif args.command == "agent-context":
            async with pool.acquire() as connection:
                actor = await load_context_actor(connection, args.actor)
            if actor is None or not actor.active:
                raise RuntimeError("agent-context requires an active actor")
            snapshot = await AgentContextGateway(pool).snapshot(
                actor=actor,
                mode=AgentMode(args.mode),
                city_slug=args.city,
            )
            print(
                json.dumps(
                    snapshot.model_dump(mode="json"), ensure_ascii=False, indent=2
                )
            )
        elif args.command == "bot":
            token = settings.require_telegram_token()
            bot = Bot(token)
            ingress = TelegramIngress(
                bot,
                pool,
                poll_timeout=settings.poll_timeout_seconds,
                download_root=settings.media_root / "incoming",
                max_download_bytes=settings.max_download_bytes,
            )
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop.set)
            try:
                await supervise_bot_tasks(
                    ingress=ingress,
                    outbox_coro=run_outbox(bot, pool, "bot-outbox", stop),
                    stop=stop,
                )
            finally:
                await bot.session.close()
        elif args.command == "worker":
            key = settings.require_assemblyai_key()
            transcriber = RoutedAssemblyAITranscriber(
                AssemblyAIBatchTranscriber(key, settings.assemblyai_timeout_seconds),
                AssemblyAIWhisperStreamingTranscriber(key, settings.assemblyai_timeout_seconds),
            )
            worker = Worker(
                pool,
                extractor=CodexExtractor(settings.codex_cli, settings.codex_timeout_seconds),
                transcriber=transcriber,
                media_pipeline=MediaPipeline(
                    settings.media_root,
                    max_input_bytes=settings.max_download_bytes,
                    max_pixels=settings.max_image_pixels,
                    max_derivative_bytes=settings.max_derivative_bytes,
                ),
                publisher=GitPublisher(
                    pool,
                    settings.floorball_site_repo,
                    settings.worktree_root,
                    publish_enabled=settings.publish_enabled,
                    media_root=settings.media_root,
                ),
                contact_mailer=(
                    SmtpContactMailer(
                        host=settings.smtp_host,
                        port=settings.smtp_port,
                        username=settings.smtp_username,
                        password=settings.smtp_password.get_secret_value(),
                        use_ssl=settings.smtp_use_ssl,
                    )
                    if settings.smtp_configured()
                    else None
                ),
                lease_seconds=settings.job_lease_seconds,
            )
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, lambda: asyncio.create_task(worker.stop()))
            await worker.run()
        elif args.command == "contact-api":
            app = create_contact_app(
                pool,
                fingerprint_secret=settings.require_contact_api_secret(),
                allowed_origins=settings.allowed_contact_origins(),
            )
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            site = web.TCPSite(
                runner,
                host=settings.contact_api_host,
                port=settings.contact_api_port,
            )
            await site.start()
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop.set)
            try:
                await stop.wait()
            finally:
                await runner.cleanup()
        elif args.command == "import-report":
            incoming = read_city_bundle(args.path)
            existing = {}
            if args.existing:
                existing = {
                    record.entity_key: record.content_hash
                    for record in read_city_bundle(args.existing)
                }
            print(json.dumps(reconcile(existing, incoming), ensure_ascii=False, indent=2))
        elif args.command == "import-city":
            incoming = read_city_bundle(args.path)
            existing = await latest_import_hashes(
                pool, source="google_forms_import", entity_type="city"
            )
            report = reconcile(existing, incoming)
            if args.apply:
                async with pool.acquire() as connection, connection.transaction():
                    report["imported"] = await apply_city_import(connection, incoming)
            print(json.dumps(report, ensure_ascii=False, indent=2))
        elif args.command == "import-federation":
            incoming = read_federation_bundle(args.path)
            existing = await latest_import_hashes(
                pool, source="federation_bundle_import", entity_type="federation_section"
            )
            report = reconcile(existing, incoming)
            if args.apply:
                async with pool.acquire() as connection, connection.transaction():
                    report["imported"] = await apply_federation_import(connection, incoming)
            print(json.dumps(report, ensure_ascii=False, indent=2))
        elif args.command == "create-user":
            from floorball_bot.domain import normalize_phone

            user_id = await pool.fetchval(
                """
                INSERT INTO users(phone_e164, display_name) VALUES ($1,$2)
                ON CONFLICT (phone_e164) DO UPDATE SET display_name=EXCLUDED.display_name,
                    updated_at=now() RETURNING id
                """,
                normalize_phone(args.phone),
                args.name,
            )
            print(user_id)
        elif args.command == "grant-role":
            await pool.execute(
                "INSERT INTO user_roles(user_id, role_name) VALUES ($1,$2) ON CONFLICT DO NOTHING",
                args.user,
                args.role,
            )
            print("ok")
        elif args.command == "scope-city":
            await pool.execute(
                """
                INSERT INTO user_city_scopes(user_id, city_id) VALUES ($1,$2)
                ON CONFLICT DO UPDATE SET revoked_at=NULL, granted_at=now()
                """,
                args.user,
                args.city,
            )
            print("ok")
        elif args.command == "city-initialize":
            async with pool.acquire() as connection:
                actor = await load_context_actor(connection, args.actor)
            if actor is None or not actor.active:
                raise RuntimeError("city-initialize requires an active superadmin")
            result = await initialize_city_application(
                pool,
                application_id=args.application,
                actor=actor,
                apply=args.apply,
            )
            print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
        elif args.command == "project-trainer":
            async with pool.acquire() as connection:
                actor = await load_context_actor(connection, args.actor)
            if actor is None or not actor.active:
                raise RuntimeError("project-trainer requires an active reviewer")
            result = (
                await apply_approved_trainer_draft(
                    pool, draft_id=args.draft, actor=actor
                )
                if args.apply
                else await inspect_trainer_draft(
                    pool, draft_id=args.draft, actor=actor
                )
            )
            print(
                json.dumps(
                    result.model_dump(mode="json"), ensure_ascii=False, indent=2
                )
            )
        elif args.command == "publish-preview":
            row = await pool.fetchrow(
                """
                SELECT d.id, d.approved_revision, r.content, r.content_hash
                FROM drafts d
                JOIN draft_revisions r ON r.draft_id=d.id AND r.revision=d.approved_revision
                WHERE d.id=$1 AND d.status='approved'
                """,
                args.draft,
            )
            if not row:
                raise RuntimeError("draft must have an approved revision")
            publication_id = await pool.fetchval(
                """
                INSERT INTO publication_jobs(
                    draft_id, revision, revision_hash, requested_by
                ) VALUES ($1,$2,$3,$4) RETURNING id
                """,
                args.draft,
                row["approved_revision"],
                row["content_hash"],
                args.actor,
            )
            publisher = GitPublisher(
                pool,
                settings.floorball_site_repo,
                settings.worktree_root,
                publish_enabled=settings.publish_enabled,
                media_root=settings.media_root,
            )
            preview_result = await publisher.build_preview(publication_id, row["content"])
            print(
                json.dumps(
                    {
                        "publication_id": str(preview_result.publication_id),
                        "base_commit": preview_result.base_commit,
                        "revision_hash": preview_result.revision_hash,
                        "nonce": preview_result.nonce,
                        "diff_summary": preview_result.diff_summary,
                        "screenshot_manifest_hash": preview_result.screenshot_manifest_hash,
                        "artifact_count": len(preview_result.artifacts),
                    },
                    ensure_ascii=False,
                )
            )
        elif args.command == "publish-confirm":
            publisher = GitPublisher(
                pool,
                settings.floorball_site_repo,
                settings.worktree_root,
                publish_enabled=settings.publish_enabled,
                media_root=settings.media_root,
            )
            manifest_hash = await pool.fetchval(
                "SELECT screenshot_manifest_hash FROM publication_jobs WHERE id=$1",
                args.publication,
            )
            if not manifest_hash:
                raise RuntimeError("publication screenshot manifest is missing")
            main_commit, static_commit = await publisher.confirm_and_push(
                args.publication, args.nonce, args.actor, manifest_hash
            )
            print(json.dumps({"main_commit": main_commit, "plesk_static_commit": static_commit}))
        elif args.command == "publish-reconcile":
            publisher = GitPublisher(
                pool,
                settings.floorball_site_repo,
                settings.worktree_root,
                publish_enabled=settings.publish_enabled,
                media_root=settings.media_root,
            )
            main_commit, static_commit = await publisher.reconcile_publication(args.publication)
            print(json.dumps({"main_commit": main_commit, "plesk_static_commit": static_commit}))
    finally:
        await pool.close()


def main() -> None:
    args = parser().parse_args()
    settings = get_settings()
    configure_logging(settings.log_level)
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
