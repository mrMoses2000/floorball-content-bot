from __future__ import annotations

import asyncio

import pytest
from aiogram.exceptions import TelegramNetworkError

from floorball_bot.cli import supervise_bot_tasks
from floorball_bot.telegram import TelegramIngress


class WaitingIngress:
    def __init__(self) -> None:
        self.stopped = asyncio.Event()

    async def run(self) -> None:
        await self.stopped.wait()

    async def stop(self) -> None:
        self.stopped.set()


class FailingIngress(WaitingIngress):
    async def run(self) -> None:
        raise ConnectionError("polling failed")


async def wait_for_stop(stop: asyncio.Event) -> None:
    await stop.wait()


@pytest.mark.asyncio
async def test_supervisor_fails_process_when_polling_crashes():
    stop = asyncio.Event()
    ingress = FailingIngress()

    with pytest.raises(RuntimeError, match="telegram-ingress failed") as captured:
        await supervise_bot_tasks(
            ingress=ingress,
            outbox_coro=wait_for_stop(stop),
            stop=stop,
        )

    assert isinstance(captured.value.__cause__, ConnectionError)
    assert stop.is_set()
    assert ingress.stopped.is_set()


@pytest.mark.asyncio
async def test_supervisor_stops_both_loops_gracefully():
    stop = asyncio.Event()
    ingress = WaitingIngress()

    task = asyncio.create_task(
        supervise_bot_tasks(
            ingress=ingress,
            outbox_coro=wait_for_stop(stop),
            stop=stop,
        )
    )
    await asyncio.sleep(0)
    stop.set()
    await task

    assert ingress.stopped.is_set()


@pytest.mark.asyncio
async def test_supervisor_rejects_silent_outbox_exit():
    stop = asyncio.Event()
    ingress = WaitingIngress()

    async def stopped_outbox() -> None:
        return None

    with pytest.raises(RuntimeError, match="telegram-outbox stopped unexpectedly"):
        await supervise_bot_tasks(
            ingress=ingress,
            outbox_coro=stopped_outbox(),
            stop=stop,
        )


class RetryingIngress(TelegramIngress):
    def __init__(self, bot) -> None:
        super().__init__(bot, object())
        self.retries: list[str] = []

    async def prepare_long_polling(self) -> None:
        return None

    async def _wait_polling_retry(self, _delay: float, error_class: str) -> None:
        self.retries.append(error_class)


class NetworkFlapBot:
    def __init__(self) -> None:
        self.calls = 0
        self.ingress: RetryingIngress | None = None

    async def __call__(self, method):
        self.calls += 1
        if self.calls == 1:
            raise TelegramNetworkError(method=method, message="temporary network failure")
        assert self.ingress is not None
        await self.ingress.stop()
        return []


@pytest.mark.asyncio
async def test_polling_retries_network_failure_without_stopping_task(monkeypatch):
    bot = NetworkFlapBot()
    ingress = RetryingIngress(bot)
    bot.ingress = ingress

    async def fake_heartbeat(*_args, **_kwargs):
        return None

    monkeypatch.setattr("floorball_bot.telegram.record_heartbeat", fake_heartbeat)

    await ingress.run()

    assert bot.calls == 2
    assert ingress.retries == ["TelegramNetworkError"]
