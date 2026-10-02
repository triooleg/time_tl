"""No live database or Telegram: bounded calendar-only maintenance lifecycle."""
import asyncio
from datetime import UTC
from unittest.mock import AsyncMock

import pytest

from time_tl.runtime import services


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_retention_one_bounded_batch_hourly_and_cancellation(monkeypatch, failed):
    repository = AsyncMock()
    if failed:
        repository.cleanup_invitations.side_effect = RuntimeError("private details")
    sleep = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr(services.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await services._calendar_invitation_retention_loop(repository)
    repository.cleanup_invitations.assert_awaited_once()
    args, kwargs = repository.cleanup_invitations.call_args
    assert args[0].tzinfo is UTC and kwargs == {"limit": 200}
    sleep.assert_awaited_once_with(3600)
