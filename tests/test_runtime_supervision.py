from __future__ import annotations

import asyncio

import pytest
from aiogram.exceptions import TelegramNetworkError

from floorball_bot.cli import supervise_bot_tasks
from floorball_bot.telegram import TelegramIngress, canonical_command, run_outbox


def test_telegram_menu_aliases_use_botfather_compatible_commands():
    assert canonical_command("/coach_form@floorball_site_agent_bot") == "/coach-form"
    assert canonical_command("/photos_ready") == "/photos-ready"
    assert canonical_command("/city_applications extra") == "/city-applications"


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


class FlappingOutboxPool:
    def __init__(self, stop: asyncio.Event) -> None:
        self.stop = stop
        self.fetch_attempts = 0
        self.execute_calls: list[tuple[str, tuple]] = []

    async def execute(self, query: str, *args) -> None:
        self.execute_calls.append((query, args))


@pytest.mark.asyncio
async def test_outbox_retries_transient_db_timeout_without_crashing(monkeypatch):
    stop = asyncio.Event()
    flapping_pool = FlappingOutboxPool(stop)
    attempts = 0

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def mock_transaction(_pool):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("connection fetchrow timed out")
        stop.set()
        yield None

    monkeypatch.setattr("floorball_bot.telegram.transaction", mock_transaction)

    bot = NetworkFlapBot()
    # Should not raise TimeoutError or RuntimeError
    await run_outbox(bot, flapping_pool, "test-worker", stop)

    assert attempts >= 1
    assert stop.is_set()


@pytest.mark.asyncio
async def test_outbox_handles_telegram_retry_after_without_crashing(monkeypatch):
    stop = asyncio.Event()
    flapping_pool = FlappingOutboxPool(stop)
    attempts = 0

    from contextlib import asynccontextmanager

    fake_event = {
        "id": "11111111-1111-1111-1111-111111111111",
        "event_type": "telegram_message",
        "payload": {"chat_id": 12345, "text": "Hello"},
        "attempts": 1,
    }

    class FakeOutboxConn:
        async def fetchrow(self, *args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return fake_event
            stop.set()
            return None

    @asynccontextmanager
    async def mock_transaction(_pool):
        yield FakeOutboxConn()

    monkeypatch.setattr("floorball_bot.telegram.transaction", mock_transaction)

    class RateLimitingBot:
        async def send_message(self, **kwargs):
            raise TelegramRetryAfter(method=None, message="Too Many Requests", retry_after=0.1)

    bot = RateLimitingBot()
    await run_outbox(bot, flapping_pool, "test-worker", stop)

    assert stop.is_set()
    assert any("TelegramRetryAfter" in str(args) for _, args in flapping_pool.execute_calls)


class PoisonUpdateBot:
    def __init__(self, updates: list) -> None:
        self.updates = updates
        self.ingress: RetryingIngress | None = None
        self.calls = 0

    async def __call__(self, method):
        self.calls += 1
        if self.calls == 1:
            return self.updates
        assert self.ingress is not None
        await self.ingress.stop()
        return []


@pytest.mark.asyncio
async def test_ingress_survives_failed_update_and_increments_offset(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    from aiogram.types import Update

    update1 = MagicMock(spec=Update)
    update1.update_id = 100
    update2 = MagicMock(spec=Update)
    update2.update_id = 101

    bot = PoisonUpdateBot([update1, update2])
    ingress = RetryingIngress(bot)
    bot.ingress = ingress

    async def fake_heartbeat(*_args, **_kwargs):
        return None

    monkeypatch.setattr("floorball_bot.telegram.record_heartbeat", fake_heartbeat)

    accepted: list[int] = []

    async def mock_accept(update):
        if update.update_id == 100:
            raise ValueError("malformed or poison update")
        accepted.append(update.update_id)

    ingress.accept = mock_accept
    ingress.pool = MagicMock()
    ingress.pool.execute = AsyncMock()

    await ingress.run()

    # The failing update didn't crash ingress, update 101 was processed, and offset advanced past 101
    assert accepted == [101]
    assert ingress.offset == 102

