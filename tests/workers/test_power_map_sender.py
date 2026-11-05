"""Map events cannot enter the sender through its legacy permissive fallback."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from time_tl.config import Settings
from time_tl.infrastructure.db import workers

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    "kind,aggregate,enabled",
    [
        ("POWER_MAP_OUTAGE_UPCOMING", "power_map", False),
        ("POWER_MAP_UNKNOWN", "power_map", True),
        ("NOT_SUPPORTED", "power_map", True),
        ("POWER_MAP_UNKNOWN", "schedule", True),
    ],
)
async def test_map_namespace_fails_closed_before_db(kind, aggregate, enabled, monkeypatch):
    policy = AsyncMock(side_effect=AssertionError("unadmitted event reached policy"))
    monkeypatch.setattr(workers, "map_delivery_policy", policy)
    allowed = await workers.PostgresSenderRepository._is_currently_deliverable(
        None,
        SimpleNamespace(event_type=kind, aggregate_type=aggregate),
        authorization_at=NOW,
        stale_after=timedelta(minutes=30),
        power_map_notifications_enabled=enabled,
    )
    assert not allowed
    policy.assert_not_awaited()


async def test_explicit_map_capability_uses_separate_final_policy(monkeypatch):
    policy = AsyncMock(return_value="AUTHORIZED")
    official = AsyncMock(side_effect=AssertionError("API event entered official path"))
    monkeypatch.setattr(workers, "map_delivery_policy", policy)
    monkeypatch.setattr(workers, "power_delivery_policy", official)
    row = SimpleNamespace(event_type="POWER_MAP_OUTAGE_UPCOMING", aggregate_type="power_map")
    fences = []
    recipient = SimpleNamespace(chat_id=42)
    allowed = await workers.PostgresSenderRepository._is_currently_deliverable(
        None,
        row,
        authorization_at=NOW,
        stale_after=timedelta(minutes=30),
        lock_policy_rows=True,
        expected_recipient=recipient,
        power_map_notifications_enabled=True,
        power_map_fences=fences,
    )
    assert allowed
    policy.assert_awaited_once_with(
        None,
        row,
        NOW,
        stale_after_seconds=1800,
        lock=True,
        expected_recipient=recipient,
        fences=fences,
    )
    official.assert_not_awaited()


def test_reminder_feature_requires_explicit_api_and_power_flags():
    base = {"DATABASE_URL": "postgresql+asyncpg://unused/unused", "_env_file": None}
    assert not Settings(**base).power_map_reminders_enabled
    with pytest.raises(ValidationError, match="map reminders require"):
        Settings(**base, POWER_MAP_REMINDERS_ENABLED=True)
    config = Settings(
        **base,
        POWER_MAP_REMINDERS_ENABLED=True,
        POWER_OUTAGES_ENABLED=True,
        POWER_ALERTS_ENERGY_API_ENABLED=True,
    )
    assert config.power_notification_default_outage_lead == 10
    assert config.power_notification_default_restore_lead == 10
