import hashlib
import json
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from floorball_bot.publisher import CommandFailed, GitPublisher


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
    (site / "scripts/test-coach-data-api.mjs").write_text("console.log('coach ok')\n")
    (site / "scripts/test-federation-content.mjs").write_text("console.log('fed ok')\n")
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


@pytest.mark.asyncio
async def test_publication_preview_uses_isolated_worktree_and_does_not_push(tmp_path):
    site, bare, base = make_site(tmp_path)
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    expected_payload = payload()
    pool = FakePool(expected_payload)
    publisher = GitPublisher(pool, site, worktrees)
    preview = await publisher.build_preview(uuid4(), expected_payload)

    assert preview.base_commit == base
    assert preview.worktree != site
    assert preview.worktree.is_dir()
    assert "city-content.json" in preview.diff_summary
    assert command("git", "rev-parse", "main", cwd=bare) == base
    assert any("preview_ready" in execution[0] for execution in pool.executions)
    await publisher.cleanup(preview.worktree)
    assert not preview.worktree.exists()


@pytest.mark.asyncio
async def test_build_failure_does_not_push_and_cleans_worktree(tmp_path):
    site, bare, base = make_site(tmp_path, fail_build=True)
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    expected_payload = payload()
    publisher = GitPublisher(FakePool(expected_payload), site, worktrees)
    publication_id = uuid4()

    with pytest.raises(CommandFailed):
        await publisher.build_preview(publication_id, expected_payload)

    assert command("git", "rev-parse", "main", cwd=bare) == base
    assert not (worktrees / str(publication_id)).exists()
