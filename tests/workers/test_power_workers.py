"""Offline worker and real-policy fake-port tests; never contacts Telegram/providers."""

from __future__ import annotations

import importlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from time_tl.application.power_notification_worker import render_power_message
from time_tl.application.power_outage_worker import PowerOutageWorker
from time_tl.application.sender_worker import DeliveryRecipient
from time_tl.domain.power_outages.models import (
    PowerDaySchedule,
    PowerInterval,
    PowerProvider,
    PowerScheduleSnapshot,
    PowerState,
    ScheduleKind,
    SourceStatus,
)
from time_tl.domain.power_outages.normalization import day_bounds
from time_tl.infrastructure.db import models
from time_tl.infrastructure.db.power import (
    PostgresPowerRepository,
    _joined_windows,
    power_delivery_policy,
)
from time_tl.infrastructure.db.workers import PostgresSenderRepository
from time_tl.infrastructure.power_outages.base import PowerFetchError, PowerFetchErrorCode

NOW = datetime(2026, 11, 4, 10, tzinfo=UTC)


def day(provider=PowerProvider.DTEK):
    lower, upper = day_bounds(NOW.date())
    start, end = NOW + timedelta(minutes=30), NOW + timedelta(minutes=90)
    return PowerDaySchedule(
        provider,
        "1.1",
        NOW.date(),
        ScheduleKind.CONFIRMED_DAY,
        (
            PowerInterval("1.1", lower, start, PowerState.POWER_EXPECTED),
            PowerInterval("1.1", start, end, PowerState.OUTAGE_CONFIRMED),
            PowerInterval("1.1", end, upper, PowerState.POWER_EXPECTED),
        ),
    )


def snapshot():
    return PowerScheduleSnapshot(PowerProvider.DTEK, NOW, None, (day(),), SourceStatus.FRESH)


class Lease:
    def __init__(self, acquired=True):
        self.acquired, self.acquires, self.renewals = acquired, 0, 0

    async def acquire(self):
        self.acquires += 1
        return self.acquired

    async def renew(self):
        self.renewals += 1
        return self.acquired


class Provider:
    provider_id = PowerProvider.DTEK

    def __init__(self, error=None):
        self.error, self.calls = error, 0

    async def fetch_schedules(self):
        self.calls += 1
        if self.error:
            raise self.error
        return snapshot()


class Repository:
    def __init__(self):
        self.slot, self.stored = 1, []

    async def acquire_poll_slot(self, provider, now, interval_seconds):
        result, self.slot = self.slot, None
        return result

    async def store_snapshot(self, value, now, interval_seconds, **kwargs):
        self.stored.append((value, kwargs))


async def test_one_acquisition_and_central_cooldown_not_per_user():
    provider, repository, lease = Provider(), Repository(), Lease()
    worker = PowerOutageWorker(provider, repository, lease, clock=lambda: NOW)
    assert await worker.run_once(NOW)
    assert not await worker.run_once(NOW)
    assert lease.acquires == provider.calls == 1 and lease.renewals == 2
    assert repository.stored[0][1]["expected_configuration_revision"] == 1


async def test_missing_lease_does_not_fetch_or_write():
    provider, repository = Provider(), Repository()
    worker = PowerOutageWorker(provider, repository, Lease(False), clock=lambda: NOW)
    assert not await worker.run_once(NOW)
    assert provider.calls == 0 and repository.stored == []


@pytest.mark.parametrize(
    "error,code,permanent,retry",
    [
        (
            PowerFetchError(
                PowerFetchErrorCode.RATE_LIMITED, status_code=429, retry_after_seconds=1200
            ),
            "RATE_LIMITED",
            False,
            1200,
        ),
        (
            PowerFetchError(PowerFetchErrorCode.ACCESS_DENIED, status_code=403),
            "ACCESS_DENIED",
            True,
            None,
        ),
        (RuntimeError("private-response-never-log"), "FETCH_UNAVAILABLE", False, None),
    ],
)
async def test_fetch_failures_are_redacted_and_persist_retry_policy(error, code, permanent, retry):
    repository = Repository()
    worker = PowerOutageWorker(Provider(error), repository, Lease(), clock=lambda: NOW)
    assert await worker.run_once(NOW)
    result, options = repository.stored[0]
    assert result.status is SourceStatus.UNAVAILABLE and result.error_code == code
    assert result.days == () and options["permanent"] is permanent
    assert options["retry_after_seconds"] == retry


