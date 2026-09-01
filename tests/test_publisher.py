import hashlib
import json
import os
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image, ImageDraw

from floorball_bot import publisher as publisher_module
from floorball_bot.db import create_pool, run_migrations
from floorball_bot.errors import ValidationBlocked
from floorball_bot.publisher import (
    CommandFailed,
    GitPublisher,
    build_change_manifest,
    derive_affected_routes,
)
from floorball_bot.workflow import canonical_hash


@pytest.fixture
async def publisher_pg_pool():
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not configured")
    pool = await create_pool(dsn)
    await run_migrations(pool, Path(__file__).parents[1] / "migrations")
    database = await pool.fetchval("SELECT current_database()")
    if not database.endswith("_test"):
        await pool.close()
        raise RuntimeError(f"refusing destructive fixture database: {database}")
    await pool.execute("TRUNCATE users, publication_jobs RESTART IDENTITY CASCADE")
    yield pool
    await pool.close()


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


def test_content_and_code_template_change_manifests_use_separate_allowlists(tmp_path):
    content = tmp_path / "app/src/data/generated/city-content.json"
    generated = tmp_path / "app/dist/index.html"
    component = tmp_path / "app/src/components/Card.jsx"
    content.parent.mkdir(parents=True)
    generated.parent.mkdir(parents=True)
    component.parent.mkdir(parents=True)
    content.write_text("{}\n")
    generated.write_text("<html></html>\n")
    component.write_text("export default null\n")

    content_manifest = build_change_manifest(
        tmp_path,
        {"app/src/data/generated/city-content.json", "app/dist/index.html"},
        change_class="content",
    )
    assert content_manifest["class"] == "content"
    with pytest.raises(ValidationBlocked, match="unexpected"):
        build_change_manifest(
            tmp_path, {"app/src/components/Card.jsx"}, change_class="content"
        )
    code_manifest = build_change_manifest(
        tmp_path,
        {"app/src/components/Card.jsx", "app/dist/index.html"},
        change_class="code_template",
    )
    assert code_manifest["class"] == "code_template"


@pytest.mark.asyncio
async def test_publish_enabled_false_refuses_before_touching_git_or_database(tmp_path):
    repository = tmp_path / "site"
    worktrees = tmp_path / "worktrees"
    repository.mkdir()
    worktrees.mkdir()
    publisher = GitPublisher(
        object(), repository, worktrees, publish_enabled=False
    )

    with pytest.raises(ValidationBlocked, match="PUBLISH_ENABLED=false"):
        await publisher.confirm_and_push(
            uuid4(), "nonce", uuid4(), "e" * 64
        )


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


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_full_preview_confirm_atomic_push_and_reconcile_roundtrip(
    publisher_pg_pool, tmp_path
):
    site, bare, base = make_site(tmp_path)
    command("git", "config", "user.name", "Test Publisher", cwd=site)
    command("git", "config", "user.email", "publisher@example.invalid", cwd=site)
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    actor_id = await publisher_pg_pool.fetchval(
        "INSERT INTO users(phone_e164,display_name) VALUES ($1,'Publisher') RETURNING id",
        f"+77{uuid4().int % 10**9:09d}",
    )
    await publisher_pg_pool.execute(
        "INSERT INTO user_roles(user_id,role_name) VALUES ($1,'superadmin')", actor_id
    )
    session_id = await publisher_pg_pool.fetchval(
        "INSERT INTO conversation_sessions(user_id,workflow) VALUES ($1,'publish') RETURNING id",
        actor_id,
    )
    draft_id = await publisher_pg_pool.fetchval(
        """
        INSERT INTO drafts(
            session_id,entity_type,status,current_revision,approved_revision,created_by,updated_by
        ) VALUES ($1,'city','approved',1,1,$2,$2) RETURNING id
        """,
        session_id,
        actor_id,
    )
    expected_payload = payload()
    revision_hash = canonical_hash(expected_payload)
    await publisher_pg_pool.execute(
        """
        INSERT INTO draft_revisions(draft_id,revision,content,content_hash,created_by)
        VALUES ($1,1,$2::jsonb,$3,$4)
        """,
        draft_id,
        expected_payload,
        revision_hash,
        actor_id,
    )
    publication_id = await publisher_pg_pool.fetchval(
        """
        INSERT INTO publication_jobs(draft_id,revision,revision_hash,requested_by)
        VALUES ($1,1,$2,$3) RETURNING id
        """,
        draft_id,
        revision_hash,
        actor_id,
    )
    publisher = GitPublisher(
        publisher_pg_pool,
        site,
        worktrees,
        screenshot_capture=fake_screenshot_capture,
    )

    preview = await publisher.build_preview(publication_id, expected_payload)
    main_commit, static_commit = await publisher.confirm_and_push(
        publication_id,
        preview.nonce,
        actor_id,
        preview.screenshot_manifest_hash,
        chat_id=778899,
    )

    assert command("git", "rev-parse", "main", cwd=bare) == main_commit
    assert command("git", "rev-parse", "plesk-static", cwd=bare) == static_commit
    assert main_commit != base
    assert await publisher_pg_pool.fetchval(
        "SELECT status FROM publication_jobs WHERE id=$1", publication_id
    ) == "published"
    assert await publisher.reconcile_publication(publication_id) == (
        main_commit,
        static_commit,
    )


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


class FakePushPool:
    def __init__(self, publication_id):
        self.publication_id = publication_id
        self.executions = []

    async def fetchval(self, *args):
        self.executions.append(args)
        return self.publication_id

    async def execute(self, *args):
        self.executions.append(args)


@pytest.mark.asyncio
async def test_reconciler_pushes_expected_main_and_static_atomically_and_verifies_refs(
    tmp_path, monkeypatch
):
    publication_id = uuid4()
    base = "a" * 40
    base_static = "d" * 40
    main_commit = "b" * 40
    static_commit = "c" * 40
    pool = FakePushPool(publication_id)
    repository = tmp_path / "site"
    worktrees = tmp_path / "worktrees"
    worktree = worktrees / str(publication_id)
    repository.mkdir()
    worktree.mkdir(parents=True)
    calls = []
    pushed = False

    async def fake_run_command(*args, **_kwargs):
        nonlocal pushed
        calls.append(args)
        if args[:3] == ("git", "ls-remote", "origin"):
            if args[3] == "refs/heads/main":
                commit = main_commit if pushed else base
            else:
                commit = static_commit if pushed else base_static
            return f"{commit}\t{args[3]}\n"
        if args[:3] == ("git", "push", "--atomic"):
            pushed = True
        return ""

    monkeypatch.setattr(publisher_module, "run_command", fake_run_command)
    publisher = GitPublisher(pool, repository, worktrees)
    result = await publisher._push_or_observe(
        {
            "id": publication_id,
            "base_commit": base,
            "base_static_commit": base_static,
            "expected_main_commit": main_commit,
            "expected_static_commit": static_commit,
        },
        "lease-owner",
    )

    assert result == (main_commit, static_commit)
    assert (
        "git",
        "push",
        "--atomic",
        "origin",
        f"{main_commit}:refs/heads/main",
        f"{static_commit}:refs/heads/plesk-static",
    ) in calls
    assert any("status='remote_verified'" in execution[0] for execution in pool.executions)


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
