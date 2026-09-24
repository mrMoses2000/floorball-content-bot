from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from floorball_bot import auth


@pytest.mark.asyncio
async def test_telegram_only_account_can_attach_its_verified_contact(monkeypatch):
    user_id = uuid4()
    connection = AsyncMock()
    connection.fetchrow.side_effect = [
        None,
        {"id": user_id, "telegram_id": 191148810},
    ]
    connection.fetchval.return_value = user_id
    actor = object()
    monkeypatch.setattr(auth, "get_actor_by_telegram_id", AsyncMock(return_value=actor))

    result = await auth.bind_self_contact(
        connection,
        sender_id=191148810,
        contact_user_id=191148810,
        raw_phone="+7 700 123 45 67",
    )

    assert result is actor
    assert connection.execute.await_args.args[1:] == (
        user_id,
        191148810,
        "+77001234567",
    )


@pytest.mark.asyncio
async def test_telegram_only_account_cannot_attach_someone_elses_contact():
    connection = AsyncMock()
    with pytest.raises(ValueError, match="contact must belong"):
        await auth.bind_self_contact(
            connection,
            sender_id=191148810,
            contact_user_id=123,
            raw_phone="+7 700 123 45 67",
        )
    connection.fetchrow.assert_not_awaited()
