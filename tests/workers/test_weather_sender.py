"""Real SenderWorker plus PostgresSenderRepository methods; fake SQL sessions/transport only."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.infrastructure.test_weather_notification_repository import (
    ACTOR,
    NOW,
    PolicyStore,
)
from time_tl.application.sender_worker import ClaimedOutboxMessage, SenderWorker
from time_tl.application.weather_notifications import (
    WeatherNotificationPlanner,
    WeatherNotificationPreferences,
)
from time_tl.domain.weather import ForecastHour
from time_tl.infrastructure.db import weather_notifications as policy
from time_tl.infrastructure.db.workers import PostgresSenderRepository


async def sleep(_: float) -> None:
    pass


class Repository(PostgresSenderRepository):
    """Only owner admission/claim selection substituted; final weather policy is real."""

    def __init__(self, store, *, enabled=True, clock=lambda: NOW):
        super().__init__(
            store, stale_after_seconds=90, weather_notifications_enabled=enabled, clock=clock
        )
        self.store = store
        self.dead = []
        self.released = []
        self.preempt = False

    async def _locked(self, session, message_id):
        assert message_id == str(self.store.row.id)
        return self.store.row

    async def _active_owner_config_authorizes(self, *args, **kwargs):
        return True

    async def higher_priority_pending(self, event_type, now):
        return self.preempt

    async def claim_batch_skip_locked(self, now, *, limit):
        payload = {
            **deepcopy(self.store.row.payload),
            "telegram_chat_id": ACTOR,
            "confirmed_region_id": "h1",
            "confirmed_region_name": "Synthetic",
        }
        return (
            ClaimedOutboxMessage(
                str(self.store.row.id), self.store.row.event_type, payload, 0, self.store.recipient
            ),
        )

    async def mark_dead(self, message_id, *, error_code):
        self.dead.append(error_code)

    async def mark_retry(self, message_id, *, available_at, error_code):
        raise AssertionError("Unexpected retry: " + error_code)

    async def release_unattempted(self, message_ids, **kwargs):
        self.released.extend(message_ids)


class Transport:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message)


@pytest.fixture
def store(monkeypatch):
    value = PolicyStore()

    async def previous(session, *args):
        return value.snapshot

    monkeypatch.setattr(policy, "_bound_previous", previous)
    return value


async def test_real_sender_delivers_strict_weather_via_existing_fake_transport(store):
    repository, transport = Repository(store), Transport()
    assert (
        await SenderWorker(repository=repository, transport=transport, clock=lambda: NOW).run_once(
            NOW, sleep=sleep
        )
        == 1
    )
    assert len(transport.sent) == 1 and transport.sent[0].chat_id == ACTOR
    assert "MET Norway" in transport.sent[0].text and "CC BY 4.0" in transport.sent[0].text
    assert store.row.delivered_at == store.delivery.delivered_at == NOW
    assert store.row.claimed_at is None


@pytest.mark.parametrize("hour", [7, 12, 18])
async def test_scheduled_daily_forecast_reaches_transport(store, hour):
    now = datetime(2026, 11, 5, hour, 1, tzinfo=ZoneInfo("Europe/Kyiv")).astimezone(UTC)
    store.snapshot = replace(
        store.snapshot,
        fetched_at=now,
        provider_updated_at=now,
        expires_at=now + timedelta(hours=4),
        hours=(ForecastHour(now + timedelta(hours=1), temperature_c=9),),
    )
    store.pref.daily_forecast_enabled = True
    (plan,) = WeatherNotificationPlanner().plan(
        user_id=str(store.user.id),
        region_id="h1",
        locality="Synthetic",
        snapshot=store.snapshot,
        events=(),
        now=now,
        configuration_revision=1,
        preferences=WeatherNotificationPreferences(
            weather_enabled=True,
            forecast_requested=True,
            daily_forecast_enabled=True,
            revision=1,
        ),
        delivered_event_keys=frozenset(),
        known_notification_keys=frozenset(),
    )
    assert plan.kind == "WEATHER_DAILY"
    store.row.event_type, store.row.event_key = plan.kind, plan.key
    store.row.payload, store.row.claimed_at = plan.payload, now
    store.delivery.notification_key = plan.key.removeprefix("weather:")
    transport = Transport()
    sender = SenderWorker(
        repository=Repository(store, clock=lambda: now),
        transport=transport,
        clock=lambda: now,
    )
    assert await sender.run_once(now, sleep=sleep) == 1
    assert [message.chat_id for message in transport.sent] == [ACTOR]
    assert store.row.delivered_at == store.delivery.delivered_at == now


def test_bad_forecast_deadline_fails_closed():
    fence = policy.WeatherDeliveryFence(
        NOW.isoformat(),
        NOW + timedelta(minutes=10),
        NOW,
    )
    assert not fence.valid_at(NOW)


async def test_deployment_capability_off_prevents_weather_io(store):
    transport = Transport()
    assert (
        await SenderWorker(
            repository=Repository(store, enabled=False), transport=transport, clock=lambda: NOW
        ).run_once(NOW, sleep=sleep)
        == 0
    )
    assert transport.sent == []


async def test_final_clock_after_policy_locks_expires_claim_without_transport(store):
    times = iter([NOW, NOW + timedelta(minutes=11)])
    repository, transport = Repository(store, clock=lambda: next(times)), Transport()
    assert not await repository.deliver_if_authorized(
        str(store.row.id), NOW, lambda: transport.send(None), expected_recipient=store.recipient
    )
    assert transport.sent == []
    assert store.row.last_error_code == "final_weather_expired_or_stale"
    assert store.row.dispatch_started_at is None


async def test_recipient_change_after_claim_prevents_transport(store):
    repository, transport = Repository(store), Transport()
    store.user.telegram_user_id = 999
    assert (
        await SenderWorker(repository=repository, transport=transport, clock=lambda: NOW).run_once(
            NOW, sleep=sleep
        )
        == 0
    )
    assert transport.sent == []


async def test_new_air_alert_preempts_weather_before_transport(store):
    repository, transport = Repository(store), Transport()
    repository.preempt = True
    assert (
        await SenderWorker(repository=repository, transport=transport, clock=lambda: NOW).run_once(
            NOW, sleep=sleep
        )
        == 0
    )
    assert transport.sent == [] and repository.released == [str(store.row.id)]


@pytest.mark.parametrize("case", ["unknown_type", "unknown_payload", "forged_kind"])
async def test_strict_namespace_and_renderer_reject_before_io(store, case):
    if case == "unknown_type":
        store.row.event_type = "WEATHER_OFFICIAL_CLEAR"
    elif case == "unknown_payload":
        store.row.payload["secret"] = "not accepted"
    else:
        store.row.payload["kind"] = "WEATHER_DAILY"
    repository, transport = Repository(store), Transport()
    assert (
        await SenderWorker(repository=repository, transport=transport, clock=lambda: NOW).run_once(
            NOW, sleep=sleep
        )
        == 0
    )
    assert transport.sent == [] and repository.dead == ["invalid_weather_payload"]