async def test_nonofficial_snapshot_rejected_before_database_access():
    def forbidden():
        raise AssertionError("must reject provenance before DB")

    repository = PostgresPowerRepository(forbidden)
    with pytest.raises(ValueError, match="nonofficial"):
        await repository.store_snapshot(SimpleNamespace(source_kind="bezsvitla"), NOW)


def records():
    user_id, snapshot_id, event_id, outbox_id = uuid4(), uuid4(), uuid4(), uuid4()
    schedule = day()
    start, end = schedule.intervals[1].starts_at, schedule.intervals[1].ends_at
    user = models.User(id=user_id, telegram_user_id=1001)
    subscription = models.UserPowerSubscription(
        user_id=user_id,
        provider_id="dtek_dnipro",
        group_id="1.1",
        enabled=True,
        outage_notifications_enabled=True,
        restoration_notifications_enabled=True,
        outage_lead_minutes=30,
        restoration_lead_minutes=15,
        revision=1,
    )
    provider = models.PowerProviderState(
        provider_id="dtek_dnipro", enabled=True, source_status="FRESH", last_success_at=NOW
    )
    stored = models.PowerScheduleSnapshotRow(
        id=snapshot_id,
        provider_id="dtek_dnipro",
        group_id="1.1",
        valid_for_date=NOW.date(),
        schedule_type="CONFIRMED_DAY",
        mapping_version="1",
        revision=1,
        fetched_at=NOW,
        is_current=True,
        source_kind="official",
        publication_status="PLANNED",
    )
    event = models.PowerNotificationEvent(
        id=event_id,
        user_id=user_id,
        snapshot_id=snapshot_id,
        outbox_id=outbox_id,
        status="PENDING",
        transition_type="POWER_OUTAGE_UPCOMING",
        event_key="power:test",
        subscription_revision=1,
        schedule_revision=1,
        transition_at=start,
        planned_send_at=NOW,
    )
    row = models.OutboxMessage(
        id=outbox_id,
        aggregate_type="power",
        aggregate_id=str(user_id),
        event_type=event.transition_type,
        event_key="power:test",
        payload={
            "power_event_id": str(event_id),
            "source_kind": "official",
            "provider_id": "dtek_dnipro",
            "group_id": "1.1",
            "subscription_revision": 1,
            "schedule_revision": 1,
            "valid_until": start.isoformat(),
            "transition_at": start.isoformat(),
            "outage_starts_at": start.isoformat(),
            "outage_ends_at": end.isoformat(),
            "schedule_bindings": [{"id": str(snapshot_id), "revision": 1}],
        },
    )
    intervals = [
        models.PowerScheduleIntervalRow(
            group_id="1.1", starts_at=item.starts_at, ends_at=item.ends_at, state=item.state.value
        )
        for item in schedule.intervals
    ]
    return {
        models.User: user,
        models.UserPowerSubscription: subscription,
        models.PowerProviderState: provider,
        models.PowerScheduleSnapshotRow: stored,
        models.PowerNotificationEvent: event,
        models.PowerScheduleIntervalRow: intervals,
    }, row


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.value if type(self.value) is list else [self.value]


class Session:
    def __init__(self, values):
        self.values, self.queries = values, []

    async def execute(self, query):
        self.queries.append(query)
        return Result(self.values[query.column_descriptions[0]["entity"]])


async def test_new_neighboring_date_invalidates_previously_bound_notification():
    values, row = records()
    old = values[models.PowerScheduleSnapshotRow]
    neighbor = models.PowerScheduleSnapshotRow(id=uuid4(), fetched_at=NOW)

    class AddedNeighbor(Session):
        async def execute(self, query):
            if (
                query.column_descriptions[0]["entity"] is models.PowerScheduleSnapshotRow
                and query._limit_clause is not None
            ):
                return Result([old, neighbor])
            return await super().execute(query)

    assert (
        await power_delivery_policy(
            AddedNeighbor(values), row, NOW, stale_after_seconds=1800, lock=True
        )
        == "REJECTED"
    )


async def test_actual_policy_binds_user_source_sub_revision_snapshots_and_locked_time_fence():
    values, row = records()
    session, fences = Session(values), []
    assert (
        await power_delivery_policy(
            session,
            row,
            NOW,
            stale_after_seconds=1800,
            lock=True,
            expected_recipient=DeliveryRecipient(1001),
            fences=fences,
        )
        == "AUTHORIZED"
    )
    assert len(fences) == 1 and fences[0].valid_at(NOW)
    assert not fences[0].valid_at(NOW + timedelta(minutes=31))
    for query in session.queries:
        if query.column_descriptions[0]["entity"] is not models.PowerScheduleIntervalRow:
            assert query._for_update_arg is not None
    text = render_power_message(row.event_type, row.payload, NOW)
    assert "через 30 хв" in text and "за графіком" in text
    assert "світло вже є" not in text


