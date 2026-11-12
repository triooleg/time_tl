"""Head9 map admission and durable planning using synthetic SQL-state ports."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Insert

from time_tl.application.sender_worker import DeliveryRecipient
from time_tl.domain.power_outages.map import MapArea, RegionMap
from time_tl.domain.power_outages.map_reminders import OUTAGE, plan_map_reminders
from time_tl.domain.power_outages.models import (
    GROUPS,
    PowerDaySchedule,
    PowerInterval,
    PowerProvider,
    PowerPublicationStatus,
    PowerScheduleView,
    PowerSourceKind,
    PowerState,
    PowerSubscription,
    ScheduleKind,
    SourceStatus,
)
from time_tl.domain.power_outages.normalization import KYIV, day_bounds
from time_tl.infrastructure.db import power_map_notifications as module
from time_tl.infrastructure.db.models import OutboxMessage, RuntimeSetting, User
from time_tl.infrastructure.db.power_map import REGION_PREFIX, _empty_region
from time_tl.infrastructure.db.power_map_notifications import (
    CHANGED,
    EVENT_TYPES,
    MapDeliveryFence,
    PostgresPowerMapNotificationRepository,
    disable_map_for_failed_recipient,
    map_delivery_policy,
    reminder_state_key,
    render_map_outbox,
)
from time_tl.infrastructure.db.power_map_preferences import preference_key

NOW = datetime(2026, 11, 4, 6, 20, tzinfo=UTC)
IDENTIFIER = UUID("00000000-0000-4000-8000-000000000011")
CHAT = 12345
PROVIDER = PowerProvider.DTEK
DAY = NOW.astimezone(KYIV).date()


def view(day=DAY, *, outage_hour=8, fetched=NOW):
    lower, upper = day_bounds(day)
    start = lower + timedelta(hours=outage_hour, minutes=30)
    end = start + timedelta(hours=2)
    schedule = PowerDaySchedule(
        PROVIDER,
        "1.1",
        day,
        ScheduleKind.CONFIRMED_DAY,
        (
            PowerInterval("1.1", lower, start, PowerState.POWER_EXPECTED),
            PowerInterval("1.1", start, end, PowerState.OUTAGE_CONFIRMED),
            PowerInterval("1.1", end, upper, PowerState.POWER_EXPECTED),
        ),
    )
    return PowerScheduleView(
        PROVIDER,
        "1.1",
        SourceStatus.FRESH,
        fetched,
        fetched,
        schedule,
        source_kind=PowerSourceKind.ALERTS_ENERGY_API,
    )


def context(*, sub=None, views=None, checked=NOW):
    subscription = sub or PowerSubscription(
        str(IDENTIFIER), PROVIDER, "1.1", True, True, True, 10, 10, 2
    )
    data = SimpleNamespace(fetched_at=NOW, record_checked_at=lambda *a, **kw: checked)
    return module._Context(
        User(id=IDENTIFIER, telegram_user_id=CHAT),
        subscription,
        views or (view(), view(DAY + timedelta(days=1))),
        SimpleNamespace(data=data),
        1,
        1800,
    )


def reminder_payload(ctx=None):
    ctx = ctx or context()
    event = next(
        event for event in plan_map_reminders(ctx.views, NOW) if event.event_type == OUTAGE
    )
    payload = module._identity_payload(ctx, event.expires_at)
    payload["event"] = event.to_wire()
    return payload


def row(payload=None, kind=OUTAGE):
    return OutboxMessage(
        id=uuid4(),
        event_key="synthetic",
        aggregate_type="power_map",
        aggregate_id=str(IDENTIFIER),
        event_type=kind,
        payload=payload or reminder_payload(),
        attempts=0,
    )


class Result:
    def __init__(self, rows=(), one=None):
        self.rows, self.one = rows, one

    def scalars(self):
        return self

    def all(self):
        return list(self.rows)

    def scalar_one_or_none(self):
        return self.one


class Store:
    def __init__(self):
        self.rows, self.outbox, self.statements = {}, {}, []

    def __call__(self):
        return Session(self)


class Session:
    def __init__(self, store):
        self.store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def begin(self):
        return self

    async def get(self, model, key, **kwargs):
        return self.store.rows.get((model, key))

    def add(self, item):
        self.store.rows[(type(item), item.key)] = item

    async def execute(self, statement):
        self.store.statements.append(statement)
        if isinstance(statement, Insert):
            params = statement.compile().params
            key = params["event_key"]
            if key in self.store.outbox:
                return Result()
            self.store.outbox[key] = OutboxMessage(**params)
            return Result(one=params["id"])
        keys = statement.compile().params.get("event_key_1")
        if isinstance(keys, str):
            return Result(rows=[item for key, item in self.store.outbox.items() if key == keys])
        if isinstance(keys, (list, tuple)):
            return Result(rows=[item for key, item in self.store.outbox.items() if key in keys])
        return Result(
            rows=[
                value
                for value in self.store.outbox.values()
                if value.dead_at is None and value.delivered_at is None
            ]
        )


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("cache notification must not open HTTP")

    monkeypatch.setattr("httpx.AsyncClient", forbidden)


def bind_context(monkeypatch, value):
    async def read(*args, **kwargs):
        return value

    monkeypatch.setattr(module, "_context", read)


async def test_final_policy_exact_identity_fence_and_claim_injected_chat(monkeypatch):
    bind_context(monkeypatch, context())
    fences = []
    item = row()
    assert (
        await map_delivery_policy(
            Session(Store()),
            item,
            NOW,
            lock=True,
            expected_recipient=DeliveryRecipient(CHAT),
            fences=fences,
        )
        == "AUTHORIZED"
    )
    assert len(fences) == 1 and fences[0].valid_at(NOW)
    text = render_map_outbox(item.event_type, {**item.payload, "telegram_chat_id": CHAT}, NOW)
    assert "08:30" in text and "10:30" in text and "2 год" in text
    assert "alerts.energy" not in text and "Джерело" not in text


@pytest.mark.parametrize(
    "change",
    [
        {"aggregate_type": "power"},
        {"aggregate_id": str(uuid4())},
        {"event_type": "POWER_OUTAGE_UPCOMING"},
    ],
)
async def test_wrong_namespace_cannot_enter_official_or_other_user_path(monkeypatch, change):
    ctx = context()

    async def read(session, identifier, *args, **kwargs):
        return ctx if identifier == IDENTIFIER else None

    monkeypatch.setattr(module, "_context", read)
    item = row()
    for key, value in change.items():
        setattr(item, key, value)
    assert await map_delivery_policy(Session(Store()), item, NOW) == "REJECTED"


@pytest.mark.parametrize(
    "override",
    [
        {"source_kind": "official"},
        {"preference_revision": 99},
        {"credential_revision": 99},
        {"group": "1.2"},
        {"version": True},
        {"telegram_chat_id": CHAT},
        {"unexpected": "x"},
    ],
)
async def test_payload_or_preference_changes_reject_delivery(monkeypatch, override):
    bind_context(monkeypatch, context())
    assert (
        await map_delivery_policy(Session(Store()), row({**reminder_payload(), **override}), NOW)
        == "REJECTED"
    )


async def test_missing_account_disabled_flag_recipient_and_revision(monkeypatch):
    for ctx in (
        None,
        context(sub=replace(context().subscription, enabled=False)),
        context(sub=replace(context().subscription, outage_notifications_enabled=False)),
        context(sub=replace(context().subscription, revision=3)),
    ):
        bind_context(monkeypatch, ctx)
        assert await map_delivery_policy(Session(Store()), row(), NOW) == "REJECTED"
    bind_context(monkeypatch, context())
    assert await map_delivery_policy(Session(Store()), row(), NOW, lock=True) == "REJECTED"
    assert (
        await map_delivery_policy(
            Session(Store()), row(), NOW, lock=True, expected_recipient=DeliveryRecipient(CHAT + 1)
        )
        == "REJECTED"
    )


async def test_changed_cache_and_stale_source_between_claim_and_send(monkeypatch):
    item = row()
    for ctx in (
        context(views=(view(outage_hour=9), view(DAY + timedelta(days=1)))),
        context(views=(replace(view(), source_status=SourceStatus.STALE),)),
        context(checked=NOW - timedelta(seconds=1801)),
    ):
        bind_context(monkeypatch, ctx)
        assert await map_delivery_policy(Session(Store()), item, NOW) == "REJECTED"
    bind_context(monkeypatch, context())
    assert (
        await map_delivery_policy(Session(Store()), item, NOW + timedelta(seconds=60)) == "REJECTED"
    )


def test_clock_fence_rejects_expiry_or_source_ttl_while_lock_waiting():
    fence = MapDeliveryFence(NOW + timedelta(seconds=60), (NOW + timedelta(seconds=5),))
    assert fence.valid_at(NOW + timedelta(seconds=5))
    assert not fence.valid_at(NOW + timedelta(seconds=6))
    assert not fence.valid_at(NOW + timedelta(seconds=60))
    assert not MapDeliveryFence(NOW + timedelta(seconds=60), ()).valid_at(NOW)
    assert not MapDeliveryFence(
        NOW + timedelta(seconds=60), (NOW + timedelta(seconds=60),), NOW
    ).valid_at(NOW - timedelta(microseconds=1))


async def test_restart_repeated_poll_keeps_one_durable_event(monkeypatch):
    bind_context(monkeypatch, context())
    store = Store()
    assert await PostgresPowerMapNotificationRepository(store)._plan_user(IDENTIFIER, NOW) == 1
    assert await PostgresPowerMapNotificationRepository(store)._plan_user(IDENTIFIER, NOW) == 0
    assert len(store.outbox) == 1
    assert all(value.aggregate_type == "power_map" for value in store.outbox.values())
    statements = [str(s.compile(dialect=postgresql.dialect())) for s in store.statements]
    statement = next(s for s in statements if "SKIP LOCKED" in s)
    assert "FOR UPDATE" in statement


async def test_preference_edit_cancels_old_pending_without_resurrecting_boundary(monkeypatch):
    bind_context(monkeypatch, context())
    store = Store()
    repo = PostgresPowerMapNotificationRepository(store)
    assert await repo._plan_user(IDENTIFIER, NOW) == 1
    bind_context(monkeypatch, context(sub=replace(context().subscription, revision=3)))
    assert await repo._plan_user(IDENTIFIER, NOW) == 0
    assert len(store.outbox) == 1
    assert next(iter(store.outbox.values())).dead_at == NOW


async def test_first_graph_silent_baseline_then_complete_changed_tomorrow_once(monkeypatch):
    # No due reminder now, but tomorrow exists. First baseline is silent.
    views = (view(outage_hour=12), view(DAY + timedelta(days=1)))
    bind_context(monkeypatch, context(views=views))
    store = Store()
    assert await PostgresPowerMapNotificationRepository(store)._plan_user(IDENTIFIER, NOW) == 0
    changed = (views[0], view(DAY + timedelta(days=1), outage_hour=10))
    bind_context(monkeypatch, context(views=changed))
    assert await PostgresPowerMapNotificationRepository(store)._plan_user(IDENTIFIER, NOW) == 1
    item = next(iter(store.outbox.values()))
    assert item.event_type == CHANGED
    assert await map_delivery_policy(Session(store), item, NOW) == "AUTHORIZED"
    text = render_map_outbox(CHANGED, item.payload, NOW)
    assert "05.11.2026" in text and "10:30" in text and "12:30" in text
    assert await PostgresPowerMapNotificationRepository(store)._plan_user(IDENTIFIER, NOW) == 0
    assert await map_delivery_policy(Session(store), item, NOW + timedelta(days=1)) == "REJECTED"


async def test_timestamp_only_refresh_no_tomorrow_change_and_bad_cursor_does_not_crash(monkeypatch):
    views = (view(outage_hour=12), view(DAY + timedelta(days=1)))
    bind_context(monkeypatch, context(views=views))
    store = Store()
    repo = PostgresPowerMapNotificationRepository(store)
    assert await repo._plan_user(IDENTIFIER, NOW) == 0
    bind_context(monkeypatch, context(views=tuple(replace(v, last_success_at=NOW) for v in views)))
    assert await repo._plan_user(IDENTIFIER, NOW) == 0
    store.rows[(RuntimeSetting, reminder_state_key(str(IDENTIFIER)))].value = {"version": True}
    assert await repo._plan_user(IDENTIFIER, NOW) == 0


@pytest.mark.parametrize("ids", [(), (True,), (0,), (-1,), (1, 1), [1]])
def test_allowlist_must_be_nonempty_unique_positive_integer_tuple(ids):
    with pytest.raises(ValueError):
        PostgresPowerMapNotificationRepository(Store(), allowed_chat_ids=ids)


async def test_allowlist_applies_after_user_binding(monkeypatch):
    bind_context(monkeypatch, context())
    store = Store()
    assert (
        await PostgresPowerMapNotificationRepository(
            store, allowed_chat_ids=(CHAT + 1,)
        )._plan_user(IDENTIFIER, NOW)
        == 0
    )
    assert not store.outbox


def pref_wire(revision=2):
    return {
        "version": 2,
        "provider": PROVIDER.value,
        "group": "1.1",
        "revision": revision,
        "enabled": True,
        "outage_enabled": True,
        "restoration_enabled": True,
        "outage_lead": 10,
        "restoration_lead": 10,
    }


async def test_permanent_recipient_failure_disables_only_matching_map_revision():
    store = Store()
    store.rows[(User, IDENTIFIER)] = User(
        id=IDENTIFIER, telegram_user_id=CHAT, alerts_enabled=True, lessons_enabled=True
    )
    pref = RuntimeSetting(key=preference_key(str(IDENTIFIER)), value=pref_wire())
    store.rows[(RuntimeSetting, pref.key)] = pref
    await disable_map_for_failed_recipient(Session(store), row(), NOW)
    assert pref.value == {**pref_wire(), "enabled": False, "revision": 3}
    assert store.rows[(User, IDENTIFIER)].alerts_enabled
    assert store.rows[(User, IDENTIFIER)].lessons_enabled
    pref.value = pref_wire(revision=7)
    await disable_map_for_failed_recipient(Session(store), row(), NOW)
    assert pref.value == pref_wire(revision=7)


class MetadataSession(Session):
    def __init__(self, store, credential=(1, "VALID")):
        super().__init__(store)
        self.credential = credential

    async def execute(self, statement):
        self.store.statements.append(statement)
        return SimpleNamespace(one_or_none=lambda: self.credential)


def metadata_store():
    store = Store()
    store.rows[(User, IDENTIFIER)] = User(id=IDENTIFIER, telegram_user_id=CHAT)
    store.rows[(RuntimeSetting, preference_key(str(IDENTIFIER)))] = RuntimeSetting(
        key=preference_key(str(IDENTIFIER)), value=pref_wire()
    )
    area = MapArea("dnipropetrovska-oblast", "UA" + "1" * 17, "Synthetic area")
    rows = tuple(
        {
            "initiator": "dnipropetrovska_oblast_oblenergo",
            "queue": group,
            "updated": NOW.isoformat(),
            "today": [0] * 8 + [3, 1, 2] + [0] * 13,
            "tomorrow": [0] * 24,
            "today_date": DAY.isoformat(),
            "tomorrow_date": (DAY + timedelta(days=1)).isoformat(),
            "today_status": "ScheduleApplies",
            "tomorrow_status": "NoShutdowns",
            "today_checked_at": NOW.isoformat(),
            "tomorrow_checked_at": NOW.isoformat(),
        }
        for group in GROUPS
    )
    data = RegionMap(area, rows, NOW)
    value = {
        **_empty_region(area),
        "revision": 1,
        "payload": data.to_wire(),
        "content_hash": data.semantic_hash,
        "source_status": "FRESH",
        "last_fetch_succeeded": True,
        "last_success_at": NOW.isoformat(),
    }
    store.rows[(RuntimeSetting, REGION_PREFIX + area.slug)] = RuntimeSetting(
        key=REGION_PREFIX + area.slug, value=value
    )
    return store


async def test_actual_context_projection_locks_public_metadata_without_reading_key_ciphertext():
    store = metadata_store()
    ctx = await module._context(MetadataSession(store), IDENTIFIER, NOW, 1800, lock=True)
    assert ctx is not None and ctx.credential_revision == 1
    assert ctx.views[0].source_status is SourceStatus.UNAVAILABLE
    assert ctx.views[1].source_status is SourceStatus.FRESH
    query = store.statements[0]
    assert len(query.selected_columns) == 2 and query._for_update_arg is not None
    assert set(query.compile().params.values()) >= {"revision", "check_status"}
    assert "encrypted" not in str(query)


@pytest.mark.parametrize("credential", [None, (True, "VALID"), (1, "INVALID"), (1, "UNKNOWN")])
async def test_invalid_or_removed_credentials_cannot_authorize_context(credential):
    assert (
        await module._context(
            MetadataSession(metadata_store(), credential), IDENTIFIER, NOW, 1800, lock=True
        )
        is None
    )


def test_state_key_canonical_uuid_and_no_shared_identifier_names():
    assert reminder_state_key(str(IDENTIFIER)) == "power_api_reminder_state:" + str(IDENTIFIER)
    with pytest.raises(ValueError):
        reminder_state_key("12345")
    assert EVENT_TYPES.isdisjoint({"POWER_OUTAGE_UPCOMING", "ALERT_STARTED", "ALL_CLEAR_CONFIRMED"})


async def test_forged_day_hash_or_extra_claim_metadata_rejected_by_renderer(monkeypatch):
    payload = reminder_payload()
    with pytest.raises(ValueError):
        render_map_outbox(OUTAGE, {**payload, "confirmed_region_name": "forged"}, NOW)
    with pytest.raises(ValueError):
        render_map_outbox(OUTAGE, {**payload, "source_kind": "official"}, NOW)
    copied = deepcopy(payload)
    copied["event"]["outage_ends_at"] = (NOW + timedelta(hours=4)).isoformat()
    # It may remain structurally representable, but cannot pass current-cache authorization.
    bind_context(monkeypatch, context())
    assert await map_delivery_policy(Session(Store()), row(copied), NOW) == "REJECTED"


async def test_today_change_also_sends_full_date_qualified_graph(monkeypatch):
    views = (view(outage_hour=12), view(DAY + timedelta(days=1)))
    bind_context(monkeypatch, context(views=views))
    store = Store()
    repo = PostgresPowerMapNotificationRepository(store)
    assert await repo._plan_user(IDENTIFIER, NOW) == 0
    bind_context(monkeypatch, context(views=(view(outage_hour=13), views[1])))
    assert await repo._plan_user(IDENTIFIER, NOW) == 1
    item = next(iter(store.outbox.values()))
    assert item.event_type == CHANGED
    assert await map_delivery_policy(Session(store), item, NOW) == "AUTHORIZED"
    rendered = render_map_outbox(CHANGED, item.payload, NOW)
    assert "04.11.2026" in rendered and "13:30" in rendered and "15:30" in rendered
    assert "сьогодні" in rendered


async def test_a_b_a_b_has_three_distinct_change_generations(monkeypatch):
    today = view(outage_hour=12)
    original = view(DAY + timedelta(days=1))
    alternate = view(DAY + timedelta(days=1), outage_hour=10)
    store = Store()
    repo = PostgresPowerMapNotificationRepository(store)
    bind_context(monkeypatch, context(views=(today, original)))
    assert await repo._plan_user(IDENTIFIER, NOW) == 0
    for tomorrow in (alternate, original, alternate):
        bind_context(monkeypatch, context(views=(today, tomorrow)))
        assert await repo._plan_user(IDENTIFIER, NOW) == 1
        assert await PostgresPowerMapNotificationRepository(store)._plan_user(IDENTIFIER, NOW) == 0
    assert len(store.outbox) == 3
    cursor = store.rows[(RuntimeSetting, reminder_state_key(str(IDENTIFIER)))].value
    assert cursor["generation"] == 3


async def test_preference_revision_reset_cannot_collide_with_old_change_generation(monkeypatch):
    today, original = view(outage_hour=12), view(DAY + timedelta(days=1))
    alternate = view(DAY + timedelta(days=1), outage_hour=10)
    store = Store()
    repo = PostgresPowerMapNotificationRepository(store)
    bind_context(monkeypatch, context(views=(today, original)))
    assert await repo._plan_user(IDENTIFIER, NOW) == 0
    bind_context(monkeypatch, context(views=(today, alternate)))
    assert await repo._plan_user(IDENTIFIER, NOW) == 1
    edited = replace(context().subscription, revision=3, outage_lead_minutes=17)
    bind_context(monkeypatch, context(sub=edited, views=(today, original)))
    assert await repo._plan_user(IDENTIFIER, NOW) == 0
    bind_context(monkeypatch, context(sub=edited, views=(today, alternate)))
    assert await repo._plan_user(IDENTIFIER, NOW) == 1
    assert len(store.outbox) == 2


async def changed_store(monkeypatch):
    today = view(outage_hour=12)
    tomorrow = view(DAY + timedelta(days=1))
    bind_context(monkeypatch, context(views=(today, tomorrow)))
    store = Store()
    repo = PostgresPowerMapNotificationRepository(store)
    assert await repo._plan_user(IDENTIFIER, NOW) == 0
    tomorrow = view(DAY + timedelta(days=1), outage_hour=10)
    bind_context(monkeypatch, context(views=(today, tomorrow)))
    assert await repo._plan_user(IDENTIFIER, NOW) == 1
    return store, repo, today, tomorrow


async def test_stale_queued_change_recovery_creates_new_once_without_reviving_terminal(monkeypatch):
    store, repo, today, tomorrow = await changed_store(monkeypatch)
    original = next(iter(store.outbox.values()))
    bind_context(
        monkeypatch, context(views=(today, replace(tomorrow, source_status=SourceStatus.STALE)))
    )
    assert await repo._plan_user(IDENTIFIER, NOW) == 0
    assert original.dead_at == NOW and original.dispatch_started_at is None
    bind_context(monkeypatch, context(views=(today, tomorrow)))
    assert await PostgresPowerMapNotificationRepository(store)._plan_user(IDENTIFIER, NOW) == 1
    assert await repo._plan_user(IDENTIFIER, NOW) == 0
    assert len(store.outbox) == 2 and original.dead_at == NOW
    assert len({item.event_key for item in store.outbox.values()}) == 2


@pytest.mark.parametrize("outcome", ["delivered_at", "indeterminate_at", "dispatch_started_at"])
async def test_sent_or_uncertain_change_is_never_requeued_after_restart(monkeypatch, outcome):
    store, repo, today, tomorrow = await changed_store(monkeypatch)
    original = next(iter(store.outbox.values()))
    setattr(original, outcome, NOW)
    if outcome != "delivered_at":
        original.dead_at = NOW
    bind_context(monkeypatch, context(views=(today, tomorrow)))
    assert await PostgresPowerMapNotificationRepository(store)._plan_user(IDENTIFIER, NOW) == 0
    assert len(store.outbox) == 1


def midnight_store(kind, *, with_previous=True, stale=False):
    boundary = day_bounds(DAY + timedelta(days=1))[0]
    fetched = (
        boundary - timedelta(seconds=30) if with_previous else boundary + timedelta(seconds=15)
    )
    if stale:
        fetched = boundary - timedelta(seconds=1801)
    local_today = fetched.astimezone(KYIV).date()
    store = Store()
    store.rows[(User, IDENTIFIER)] = User(id=IDENTIFIER, telegram_user_id=CHAT)
    store.rows[(RuntimeSetting, preference_key(str(IDENTIFIER)))] = RuntimeSetting(
        key=preference_key(str(IDENTIFIER)),
        value={**pref_wire(), "outage_lead": 0, "restoration_lead": 0},
    )
    area = MapArea("dnipropetrovska-oblast", "UA" + "1" * 17, "Synthetic area")
    today, tomorrow = [0] * 24, [0] * 24
    if with_previous:
        if kind == OUTAGE:
            tomorrow[0] = 1
        else:
            today[23] = 1
    elif kind == OUTAGE:
        today[0] = 1
    rows = tuple(
        {
            "initiator": "dnipropetrovska_oblast_oblenergo",
            "queue": group,
            "updated": fetched.isoformat(),
            "today": list(today),
            "tomorrow": list(tomorrow),
            "today_date": local_today.isoformat(),
            "tomorrow_date": (local_today + timedelta(days=1)).isoformat(),
            "today_status": "ScheduleApplies" if any(today) else "NoShutdowns",
            "tomorrow_status": "ScheduleApplies" if any(tomorrow) else "NoShutdowns",
            "today_checked_at": fetched.isoformat(),
            "tomorrow_checked_at": fetched.isoformat(),
        }
        for group in GROUPS
    )
    data = RegionMap(area, rows, fetched)
    value = {
        **_empty_region(area),
        "revision": 1,
        "payload": data.to_wire(),
        "content_hash": data.semantic_hash,
        "source_status": "FRESH",
        "last_fetch_succeeded": True,
        "last_success_at": fetched.isoformat(),
    }
    store.rows[(RuntimeSetting, REGION_PREFIX + area.slug)] = RuntimeSetting(
        key=REGION_PREFIX + area.slug, value=value
    )
    return store, boundary + timedelta(seconds=30)


@pytest.mark.parametrize("kind", [OUTAGE, "POWER_MAP_RESTORATION_UPCOMING"])
async def test_actual_context_retains_fresh_previous_day_for_zero_lead_midnight(kind):
    store, clock = midnight_store(kind)
    ctx = await module._context(MetadataSession(store), IDENTIFIER, clock, 1800, lock=True)
    assert ctx is not None
    assert len(ctx.views) == 3
    assert ctx.views[0].schedule.local_date == clock.astimezone(KYIV).date() - timedelta(days=1)
    events = plan_map_reminders(ctx.views, clock, outage_lead_minutes=0, restoration_lead_minutes=0)
    matching = [event for event in events if event.event_type == kind]
    assert len(matching) == 1
    assert matching[0].transition_at == day_bounds(clock.astimezone(KYIV).date())[0]
    payload = module._identity_payload(ctx, matching[0].expires_at)
    payload["event"] = matching[0].to_wire()
    item = row(payload, kind)
    assert (
        await map_delivery_policy(
            MetadataSession(store),
            item,
            clock,
            lock=True,
            expected_recipient=DeliveryRecipient(CHAT),
        )
        == "AUTHORIZED"
    )


@pytest.mark.parametrize("kind", [OUTAGE, "POWER_MAP_RESTORATION_UPCOMING"])
@pytest.mark.parametrize("missing", [True, False])
async def test_actual_context_missing_or_stale_previous_day_never_guesses_midnight(kind, missing):
    store, clock = midnight_store(kind, with_previous=not missing, stale=not missing)
    ctx = await module._context(MetadataSession(store), IDENTIFIER, clock, 1800, lock=True)
    assert ctx is not None
    assert ctx.views[0].source_status in {SourceStatus.UNAVAILABLE, SourceStatus.STALE}
    events = plan_map_reminders(ctx.views, clock, outage_lead_minutes=0, restoration_lead_minutes=0)
    boundary = day_bounds(clock.astimezone(KYIV).date())[0]
    assert not any(event.event_type == kind and event.transition_at == boundary for event in events)


async def test_publishing_unrelated_tomorrow_preserves_queued_today_reminder(monkeypatch):
    today = view()
    tomorrow = view(DAY + timedelta(days=1))
    unavailable = replace(
        tomorrow, schedule=None, source_status=SourceStatus.UNAVAILABLE, last_success_at=None
    )
    bind_context(monkeypatch, context(views=(today, unavailable)))
    store = Store()
    repo = PostgresPowerMapNotificationRepository(store)
    assert await repo._plan_user(IDENTIFIER, NOW) == 1
    queued = next(iter(store.outbox.values()))
    original_payload = deepcopy(queued.payload)
    original_event = module.MapPowerReminder.from_wire(queued.payload["event"])
    assert original_event.next_outage_starts_at is None
    bind_context(monkeypatch, context(views=(today, tomorrow)))
    current_event = next(
        event for event in plan_map_reminders((today, tomorrow), NOW) if event.event_type == OUTAGE
    )
    assert current_event.to_wire() == original_event.to_wire()
    assert await map_delivery_policy(Session(store), queued, NOW) == "AUTHORIZED"
    # First publication also creates the full tomorrow change notice, while the
    # existing today reminder stays live with its original conservative payload.
    assert await repo._plan_user(IDENTIFIER, NOW) == 1
    assert queued.dead_at is None and queued.payload == original_payload
    assert len(store.outbox) == 2
    assert sum(item.event_type == CHANGED for item in store.outbox.values()) == 1
    assert await PostgresPowerMapNotificationRepository(store)._plan_user(IDENTIFIER, NOW) == 0


async def test_relevant_today_cut_change_still_cancels_queued_reminder(monkeypatch):
    bind_context(monkeypatch, context())
    store = Store()
    repo = PostgresPowerMapNotificationRepository(store)
    assert await repo._plan_user(IDENTIFIER, NOW) == 1
    queued = next(iter(store.outbox.values()))
    bind_context(monkeypatch, context(views=(view(outage_hour=9), view(DAY + timedelta(days=1)))))
    assert await map_delivery_policy(Session(store), queued, NOW) == "REJECTED"
    assert await repo._plan_user(IDENTIFIER, NOW) == 1
    assert queued.dead_at == NOW


def test_semantic_compatibility_never_discards_known_next_claims():
    clock = view().schedule.intervals[1].ends_at - timedelta(minutes=10)
    views = (view(fetched=clock), view(DAY + timedelta(days=1), fetched=clock))
    event = next(
        item
        for item in plan_map_reminders(views, clock)
        if item.event_type == "POWER_MAP_RESTORATION_UPCOMING"
    )
    assert event.next_outage_starts_at is not None and event.next_outage_ends_at is not None
    conservative = replace(event, next_outage_starts_at=None, next_outage_ends_at=None)
    assert module._same_claims(conservative, event)
    assert not module._same_claims(event, conservative)
    changed = replace(
        event,
        next_outage_starts_at=event.next_outage_starts_at + timedelta(hours=1),
        next_outage_ends_at=event.next_outage_ends_at + timedelta(hours=1),
    )
    assert not module._same_claims(event, changed)


async def test_stored_conservative_restoration_survives_new_next_outage_information(monkeypatch):
    clock = view().schedule.intervals[1].ends_at - timedelta(minutes=10)
    today = view(fetched=clock)
    tomorrow = view(DAY + timedelta(days=1), fetched=clock)
    unavailable = replace(
        tomorrow, schedule=None, source_status=SourceStatus.UNAVAILABLE, last_success_at=None
    )
    old_views = (today, unavailable)
    event = next(
        item
        for item in plan_map_reminders(old_views, clock)
        if item.event_type == "POWER_MAP_RESTORATION_UPCOMING"
    )
    ctx = context(views=old_views, checked=clock)
    data = SimpleNamespace(fetched_at=clock, record_checked_at=lambda *args, **kwargs: clock)
    ctx = replace(ctx, map_view=SimpleNamespace(data=data))
    payload = module._identity_payload(ctx, event.expires_at)
    payload["event"] = event.to_wire()
    queued = row(payload, event.event_type)
    original_payload = deepcopy(payload)
    new_views = (today, tomorrow)
    new_event = next(
        item for item in plan_map_reminders(new_views, clock) if item.event_type == event.event_type
    )
    assert event.next_outage_starts_at is None and new_event.next_outage_starts_at is not None
    bind_context(monkeypatch, replace(ctx, views=new_views))
    assert await map_delivery_policy(Session(Store()), queued, clock) == "AUTHORIZED"
    assert queued.payload == original_payload
    assert "Час наступного відключення поки не підтверджено" in render_map_outbox(
        event.event_type, queued.payload, clock
    )


def emergency_context(now, publication, *, checked=None, sub=None):
    lower, upper = day_bounds(DAY)
    day = PowerDaySchedule(
        PROVIDER,
        "1.1",
        DAY,
        ScheduleKind.CONFIRMED_DAY,
        (PowerInterval("1.1", lower, upper, PowerState.UNKNOWN),),
        publication_status=publication,
    )
    selected = replace(view(), schedule=day)
    ctx = context(sub=sub, checked=checked or now)
    ctx.map_view.data.fetched_at = now
    return replace(ctx, emergency_view=selected)


@pytest.mark.asyncio
async def test_emergency_outbox_requires_confirmed_fresh_transition_and_dedupes(monkeypatch):
    store = Store()
    planner = PostgresPowerMapNotificationRepository(store)
    start = emergency_context(NOW, PowerPublicationStatus.EMERGENCY)
    bind_context(monkeypatch, start)
    await planner._plan_user(IDENTIFIER, NOW)
    await planner._plan_user(IDENTIFIER, NOW + timedelta(seconds=1))
    (started,) = [
        item for item in store.outbox.values() if item.event_type in module.EMERGENCY_EVENTS
    ]
    assert started.event_type == module.EMERGENCY_STARTED
    assert "Запроваджено" in render_map_outbox(started.event_type, started.payload, NOW)
    assert await map_delivery_policy(Session(store), started, NOW) == "AUTHORIZED"

    later = NOW + timedelta(minutes=5)
    unavailable = emergency_context(later, PowerPublicationStatus.UNKNOWN)
    bind_context(monkeypatch, unavailable)
    assert await map_delivery_policy(Session(store), started, later) == "REJECTED"
    await planner._plan_user(IDENTIFIER, later)
    assert not any(item.event_type == module.EMERGENCY_ENDED for item in store.outbox.values())

    planned = replace(context(checked=later), emergency_view=view())
    planned.map_view.data.fetched_at = later
    bind_context(monkeypatch, planned)
    await planner._plan_user(IDENTIFIER, later)
    await planner._plan_user(IDENTIFIER, later + timedelta(seconds=1))
    (ended,) = [item for item in store.outbox.values() if item.event_type == module.EMERGENCY_ENDED]
    assert "Фактичне відновлення світла не підтверджено" in render_map_outbox(
        ended.event_type, ended.payload, later
    )
    assert await map_delivery_policy(Session(store), ended, later) == "AUTHORIZED"
    expired = later + timedelta(minutes=10)
    assert await map_delivery_policy(Session(store), ended, expired) == "REJECTED"


@pytest.mark.asyncio
async def test_emergency_never_uses_stale_snapshot_or_disabled_notice(monkeypatch):
    store = Store()
    planner = PostgresPowerMapNotificationRepository(store)
    stale = emergency_context(
        NOW, PowerPublicationStatus.EMERGENCY, checked=NOW - timedelta(minutes=31)
    )
    bind_context(monkeypatch, stale)
    await planner._plan_user(IDENTIFIER, NOW)
    sub = replace(stale.subscription, outage_notifications_enabled=False)
    bind_context(monkeypatch, emergency_context(NOW, PowerPublicationStatus.EMERGENCY, sub=sub))
    await planner._plan_user(IDENTIFIER, NOW)
    assert not any(item.event_type in module.EMERGENCY_EVENTS for item in store.outbox.values())


@pytest.mark.asyncio
async def test_emergency_uses_exact_group_when_only_subscribed_group_was_cached():
    store = metadata_store()
    session = MetadataSession(store)
    cache = store.rows[(RuntimeSetting, REGION_PREFIX + "dnipropetrovska-oblast")]
    previous = RegionMap.from_wire(cache.value["payload"])
    row_wire = next(item for item in previous.to_wire()["rows"] if item["queue"] == "1.1")
    row_wire.update(
        today=[],
        today_status="EmergencyShutdowns",
        tomorrow=[],
        tomorrow_status="WaitingForSchedule",
        tomorrow_checked_at=None,
    )
    data = RegionMap(previous.area, (row_wire,), NOW)
    cache.value = {
        **cache.value,
        "payload": data.to_wire(),
        "content_hash": data.semantic_hash,
        "source_status": data.status.value,
        "error_code": data.error_code,
    }

    ctx = await module._context(session, IDENTIFIER, NOW, 1800, lock=True)
    assert ctx is not None and ctx.views[1].schedule is None
    event = await module._plan_emergency(session, ctx, NOW)
    assert event is not None and event[0] == module.EMERGENCY_STARTED
    fences = []
    assert (
        await map_delivery_policy(
            session,
            row(event[2], event[0]),
            NOW,
            lock=True,
            expected_recipient=DeliveryRecipient(CHAT),
            fences=fences,
        )
        == "AUTHORIZED"
    )
    assert len(fences) == 1 and fences[0].valid_at(NOW)


@pytest.mark.asyncio
async def test_editing_lead_time_does_not_repeat_emergency_start(monkeypatch):
    store = Store()
    planner = PostgresPowerMapNotificationRepository(store)
    active = emergency_context(
        NOW, PowerPublicationStatus.EMERGENCY, checked=NOW - timedelta(minutes=11)
    )
    bind_context(monkeypatch, active)
    await planner._plan_user(IDENTIFIER, NOW)
    later = NOW + timedelta(minutes=5)
    sub = replace(active.subscription, revision=3, outage_lead_minutes=15)
    bind_context(monkeypatch, emergency_context(later, PowerPublicationStatus.EMERGENCY, sub=sub))
    await planner._plan_user(IDENTIFIER, later)
    (started,) = [
        item for item in store.outbox.values() if item.event_type == module.EMERGENCY_STARTED
    ]
    assert await map_delivery_policy(Session(store), started, later) == "REJECTED"
