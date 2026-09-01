from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import signal
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import asyncpg
from PIL import Image

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
    screenshot_manifest_hash: str
    artifacts: tuple[Path, ...]


ScreenshotCapture = Callable[[Path, tuple[str, ...], Path], Awaitable[list[dict]]]


class CommandFailed(RuntimeError):
    pass


def derive_affected_routes(payload: dict, previous: dict) -> tuple[str, ...]:
    routes = {"/"}
    if "cities" in payload:
        routes.add("/clubs")
        old = {item.get("slug"): item for item in previous.get("cities", [])}
        current = {item.get("slug"): item for item in payload.get("cities", [])}
        for slug in old.keys() | current.keys():
            if slug and old.get(slug) != current.get(slug):
                routes.add(f"/clubs/{slug}")
    elif "items" in payload:
        routes.add("/news")
        old = {item.get("slug"): item for item in previous.get("items", [])}
        current = {item.get("slug"): item for item in payload.get("items", [])}
        for slug in old.keys() | current.keys():
            before = old.get(slug)
            after = current.get(slug)
            if slug and before != after:
                routes.add(f"/news/{slug}")
                for item in (before, after):
                    if item and item.get("scope") == "city" and item.get("citySlug"):
                        routes.add(f"/clubs/{item['citySlug']}")
    elif "federation" in payload:
        route_map = {
            "mission": "/about/mission",
            "history": "/about/history",
            "achievements": "/about/achievements",
            "roadmap": "/about/roadmap",
            "leadership": "/about/leadership",
        }
        current = payload.get("federation", {})
        old = previous.get("federation", {})
        for key, route in route_map.items():
            if current.get(key) != old.get(key):
                routes.add(route)
    else:
        raise ValidationBlocked("unsupported publication payload")
    return tuple(sorted(routes))


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
    def __init__(
        self,
        pool: asyncpg.Pool,
        repository: Path,
        worktree_root: Path,
        *,
        screenshot_capture: ScreenshotCapture | None = None,
    ) -> None:
        self.pool = pool
        self.repository = repository.resolve()
        self.worktree_root = worktree_root.resolve()
        self.artifact_root = (self.worktree_root / "_artifacts").resolve()
        self.screenshot_capture = screenshot_capture or self._capture_screenshots

    @staticmethod
    def _bundle_path(payload: dict) -> str:
        if "cities" in payload:
            return "app/src/data/generated/city-content.json"
        if "federation" in payload:
            return "app/src/data/generated/federation-content.json"
        if "items" in payload:
            return "app/src/data/generated/news-content.json"
        raise ValidationBlocked("unsupported publication payload")

    @staticmethod
    def _allocate_loopback_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            candidate.bind(("127.0.0.1", 0))
            return int(candidate.getsockname()[1])

    async def _capture_screenshots(
        self, worktree: Path, routes: tuple[str, ...], output_dir: Path
    ) -> list[dict]:
        port = self._allocate_loopback_port()
        environment = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "LANG", "LC_ALL")
            if key in os.environ
        }
        process = await asyncio.create_subprocess_exec(
            "npm",
            "--prefix",
            "app",
            "run",
            "preview",
            "--",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--strictPort",
            cwd=worktree,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=environment,
            start_new_session=True,
        )
        try:
            for _attempt in range(100):
                if process.returncode is not None:
                    output = (await process.communicate())[0].decode(errors="replace")
                    raise CommandFailed(f"preview server stopped: {output[-4000:]}")
                try:
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                    writer.close()
                    await writer.wait_closed()
                    del reader
                    break
                except OSError:
                    await asyncio.sleep(0.1)
            else:
                raise CommandFailed("preview server did not become ready on loopback")
            output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
            config_path = output_dir / "capture-config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "baseUrl": f"http://127.0.0.1:{port}",
                        "routes": routes,
                        "outputDir": str(output_dir),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            config_path.chmod(0o600)
            try:
                await run_command(
                    "node",
                    "app/scripts/capture-publication-screenshots.mjs",
                    str(config_path),
                    cwd=worktree,
                    timeout_seconds=300,
                    env=environment,
                )
            finally:
                config_path.unlink(missing_ok=True)
            manifest = json.loads(
                (output_dir / "capture-manifest.json").read_text(encoding="utf-8")
            )
            return list(manifest.get("artifacts", []))
        finally:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except TimeoutError:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()

    @staticmethod
    def _validate_artifacts(
        raw_artifacts: list[dict],
        *,
        output_dir: Path,
        routes: tuple[str, ...],
    ) -> tuple[list[dict], str]:
        expected = {
            (route, language, viewport)
            for route in routes
            for language in ("ru", "kz", "en")
            for viewport in ("desktop-1280x720", "mobile-390x844")
        }
        validated: list[dict] = []
        observed: set[tuple[str, str, str]] = set()
        dimensions = {
            "desktop-1280x720": (1280, 720),
            "mobile-390x844": (390, 844),
        }
        for raw in raw_artifacts:
            key = (raw.get("route"), raw.get("language"), raw.get("viewport"))
            if key not in expected or key in observed:
                raise ValidationBlocked(f"unexpected or duplicate screenshot: {key}")
            artifact_path = Path(str(raw.get("path", ""))).resolve()
            if artifact_path.parent != output_dir.resolve() or not artifact_path.is_file():
                raise ValidationBlocked(
                    f"screenshot path is missing or escapes artifact root: {key}"
                )
            if artifact_path.stat().st_size < 1024:
                raise ValidationBlocked(f"screenshot is empty or truncated: {key}")
            try:
                with Image.open(artifact_path) as image:
                    image.verify()
                with Image.open(artifact_path) as image:
                    if image.format != "PNG" or image.size != dimensions[key[2]]:
                        raise ValidationBlocked(f"screenshot dimensions/format mismatch: {key}")
                    low, high = image.convert("L").getextrema()
                    if high - low < 3:
                        raise ValidationBlocked(f"screenshot appears blank: {key}")
            except (OSError, ValueError) as exc:
                raise ValidationBlocked(f"screenshot is corrupt: {key}") from exc
            sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
            validated.append(
                {
                    "route": key[0],
                    "language": key[1],
                    "viewport": key[2],
                    "width": dimensions[key[2]][0],
                    "height": dimensions[key[2]][1],
                    "path": str(artifact_path),
                    "filename": artifact_path.name,
                    "sha256": sha256,
                }
            )
            observed.add(key)
        missing = sorted(expected - observed)
        if missing:
            raise ValidationBlocked(f"expected screenshots are missing: {missing[:5]}")
        validated.sort(key=lambda item: (item["route"], item["language"], item["viewport"]))
        manifest_body = [
            {key: item[key] for key in (
                "route", "language", "viewport", "width", "height", "filename", "sha256"
            )}
            for item in validated
        ]
        manifest_hash = hashlib.sha256(
            json.dumps(manifest_body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return validated, manifest_hash

    @staticmethod
    async def _verify_persisted_manifest(
        connection: asyncpg.Connection, publication_id: UUID, expected_hash: str
    ) -> None:
        rows = await connection.fetch(
            """
            SELECT route, language, viewport, path, sha256, width, height, manifest_hash
            FROM publication_artifacts
            WHERE publication_id=$1 AND valid=TRUE
            ORDER BY route, language, viewport
            """,
            publication_id,
        )
        if not rows or any(row["manifest_hash"] != expected_hash for row in rows):
            raise ValidationBlocked("screenshot artifact manifest is missing or invalidated")
        manifest = []
        for row in rows:
            artifact_path = Path(row["path"])
            if not artifact_path.is_file():
                raise ValidationBlocked("approved screenshot file is missing")
            actual_hash = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
            if not secrets.compare_digest(actual_hash, row["sha256"]):
                raise ValidationBlocked("approved screenshot hash changed")
            try:
                with Image.open(artifact_path) as image:
                    if image.format != "PNG" or image.size != (row["width"], row["height"]):
                        raise ValidationBlocked("approved screenshot dimensions changed")
            except OSError as exc:
                raise ValidationBlocked("approved screenshot is corrupt") from exc
            manifest.append(
                {
                    "route": row["route"],
                    "language": row["language"],
                    "viewport": row["viewport"],
                    "width": row["width"],
                    "height": row["height"],
                    "filename": artifact_path.name,
                    "sha256": row["sha256"],
                }
            )
        actual_manifest_hash = hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if not secrets.compare_digest(actual_manifest_hash, expected_hash):
            raise ValidationBlocked("screenshot manifest hash changed")

    async def build_preview(self, publication_id: UUID, payload: dict) -> PublicationPreview:
        await self.cleanup_expired_artifacts()
        worktree = self.worktree_root / str(publication_id)
        artifact_dir = self.artifact_root / str(publication_id)
        if worktree.exists():
            raise ValidationBlocked("publication worktree already exists")
        if artifact_dir.exists():
            raise ValidationBlocked("publication artifact directory already exists")
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
            bundle_path = self._bundle_path(payload)
            previous = json.loads((worktree / bundle_path).read_text(encoding="utf-8"))
            affected_routes = derive_affected_routes(payload, previous)
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
            raw_artifacts = await self.screenshot_capture(
                worktree, affected_routes, artifact_dir
            )
            artifacts, manifest_hash = self._validate_artifacts(
                raw_artifacts, output_dir=artifact_dir, routes=affected_routes
            )
            (artifact_dir / "capture-manifest.json").write_text(
                json.dumps(
                    {
                        "publicationId": str(publication_id),
                        "revisionHash": revision_hash,
                        "manifestHash": manifest_hash,
                        "artifacts": artifacts,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            nonce = secrets.token_urlsafe(24)
            nonce_hash = hashlib.sha256(nonce.encode()).hexdigest()
            async with self.pool.acquire() as connection, connection.transaction():
                for artifact in artifacts:
                    await connection.execute(
                        """
                        INSERT INTO publication_artifacts(
                            publication_id, route, language, viewport, path, sha256,
                            width, height, revision_hash, manifest_hash
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                        """,
                        publication_id,
                        artifact["route"],
                        artifact["language"],
                        artifact["viewport"],
                        artifact["path"],
                        artifact["sha256"],
                        artifact["width"],
                        artifact["height"],
                        revision_hash,
                        manifest_hash,
                    )
                await connection.execute(
                    """
                    UPDATE publication_jobs
                    SET status='preview_ready', base_commit=$2, revision_hash=$3,
                        preview_nonce_hash=$4,
                        preview_expires_at=now()+interval '30 minutes',
                        screenshot_manifest_hash=$5, artifacts_invalidated_at=NULL,
                        diff_summary=$6, check_output=$7, updated_at=now()
                    WHERE id=$1
                    """,
                    publication_id,
                    base_commit,
                    revision_hash,
                    nonce_hash,
                    manifest_hash,
                    diff,
                    "\n".join(checks)[-100_000:],
                )
            return PublicationPreview(
                publication_id,
                base_commit,
                revision_hash,
                nonce,
                diff,
                worktree,
                manifest_hash,
                tuple(Path(item["path"]) for item in artifacts),
            )
        except Exception:
            await self.cleanup(worktree)
            self.cleanup_artifacts(artifact_dir)
            raise

    async def confirm_and_push(
        self,
        publication_id: UUID,
        nonce: str,
        actor_id: UUID,
        expected_manifest_hash: str,
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
                  AND p.artifacts_invalidated_at IS NULL
                  AND p.screenshot_manifest_hash=$2
                FOR UPDATE OF p
                """,
                publication_id,
                expected_manifest_hash,
            )
            if not row or not secrets.compare_digest(
                row["preview_nonce_hash"], hashlib.sha256(nonce.encode()).hexdigest()
            ):
                raise ValidationBlocked("publication confirmation is invalid or expired")
            if row["approved_hash"] != row["revision_hash"]:
                raise ValidationBlocked("approved revision hash changed")
            await self._verify_persisted_manifest(
                connection, publication_id, expected_manifest_hash
            )
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

    def cleanup_artifacts(self, artifact_dir: Path) -> None:
        resolved = artifact_dir.resolve()
        if resolved.parent != self.artifact_root:
            raise RuntimeError("refusing to clean unexpected artifact path")
        if resolved.exists():
            shutil.rmtree(resolved)

    async def cleanup_expired_artifacts(self) -> int:
        rows = await self.pool.fetch(
            """
            SELECT id, path FROM publication_artifacts
            WHERE retain_until < now()
            ORDER BY retain_until LIMIT 500
            """
        )
        removed = 0
        touched_directories: set[Path] = set()
        for row in rows:
            artifact_path = Path(row["path"]).resolve()
            if artifact_path.parent.parent != self.artifact_root:
                raise RuntimeError("refusing to remove artifact outside artifact root")
            artifact_path.unlink(missing_ok=True)
            touched_directories.add(artifact_path.parent)
            await self.pool.execute(
                "DELETE FROM publication_artifacts WHERE id=$1 AND retain_until < now()",
                row["id"],
            )
            removed += 1
        for artifact_dir in touched_directories:
            if not artifact_dir.is_dir():
                continue
            remaining = list(artifact_dir.iterdir())
            if all(item.name == "capture-manifest.json" for item in remaining):
                (artifact_dir / "capture-manifest.json").unlink(missing_ok=True)
                artifact_dir.rmdir()
        return removed