@pytest.mark.parametrize(
    "kind",
    [
        "disabled",
        "subscription_revision",
        "snapshot_revision",
        "withdrawn",
        "wrong_group",
        "wrong_recipient",
        "expired",
        "unofficial",
        "malformed_bindings",
        "duplicate_binding",
        "payload_revision_bool",
        "tampered_interval",
    ],
)
async def test_final_policy_rejects_changed_or_malformed_state(kind):
    values, row = records()
    now, recipient = NOW, DeliveryRecipient(1001)
    if kind == "disabled":
        values[models.UserPowerSubscription].enabled = False
    elif kind == "subscription_revision":
        values[models.UserPowerSubscription].revision = 2
    elif kind == "snapshot_revision":
        values[models.PowerScheduleSnapshotRow].revision = 2
    elif kind == "withdrawn":
        values[models.PowerScheduleSnapshotRow].is_current = False
    elif kind == "wrong_group":
        row.payload["group_id"] = "2.1"
    elif kind == "wrong_recipient":
        recipient = DeliveryRecipient(2002)
    elif kind == "expired":
        now += timedelta(minutes=31)
    elif kind == "unofficial":
        row.payload["source_kind"] = "bezsvitla"
    elif kind == "malformed_bindings":
        row.payload["schedule_bindings"] = []
    elif kind == "duplicate_binding":
        row.payload["schedule_bindings"] *= 2
    elif kind == "payload_revision_bool":
        row.payload["subscription_revision"] = True
    else:
        row.payload["outage_ends_at"] = (NOW + timedelta(hours=6)).isoformat()
    assert (
        await power_delivery_policy(
            Session(values),
            row,
            now,
            stale_after_seconds=1800,
            lock=True,
            expected_recipient=recipient,
        )
        == "REJECTED"
    )


@pytest.mark.parametrize("kind", ["source_stale", "source_error", "snapshot_stale"])
async def test_source_loss_defers_same_future_event_instead_of_claiming_current_schedule(kind):
    values, row = records()
    if kind == "source_stale":
        values[models.PowerProviderState].last_success_at = NOW - timedelta(hours=1)
    elif kind == "source_error":
        values[models.PowerProviderState].source_status = "UNAVAILABLE"
    else:
        values[models.PowerScheduleSnapshotRow].fetched_at = NOW - timedelta(hours=1)
    assert (
        await power_delivery_policy(Session(values), row, NOW, stale_after_seconds=1800, lock=True)
        == "TEMPORARY"
    )


async def test_disabled_global_flag_prevents_power_recipient_or_provider_queries():
    _, row = records()
    assert not await PostgresSenderRepository._is_currently_deliverable(
        None, row, authorization_at=NOW, stale_after=timedelta(seconds=120), power_enabled=False
    )


def test_zero_lead_renderer_and_fence_are_exact_boundary_only():
    values, row = records()
    boundary = datetime.fromisoformat(row.payload["transition_at"])
    assert "Зараз за графіком" in render_power_message(row.event_type, row.payload, boundary)
    with pytest.raises(ValueError, match="expired"):
        render_power_message(row.event_type, row.payload, boundary + timedelta(microseconds=1))


def test_frozen_0011_matches_orm_columns_constraints_and_six_provider_seeds():
    migration = importlib.import_module("migrations.versions.0011_power_outages")
    assert migration.down_revision == "0010_personal_calendar_overrides"
    for frozen in migration._schema():
        current = models.Base.metadata.tables[frozen.name]
        assert set(frozen.c.keys()) == set(current.c.keys())
        assert {item.name for item in frozen.constraints} == {
            item.name for item in current.constraints
        }
        assert {item.name for item in frozen.indexes} == {item.name for item in current.indexes}
    assert len(PowerProvider) == 6


def test_joined_windows_do_not_invent_a_restore_at_midnight():
    today = day()
    lower, upper = day_bounds(today.local_date)
    tomorrow_date = today.local_date + timedelta(days=1)
    _, tomorrow_end = day_bounds(tomorrow_date)
    first = replace(
        today, intervals=(PowerInterval("1.1", lower, upper, PowerState.OUTAGE_CONFIRMED),)
    )
    second = replace(
        today,
        local_date=tomorrow_date,
        intervals=(PowerInterval("1.1", upper, tomorrow_end, PowerState.OUTAGE_CONFIRMED),),
    )
    assert _joined_windows((first, second), NOW) == [[lower.isoformat(), tomorrow_end.isoformat()]]
