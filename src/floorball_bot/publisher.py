from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import asyncpg

from floorball_bot.errors import ValidationBlocked

ALLOWED_SOURCE_CHANGES = {
    "app/src/data/generated/city-content.json",
    "app/src/data/generated/federation-content.json",
    "app/src/data/generated/news-content.json",
}


def is_allowed_change(path: str) -> bool:
    return path in ALLOWED_SOURCE_CHANGES or path.startswith("app/dist/")


@dataclass(frozen=True)
class PublicationPreview:
    publication_id: UUID
    base_commit: str
    revision_hash: str
    nonce: str
    diff_summary: str
    worktree: Path


class CommandFailed(RuntimeError):
    pass


async def run_command(
    *command: str,
    cwd: Path,
    timeout_seconds: int = 600,
    env: dict[str, str] | None = None,
) -> str:
    if env is None:
        allowed = (
            "PATH",
            "HOME",
            "LANG",
            "LC_ALL",
            "SSH_AUTH_SOCK",
            "GIT_SSH_COMMAND",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        )
        env = {key: os.environ[key] for key in allowed if key in os.environ}
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise CommandFailed(f"command timeout: {command[0]}") from exc
    text = output.decode(errors="replace")
    if len(text) > 2_000_000:
        text = text[-2_000_000:]
    if process.returncode:
        raise CommandFailed(f"{command[0]} exited {process.returncode}: {text[-4000:]}")
    return text


