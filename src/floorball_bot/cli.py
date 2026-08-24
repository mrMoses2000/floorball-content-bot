from __future__ import annotations

import argparse
import asyncio
import json
import signal
from pathlib import Path
from uuid import UUID

from aiogram import Bot

from floorball_bot.config import get_settings
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
from floorball_bot.providers.codex import CodexExtractor
from floorball_bot.providers.transcription import (
    AssemblyAIBatchTranscriber,
    AssemblyAIWhisperStreamingTranscriber,
    RoutedAssemblyAITranscriber,
)
from floorball_bot.publisher import GitPublisher
from floorball_bot.telegram import TelegramIngress, run_outbox
from floorball_bot.worker import Worker


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="floorball-bot")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    commands.add_parser("bot")
    commands.add_parser("worker")
    commands.add_parser("health")
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
    preview = commands.add_parser("publish-preview")
    preview.add_argument("--draft", required=True, type=UUID)
    preview.add_argument("--actor", required=True, type=UUID)
    confirm = commands.add_parser("publish-confirm")
    confirm.add_argument("--publication", required=True, type=UUID)
    confirm.add_argument("--nonce", required=True)
    confirm.add_argument("--actor", required=True, type=UUID)
    return root


async def async_main(args: argparse.Namespace) -> None:
    settings = get_settings()
    pool = await create_pool(settings.postgres_dsn.get_secret_value())
    try:
        if args.command == "migrate":
            applied = await run_migrations(pool, Path(__file__).resolve().parents[2] / "migrations")
            print(json.dumps({"applied": applied}, ensure_ascii=False))
        elif args.command == "health":
            print(
                json.dumps(
                    await health_report(pool, settings.media_root), ensure_ascii=False, default=str
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
            outbox = asyncio.create_task(run_outbox(bot, pool, "bot-outbox", stop))
            ingress_task = asyncio.create_task(ingress.run())
            await stop.wait()
            await ingress.stop()
            await ingress_task
            await outbox
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
                lease_seconds=settings.job_lease_seconds,
            )
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, lambda: asyncio.create_task(worker.stop()))
            await worker.run()
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
            publisher = GitPublisher(pool, settings.floorball_site_repo, settings.worktree_root)
            preview_result = await publisher.build_preview(publication_id, row["content"])
            print(
                json.dumps(
                    {
                        "publication_id": str(preview_result.publication_id),
                        "base_commit": preview_result.base_commit,
                        "revision_hash": preview_result.revision_hash,
                        "nonce": preview_result.nonce,
                        "diff_summary": preview_result.diff_summary,
                    },
                    ensure_ascii=False,
                )
            )
        elif args.command == "publish-confirm":
            publisher = GitPublisher(pool, settings.floorball_site_repo, settings.worktree_root)
            main_commit, static_commit = await publisher.confirm_and_push(
                args.publication, args.nonce, args.actor
            )
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
