import hashlib
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from floorball_bot import publisher as module
from floorball_bot.errors import RetryableProviderError, ValidationBlocked
from floorball_bot.publisher import GitPublisher


@pytest.mark.asyncio
@pytest.mark.parametrize("asset_matches", [True, False])
@pytest.mark.parametrize("hook_status", [200, 204])
async def test_plesk_deploy_verifies_public_bytes_instead_of_accepting_hook_response(
    tmp_path, monkeypatch, asset_matches, hook_status
):
    index = "<html>Verified release</html>\n"
    asset = b"reviewed script"
    observed = []

    async def respond(request):
        observed.append(request.path)
        if request.path == "/hook":
            assert request.method == "POST"
            return web.Response(status=hook_status)
        if request.path == "/assets/release.js":
            return web.Response(body=asset if asset_matches else b"stale script")
        return web.Response(text=index)

    app = web.Application()
    app.router.add_post("/hook", respond)
    app.router.add_get("/{path:.*}", respond)
    async with TestServer(app) as server:

        class RoutedClient:
            def __init__(self, **kwargs):
                self.client = ClientSession(**kwargs)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                await self.client.close()

            def get(self, url, **kwargs):
                parts = urlsplit(url)
                return self.client.get(server.make_url(parts.path or "/"), **kwargs)

            def post(self, url, **kwargs):
                parts = urlsplit(url)
                return self.client.post(server.make_url(parts.path or "/"), **kwargs)

        async def command(*_args, **_kwargs):
            return index

        async def pause(_seconds):
            pass

        monkeypatch.setattr(module, "ClientSession", RoutedClient)
        monkeypatch.setattr(module, "run_command", command)
        monkeypatch.setattr(module.asyncio, "sleep", pause)
        publisher = GitPublisher(
            None,
            tmp_path,
            tmp_path / "worktrees",
            plesk_static_webhook_url="https://srv-plesk38.ps.kz:8443/hook?secret=test",
        )
        row = {
            "id": uuid4(),
            "static_commit": "a" * 40,
            "change_manifest": {
                "generated": [
                    {
                        "path": "app/dist/assets/release.js",
                        "deleted": False,
                        "byteSize": len(asset),
                        "sha256": hashlib.sha256(asset).hexdigest(),
                    },
                ]
            },
        }
        if asset_matches:
            await publisher._deploy_plesk(row)
        else:
            with pytest.raises(RetryableProviderError, match="does not match"):
                await publisher._deploy_plesk(row)
        assert observed[0] == "/hook"
        assert "/assets/release.js" in observed


def test_plesk_hook_does_not_allow_an_arbitrary_host(tmp_path):
    with pytest.raises(ValidationBlocked, match="configured HTTPS"):
        GitPublisher(
            None,
            tmp_path,
            tmp_path / "worktrees",
            plesk_static_webhook_url="http://untrusted.invalid/hook",
        )