class GitPublisher:
    def __init__(self, pool: asyncpg.Pool, repository: Path, worktree_root: Path) -> None:
        self.pool = pool
        self.repository = repository.resolve()
        self.worktree_root = worktree_root.resolve()

    async def build_preview(self, publication_id: UUID, payload: dict) -> PublicationPreview:
        worktree = self.worktree_root / str(publication_id)
        if worktree.exists():
            raise ValidationBlocked("publication worktree already exists")
        revision_hash = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        publication = await self.pool.fetchrow(
            "SELECT revision_hash, status FROM publication_jobs WHERE id=$1", publication_id
        )
        if (
            not publication
            or publication["status"] != "requested"
            or publication["revision_hash"] != revision_hash
        ):
            raise ValidationBlocked("publication payload does not match requested revision")
        await self.pool.execute(
            "UPDATE publication_jobs SET status='building', updated_at=now() WHERE id=$1",
            publication_id,
        )
        await run_command(
            "git", "fetch", "origin", "--prune", cwd=self.repository, timeout_seconds=120
        )
        base_commit = (
            await run_command("git", "rev-parse", "origin/main", cwd=self.repository)
        ).strip()
        await run_command(
            "git", "worktree", "add", "--detach", str(worktree), "origin/main", cwd=self.repository
        )
        try:
            if (worktree / "app/package-lock.json").is_file():
                await run_command("npm", "--prefix", "app", "ci", cwd=worktree, timeout_seconds=300)
            source = worktree / ".floorball-publication-source.json"
            source.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            try:
                if "cities" in payload:
                    await run_command(
                        "node",
                        "scripts/sync-coach-content.mjs",
                        "--source",
                        str(source),
                        cwd=worktree,
                    )
                elif "federation" in payload:
                    await run_command(
                        "node",
                        "scripts/sync-federation-content.mjs",
                        "--source",
                        str(source),
                        cwd=worktree,
                    )
                elif "items" in payload:
                    await run_command(
                        "node",
                        "scripts/sync-news-content.mjs",
                        "--source",
                        str(source),
                        cwd=worktree,
                    )
                else:
                    raise ValidationBlocked("unsupported publication payload")
            finally:
                source.unlink(missing_ok=True)
            source_changes = {
                line[3:]
                for line in (
                    await run_command("git", "status", "--porcelain", cwd=worktree)
                ).splitlines()
                if len(line) > 3
            }
            if not source_changes or not source_changes.issubset(ALLOWED_SOURCE_CHANGES):
                raise ValidationBlocked(
                    f"publication changed unexpected source files: {sorted(source_changes)}"
                )
            checks = []
            checks.append(
                await run_command("node", "scripts/test-coach-data-api.mjs", cwd=worktree)
            )
            checks.append(
                await run_command("node", "scripts/test-federation-content.mjs", cwd=worktree)
            )
            checks.append(
                await run_command("node", "scripts/test-news-content.mjs", cwd=worktree)
            )
            checks.append(await run_command("npm", "--prefix", "app", "test", cwd=worktree))
            checks.append(await run_command("npm", "--prefix", "app", "run", "build", cwd=worktree))
            for required in (worktree / "app/dist/index.html", worktree / "app/dist/.htaccess"):
                if not required.is_file():
                    raise ValidationBlocked(f"build output missing: {required.name}")
            all_changes = {
                line[3:]
                for line in (
                    await run_command("git", "status", "--porcelain", cwd=worktree)
                ).splitlines()
                if len(line) > 3
            }
            unexpected = sorted(path for path in all_changes if not is_allowed_change(path))
            if unexpected:
                raise ValidationBlocked(f"publication changed unexpected files: {unexpected}")
            diff = await run_command("git", "diff", "--stat", cwd=worktree)
            nonce = secrets.token_urlsafe(24)
            nonce_hash = hashlib.sha256(nonce.encode()).hexdigest()
            await self.pool.execute(
                """
                UPDATE publication_jobs
                SET status='preview_ready', base_commit=$2, revision_hash=$3,
                    preview_nonce_hash=$4, preview_expires_at=now()+interval '30 minutes',
                    diff_summary=$5, check_output=$6, updated_at=now()
                WHERE id=$1
                """,
                publication_id,
                base_commit,
                revision_hash,
                nonce_hash,
                diff,
                "\n".join(checks)[-100_000:],
            )
            return PublicationPreview(
                publication_id, base_commit, revision_hash, nonce, diff, worktree
            )
        except Exception:
            await self.cleanup(worktree)
            raise

    async def confirm_and_push(
        self, publication_id: UUID, nonce: str, actor_id: UUID
    ) -> tuple[str, str]:
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext('floorball-publication'))"
            )
            row = await connection.fetchrow(
                """
                SELECT p.*, d.status AS draft_status, r.content_hash AS approved_hash
                FROM publication_jobs p
                JOIN drafts d ON d.id=p.draft_id AND d.approved_revision=p.revision
                JOIN draft_revisions r ON r.draft_id=d.id AND r.revision=p.revision
                WHERE p.id=$1 AND p.status='preview_ready'
                  AND p.preview_expires_at > now() AND d.status='approved'
                FOR UPDATE OF p
                """,
                publication_id,
            )
            if not row or not secrets.compare_digest(
                row["preview_nonce_hash"], hashlib.sha256(nonce.encode()).hexdigest()
            ):
                raise ValidationBlocked("publication confirmation is invalid or expired")
            if row["approved_hash"] != row["revision_hash"]:
                raise ValidationBlocked("approved revision hash changed")
            current = (
                await run_command("git", "rev-parse", "origin/main", cwd=self.repository)
            ).strip()
            if current != row["base_commit"]:
                await connection.execute(
                    "UPDATE publication_jobs SET status='cancelled', updated_at=now() WHERE id=$1",
                    publication_id,
                )
                raise ValidationBlocked("origin/main changed; build a new preview")
            worktree = self.worktree_root / str(publication_id)
            await run_command(
                "git", "add", *sorted(ALLOWED_SOURCE_CHANGES), "app/dist", cwd=worktree
            )
            await run_command(
                "git",
                "commit",
                "-m",
                f"content: publish approved revision {publication_id}",
                cwd=worktree,
            )
            main_commit = (await run_command("git", "rev-parse", "HEAD", cwd=worktree)).strip()
            static_commit = (
                await run_command(
                    "git", "subtree", "split", "--prefix=app/dist", "HEAD", cwd=worktree
                )
            ).strip()
            await run_command(
                "git",
                "push",
                "--atomic",
                "origin",
                "HEAD:main",
                f"{static_commit}:refs/heads/plesk-static",
                cwd=worktree,
                timeout_seconds=120,
            )
            remote_main = (
                await run_command(
                    "git", "ls-remote", "origin", "refs/heads/main", cwd=worktree
                )
            ).split()[0]
            remote_static = (
                await run_command(
                    "git", "ls-remote", "origin", "refs/heads/plesk-static", cwd=worktree
                )
            ).split()[0]
            if remote_main != main_commit or remote_static != static_commit:
                raise ValidationBlocked("remote main/plesk-static verification failed")
            await connection.execute(
                """
                UPDATE publication_jobs SET status='published', confirmed_by=$2,
                    main_commit=$3, static_commit=$4, updated_at=now() WHERE id=$1
                """,
                publication_id,
                actor_id,
                main_commit,
                static_commit,
            )
        await self.cleanup(self.worktree_root / str(publication_id))
        return main_commit, static_commit

    async def cleanup(self, worktree: Path) -> None:
        if worktree.parent != self.worktree_root:
            raise RuntimeError("refusing to clean unexpected worktree path")
        if worktree.exists():
            await run_command(
                "git", "worktree", "remove", "--force", str(worktree), cwd=self.repository
            )
