"""Revoked public authority must stop before user/cache planning or delivery."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import uuid4

import pytest

from time_tl.infrastructure.db import workers
from time_tl.infrastructure.db.power_map_notifications import PostgresPowerMapNotificationRepository

NOW = datetime(2026, 11, 4, 21, tzinfo=UTC)


async def test_planner_authority_rejection_precedes_user_and_cache_locks():
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.begin.return_value = session
    session.get = AsyncMock(side_effect=AssertionError("User touched before authority"))
    guard = AsyncMock(side_effect=RuntimeError("authority revoked"))
    repository = PostgresPowerMapNotificationRepository(
        Mock(return_value=session),
        authority_guard=guard,
    )
    with pytest.raises(RuntimeError, match="authority revoked"):
        await repository._plan_user(uuid4(), NOW)
    guard.assert_awaited_once_with(session, NOW)
    session.get.assert_not_awaited()
    session.execute.assert_not_called()
    session.add.assert_not_called()


async def test_sender_authority_rejection_precedes_map_payload_authorization(monkeypatch):
    policy = AsyncMock(side_effect=AssertionError("revoked authority reached map policy"))
    monkeypatch.setattr(workers, "map_delivery_policy", policy)
    guard = AsyncMock(side_effect=RuntimeError("authority revoked"))
    row = SimpleNamespace(event_type="POWER_MAP_OUTAGE_UPCOMING", aggregate_type="power_map")
    with pytest.raises(RuntimeError, match="authority revoked"):
        await workers.PostgresSenderRepository._is_currently_deliverable(
            None,
            row,
            authorization_at=NOW,
            stale_after=timedelta(minutes=30),
            power_map_notifications_enabled=True,
            power_map_authority_guard=guard,
        )
    guard.assert_awaited_once_with(None, NOW)
    policy.assert_not_awaited()


async def test_disabled_map_capability_never_invokes_a_public_guard(monkeypatch):
    guard = AsyncMock(side_effect=AssertionError("disabled capability used"))
    policy = AsyncMock(side_effect=AssertionError("disabled capability reached policy"))
    monkeypatch.setattr(workers, "map_delivery_policy", policy)
    row = SimpleNamespace(event_type="POWER_MAP_OUTAGE_UPCOMING", aggregate_type="power_map")
    assert not await workers.PostgresSenderRepository._is_currently_deliverable(
        None,
        row,
        authorization_at=NOW,
        stale_after=timedelta(minutes=30),
        power_map_authority_guard=guard,
    )
    guard.assert_not_awaited()
    policy.assert_not_awaited()


async def test_sender_revoked_while_waiting_for_policy_locks_never_sends(monkeypatch):
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.begin.return_value = session
    row = SimpleNamespace(
        event_type="POWER_MAP_OUTAGE_UPCOMING", aggregate_type="power_map",
        claimed_at=NOW, dispatch_started_at=None,
    )
    guard = AsyncMock(side_effect=[None, RuntimeError("authority revoked after lock wait")])
    policy = AsyncMock(return_value="AUTHORIZED")
    monkeypatch.setattr(workers, "map_delivery_policy", policy)
    repository = workers.PostgresSenderRepository(
        Mock(return_value=session), stale_after_seconds=120, clock=lambda: NOW,
        power_map_notifications_enabled=True, power_map_authority_guard=guard,
    )
    repository._locked = AsyncMock(return_value=row)
    repository._active_owner_config_authorizes = AsyncMock(return_value=True)
    deliver = AsyncMock()
    with pytest.raises(RuntimeError, match="after lock wait"):
        await repository.deliver_if_authorized(
            str(uuid4()), NOW, deliver, expected_recipient=SimpleNamespace(chat_id=42),
        )
    assert guard.await_count == 2
    assert policy.await_count == 1
    deliver.assert_not_awaited()
