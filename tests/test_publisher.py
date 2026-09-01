import hashlib
import json
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image, ImageDraw

from floorball_bot import publisher as publisher_module
from floorball_bot.errors import ValidationBlocked
from floorball_bot.publisher import CommandFailed, GitPublisher, derive_affected_routes


class FakePool:
    def __init__(self, expected_payload):
        self.executions = []
        self.revision_hash = hashlib.sha256(
            json.dumps(
                expected_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()

    async def fetchrow(self, *args):
        return {"revision_hash": self.revision_hash, "status": "requested"}

    async def execute(self, *args):
        self.executions.append(args)

    async def fetch(self, *_args):
        return []

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        yield


async def fake_screenshot_capture(_worktree, routes, output_dir):
    output_dir.mkdir(parents=True)
    artifacts = []
    dimensions = {
        "desktop-1280x720": (1280, 720),
        "mobile-390x844": (390, 844),
    }
    for route in routes:
        for language in ("ru", "kz", "en"):
            for viewport, size in dimensions.items():
                target = output_dir / f"{len(artifacts)}.png"
                image = Image.new("RGB", size, "white")
                ImageDraw.Draw(image).rectangle((10, 10, 100, 100), fill="blue")
                image.save(target)
                artifacts.append(
                    {
                        "route": route,
                        "language": language,
                        "viewport": viewport,
                        "width": size[0],
                        "height": size[1],
                        "path": str(target),
                    }
                )
    return artifacts


def command(*args: str, cwd: Path) -> str:
    return subprocess.run(  # noqa: S603 - fixed argv is controlled by this test
        args,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def make_site(tmp_path: Path, *, fail_build: bool = False) -> tuple[Path, Path, str]:
    bare = tmp_path / "remote.git"
    site = tmp_path / "site"
    bare.mkdir()
    site.mkdir()
    command("git", "init", "--bare", "--initial-branch=main", cwd=bare)
    command("git", "init", "--initial-branch=main", cwd=site)
    (site / "scripts").mkdir()
    (site / "app/src/data/generated").mkdir(parents=True)
    (site / "app/dist").mkdir(parents=True)
    sync = """
import fs from 'node:fs'
const index = process.argv.indexOf('--source')
const payload = JSON.parse(fs.readFileSync(process.argv[index + 1], 'utf8'))
const output = JSON.stringify(payload, null, 4) + '\\n'
fs.writeFileSync('app/src/data/generated/city-content.json', output)
""".strip()
    (site / "scripts/sync-coach-content.mjs").write_text(sync, encoding="utf-8")
    (site / "scripts/sync-federation-content.mjs").write_text(
        sync.replace("city-content", "federation-content"), encoding="utf-8"
    )
    (site / "scripts/sync-news-content.mjs").write_text(
        sync.replace("city-content", "news-content"), encoding="utf-8"
    )
    (site / "scripts/test-coach-data-api.mjs").write_text("console.log('coach ok')\n")
    (site / "scripts/test-federation-content.mjs").write_text("console.log('fed ok')\n")
    (site / "scripts/test-news-content.mjs").write_text("console.log('news ok')\n")
    (site / "app/build.mjs").write_text(
        """
import fs from 'node:fs'
fs.mkdirSync('dist', { recursive: true })
fs.writeFileSync('dist/index.html', '<html>ok</html>\\n')
fs.writeFileSync('dist/.htaccess', 'RewriteEngine On\\n')
""".strip()
        + "\n",
        encoding="utf-8",
    )
    scripts = {
        "test": "node -e \"console.log('tests ok')\"",
        "build": 'node -e "process.exit(2)"' if fail_build else "node build.mjs",
    }
    (site / "app/package.json").write_text(json.dumps({"scripts": scripts}), encoding="utf-8")
    initial = {"ok": True, "version": 1, "generatedAt": "old", "cities": []}
    (site / "app/src/data/generated/city-content.json").write_text(json.dumps(initial) + "\n")
    (site / "app/src/data/generated/federation-content.json").write_text(
        json.dumps({"ok": True, "version": 1, "federation": {}}) + "\n"
    )
    (site / "app/src/data/generated/news-content.json").write_text(
        json.dumps({"ok": True, "version": 1, "items": []}) + "\n"
    )
    (site / "app/dist/index.html").write_text("<html>ok</html>\n")
    (site / "app/dist/.htaccess").write_text("RewriteEngine On\n")
    command("git", "add", ".", cwd=site)
    command(
        "git",
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "initial",
        cwd=site,
    )
    command("git", "remote", "add", "origin", str(bare), cwd=site)
    command("git", "push", "-u", "origin", "main", cwd=site)
    base = command("git", "rev-parse", "HEAD", cwd=site)
    return site, bare, base


def payload() -> dict:
    return {
        "ok": True,
        "version": 1,
        "generatedAt": "2026-08-24T00:00:00Z",
        "cities": [
            {
                "slug": "almaty",
                "nameRu": "Алматы",
                "nameKz": "Алматы",
                "nameEn": "Almaty",
                "updatedAt": "2026-08-24T00:00:00Z",
            }
        ],
    }


def news_payload() -> dict:
    return {
        "ok": True,
        "version": 1,
        "generatedAt": "2026-09-01T00:00:00Z",
        "items": [{"slug": "safe-news"}],
    }


@pytest.mark.asyncio
async def test_publication_preview_uses_isolated_worktree_and_does_not_push(tmp_path):
    site, bare, base = make_site(tmp_path)
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    expected_payload = payload()
    pool = FakePool(expected_payload)
    publisher = GitPublisher(
        pool, site, worktrees, screenshot_capture=fake_screenshot_capture
    )
    preview = await publisher.build_preview(uuid4(), expected_payload)

    assert preview.base_commit == base
    assert preview.worktree != site
    assert preview.worktree.is_dir()
    assert "city-content.json" in preview.diff_summary
    assert command("git", "rev-parse", "main", cwd=bare) == base
    assert any("preview_ready" in execution[0] for execution in pool.executions)
    assert preview.screenshot_manifest_hash
    assert len(preview.artifacts) == 18
    await publisher.cleanup(preview.worktree)
    assert not preview.worktree.exists()


@pytest.mark.asyncio
async def test_news_preview_changes_only_news_bundle_and_build_output(tmp_path):
    site, bare, base = make_site(tmp_path)
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    expected_payload = news_payload()
    publisher = GitPublisher(
        FakePool(expected_payload),
        site,
        worktrees,
        screenshot_capture=fake_screenshot_capture,
    )

    preview = await publisher.build_preview(uuid4(), expected_payload)

    assert "news-content.json" in preview.diff_summary
    assert command("git", "rev-parse", "main", cwd=bare) == base
    await publisher.cleanup(preview.worktree)


@pytest.mark.asyncio
async def test_build_failure_does_not_push_and_cleans_worktree(tmp_path):
    site, bare, base = make_site(tmp_path, fail_build=True)
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    expected_payload = payload()
    publisher = GitPublisher(
        FakePool(expected_payload),
        site,
        worktrees,
        screenshot_capture=fake_screenshot_capture,
    )
    publication_id = uuid4()

    with pytest.raises(CommandFailed):
        await publisher.build_preview(publication_id, expected_payload)

    assert command("git", "rev-parse", "main", cwd=bare) == base
    assert not (worktrees / str(publication_id)).exists()


class FakeConfirmConnection:
    def __init__(self, row):
        self.row = row
        self.executions = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchrow(self, *_args):
        return self.row

    async def execute(self, *args):
        self.executions.append(args)


class FakeConfirmPool:
    def __init__(self, row):
        self.connection = FakeConfirmConnection(row)

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@pytest.mark.asyncio
async def test_confirm_pushes_main_and_static_atomically_and_verifies_refs(
    tmp_path, monkeypatch
):
    publication_id = uuid4()
    actor_id = uuid4()
    nonce = "one-use-preview-nonce"
    base = "a" * 40
    main_commit = "b" * 40
    static_commit = "c" * 40
    pool = FakeConfirmPool(
        {
            "preview_nonce_hash": hashlib.sha256(nonce.encode()).hexdigest(),
            "base_commit": base,
            "approved_hash": "d" * 64,
            "revision_hash": "d" * 64,
        }
    )
    repository = tmp_path / "site"
    worktrees = tmp_path / "worktrees"
    worktree = worktrees / str(publication_id)
    repository.mkdir()
    worktree.mkdir(parents=True)
    calls = []

    async def fake_run_command(*args, **_kwargs):
        calls.append(args)
        if args[:3] == ("git", "rev-parse", "origin/main"):
            return base
        if args[:3] == ("git", "rev-parse", "HEAD"):
            return main_commit
        if args[:3] == ("git", "subtree", "split"):
            return static_commit
        if args[:3] == ("git", "ls-remote", "origin"):
            commit = main_commit if args[3] == "refs/heads/main" else static_commit
            return f"{commit}\t{args[3]}\n"
        return ""

    monkeypatch.setattr(publisher_module, "run_command", fake_run_command)
    publisher = GitPublisher(pool, repository, worktrees)

    async def fake_verify_manifest(*_args):
        return None

    monkeypatch.setattr(publisher, "_verify_persisted_manifest", fake_verify_manifest)

    result = await publisher.confirm_and_push(
        publication_id, nonce, actor_id, "e" * 64
    )

    assert result == (main_commit, static_commit)
    assert (
        "git",
        "push",
        "--atomic",
        "origin",
        "HEAD:main",
        f"{static_commit}:refs/heads/plesk-static",
    ) in calls
    assert any("status='published'" in execution[0] for execution in pool.connection.executions)


def test_affected_routes_are_derived_from_changed_news_entity():
    old = {
        "items": [{"slug": "old", "scope": "national", "titleRu": "Old"}]
    }
    current = {
        "items": [
            {"slug": "old", "scope": "national", "titleRu": "Old"},
            {"slug": "city-news", "scope": "city", "citySlug": "almaty"},
        ]
    }

    assert derive_affected_routes(current, old) == (
        "/",
        "/clubs/almaty",
        "/news",
        "/news/city-news",
    )


def test_affected_routes_include_old_and_new_city_when_news_moves():
    old = {
        "items": [{
            "slug": "city-news",
            "scope": "city",
            "citySlug": "almaty",
        }]
    }
    current = {
        "items": [{
            "slug": "city-news",
            "scope": "city",
            "citySlug": "astana",
        }]
    }

    assert derive_affected_routes(current, old) == (
        "/",
        "/clubs/almaty",
        "/clubs/astana",
        "/news",
        "/news/city-news",
    )


def test_screenshot_validator_rejects_blank_or_missing_matrix(tmp_path):
    output = tmp_path / "artifacts"
    output.mkdir()
    target = output / "blank.png"
    Image.new("RGB", (1280, 720), "white").save(target)
    raw = [{
        "route": "/",
        "language": "ru",
        "viewport": "desktop-1280x720",
        "path": str(target),
    }]

    with pytest.raises(ValidationBlocked, match="blank"):
        GitPublisher._validate_artifacts(raw, output_dir=output, routes=("/",))


class FakeArtifactConnection:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, *_args):
        return self.rows


@pytest.mark.asyncio
async def test_persisted_manifest_rejects_a_modified_screenshot(tmp_path):
    artifact = tmp_path / "preview.png"
    image = Image.new("RGB", (1280, 720), "white")
    ImageDraw.Draw(image).rectangle((10, 10, 100, 100), fill="blue")
    image.save(artifact)
    original_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
    manifest_hash = "e" * 64
    connection = FakeArtifactConnection([{
        "route": "/",
        "language": "ru",
        "viewport": "desktop-1280x720",
        "path": str(artifact),
        "sha256": original_hash,
        "width": 1280,
        "height": 720,
        "manifest_hash": manifest_hash,
    }])
    artifact.write_bytes(artifact.read_bytes() + b"tampered")

    with pytest.raises(ValidationBlocked, match="hash changed"):
        await GitPublisher._verify_persisted_manifest(
            connection, uuid4(), manifest_hash
        )


class FakeRetentionPool:
    def __init__(self, rows):
        self.rows = rows
        self.executions = []

    async def fetch(self, *_args):
        return self.rows

    async def execute(self, *args):
        self.executions.append(args)


@pytest.mark.asyncio
async def test_expired_artifact_cleanup_only_removes_files_under_artifact_root(tmp_path):
    repository = tmp_path / "site"
    worktrees = tmp_path / "worktrees"
    publication_id = uuid4()
    artifact_dir = worktrees / "_artifacts" / str(publication_id)
    repository.mkdir()
    artifact_dir.mkdir(parents=True)
    artifact = artifact_dir / "preview.png"
    artifact.write_bytes(b"expired")
    (artifact_dir / "capture-manifest.json").write_text("{}\n")
    row_id = uuid4()
    pool = FakeRetentionPool([{"id": row_id, "path": str(artifact)}])
    publisher = GitPublisher(pool, repository, worktrees)

    assert await publisher.cleanup_expired_artifacts() == 1
    assert not artifact.exists()
    assert not artifact_dir.exists()
    assert pool.executions[0][1] == row_id


@pytest.mark.asyncio
async def test_expired_artifact_cleanup_refuses_external_paths(tmp_path):
    repository = tmp_path / "site"
    worktrees = tmp_path / "worktrees"
    repository.mkdir()
    worktrees.mkdir()
    external = tmp_path / "external.png"
    external.write_bytes(b"keep")
    pool = FakeRetentionPool([{"id": uuid4(), "path": str(external)}])
    publisher = GitPublisher(pool, repository, worktrees)

    with pytest.raises(RuntimeError, match="outside artifact root"):
        await publisher.cleanup_expired_artifacts()
    assert external.exists()
