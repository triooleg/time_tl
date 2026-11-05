from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

from tests.workers.test_sender_worker import Repo, Transport, record
from time_tl.application.notification_priority import notification_priority
from time_tl.application.sender_worker import SenderWorker
from time_tl.infrastructure.db.outbox import pending_outbox_query


def test_air_is_first_then_lessons_then_power_then_weather():
    assert [
        notification_priority(x)
        for x in (
            "ALERT_STARTED",
            "ALL_CLEAR_CONFIRMED",
            "RETURN_TO_ACTIVE_LESSON",
            "LESSON_REMINDER",
            "POWER_OUTAGE_REMINDER",
            "WEATHER_CHANGE",
        )
    ] == [0, 0, 1, 1, 2, 3]
    query = str(
        pending_outbox_query(datetime.now(UTC)).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "ORDER BY CASE" in query and "FOR UPDATE SKIP LOCKED" in query


@pytest.mark.asyncio
async def test_new_air_alert_interrupts_previously_claimed_lower_priority_batch():
    repo = Repo((record(), record()))
    repo.higher_priority_pending = AsyncMock(side_effect=lambda event, probe: probe > now)
    transport = Transport()
    now = datetime(2026, 9, 21, 6, tzinfo=UTC)
    ticks = iter((now, now + timedelta(seconds=1)))
    worker = SenderWorker(repository=repo, transport=transport, clock=lambda: next(ticks))
    assert await worker.run_once(now, sleep=AsyncMock()) == 1
    assert len(transport.sent) == 1
    assert repo.released == [(("m1",), now, now + timedelta(seconds=1), "higher_priority_pending")]
    assert repo.higher_priority_pending.await_args.args[1] == now + timedelta(seconds=1)
