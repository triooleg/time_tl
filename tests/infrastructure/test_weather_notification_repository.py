"""Offline ORM-session fixtures exercising the real weather final policy, not live PG."""

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects.postgresql.dml import Insert

from time_tl.application.sender_worker import DeliveryRecipient
from time_tl.application.weather_notifications import (
    WeatherNotificationPlanner,
    WeatherNotificationPreferences,
)
from time_tl.domain.weather import (
    ForecastHour,
    ForecastSnapshot,
    ForecastStatus,
    WeatherChangeDetector,
)
from time_tl.infrastructure.db import weather_notifications as db
from time_tl.infrastructure.db.models import (
    ActiveTerritoryCatalog,
    OutboxMessage,
    RuntimeSetting,
    User,
    UserWeatherPreference,
    WeatherChangeEvent,
    WeatherLocation,
    WeatherNotificationDelivery,
)
from time_tl.infrastructure.db.weather_configuration import SETTING, WeatherConfiguration

NOW = datetime(2026, 11, 5, 10, tzinfo=UTC)
USER = UUID(int=200)
POINT = UUID(int=100)
ACTOR = 123


class Result:
    def __init__(self, values=()):
        self.values = list(values)

    def scalar_one_or_none(self):
        assert len(self.values) <= 1
        return next(iter(self.values), None)

    def scalars(self):
        return self

    def all(self):
        return self.values


class Context:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class PolicyStore:
    def __init__(self):
        self.snapshot = ForecastSnapshot(
            "MET Norway",
            str(POINT),
            NOW,
            NOW,
            NOW + timedelta(hours=8),
            (ForecastHour(NOW + timedelta(hours=2), wind_gust_ms=20),),
            ForecastStatus.FRESH,
            "source",
        )
        self.events = WeatherChangeDetector().detect(None, self.snapshot, NOW)
        prefs = WeatherNotificationPreferences(
            weather_enabled=True, forecast_requested=True, revision=1
        )
        plan = WeatherNotificationPlanner().plan(
            user_id=str(USER),
            region_id="h1",
            locality="Synthetic",
            snapshot=self.snapshot,
            events=self.events,
            preferences=prefs,
            delivered_event_keys=frozenset(),
            known_notification_keys=frozenset(),
            now=NOW,
            configuration_revision=1,
        )[0]
        self.row = OutboxMessage(
            id=uuid4(),
            aggregate_type="weather",
            aggregate_id=str(USER),
            event_type=plan.kind,
            event_key=plan.key,
            payload=deepcopy(plan.payload),
            available_at=NOW,
            claimed_at=NOW,
            attempts=0,
        )
        self.user = User(
            id=USER, telegram_user_id=ACTOR, confirmed_region_id="h1", region_name="Synthetic"
        )
        self.pref = UserWeatherPreference(user_id=USER, **asdict(prefs))
        self.location = WeatherLocation(
            id=POINT,
            region_id="h1",
            katottg_code="UA00000000000000003",
            locality_name="Synthetic",
            latitude=48.5,
            longitude=35,
            enabled=True,
            coverage_type="BASE",
            provenance_url="https://example.org/approved",
            provenance_sha256="a" * 64,
            provenance_version="v1",
            approved_at=NOW - timedelta(days=1),
        )
        self.delivery = WeatherNotificationDelivery(
            user_id=USER,
            notification_key=plan.key.removeprefix("weather:"),
            outbox_id=self.row.id,
            event_keys=list(plan.event_keys),
            created_at=NOW,
        )
        self.rows = {
            OutboxMessage: [],
            RuntimeSetting: [
                RuntimeSetting(
                    key=SETTING,
                    updated_by="fixture",
                    value=WeatherConfiguration(
                        enabled=True, terms_approved=True, revision=1
                    ).model_dump(),
                )
            ],
            User: [self.user],
            UserWeatherPreference: [self.pref],
            WeatherLocation: [self.location],
            ActiveTerritoryCatalog: [
                ActiveTerritoryCatalog(provider="alerts.in.ua", catalog_version="v1")
            ],
            WeatherChangeEvent: [self.orm_event(e) for e in self.events],
            WeatherNotificationDelivery: [self.delivery],
        }
        self.queries = []

    def orm_event(self, event):
        return WeatherChangeEvent(
            id=uuid4(),
            location_id=POINT,
            event_key=event.event_key,
            event_type=event.event_type,
            kind=event.kind,
            expected_start=event.expected_start,
            expected_end=event.expected_end,
            previous_value=event.previous_value,
            current_value=event.current_value,
            detected_at=event.detected_at,
            provider=event.provider,
            source_revision=event.source_revision,
            status="CANCELLED" if event.kind == "CANCELLATION" else "ACTIVE",
        )

    def __call__(self):
        return Session(self)

    @property
    def recipient(self):
        return DeliveryRecipient(ACTOR, "h1", "Synthetic")


class Session:
    def __init__(self, store):
        self.store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def begin(self):
        return Context()

    def add(self, row):
        self.store.rows.setdefault(type(row), []).append(row)

    async def execute(self, statement):
        self.store.queries.append(statement)
        if isinstance(statement, Insert):
            values = statement.compile().params
            assert statement.table.name == "outbox_messages"
            if any(row.event_key == values["event_key"] for row in self.store.rows[OutboxMessage]):
                return Result()
            self.store.rows[OutboxMessage].append(OutboxMessage(**values))
            return Result((values["id"],))
        model = statement.column_descriptions[0]["entity"]
        rows = list(self.store.rows.get(model, ()))
        params = statement.compile().params
        for key, value in params.items():
            attr = key.rsplit("_", 1)[0]
            if (
                rows
                and hasattr(rows[0], attr)
                and attr
                in {
                    "id",
                    "user_id",
                    "provider",
                    "catalog_version",
                    "region_id",
                    "coverage_type",
                    "key",
                }
                and not isinstance(value, (list, tuple))
            ):
                rows = [row for row in rows if getattr(row, attr) == value]
        if model is WeatherNotificationDelivery:
            expr = statement.column_descriptions[0]["expr"]
            if getattr(expr, "key", None) == "event_keys":
                rows = [row.event_keys for row in rows if row.delivered_at is not None]
        if model is WeatherChangeEvent:
            rows = sorted(rows, key=lambda row: (row.detected_at, str(row.id)), reverse=True)
            latest = {}
            for row in rows:
                latest.setdefault(row.event_key[:32], row)
            rows = list(latest.values())
        return Result(rows)

    async def get(self, model, key):
        if model is WeatherNotificationDelivery:
            return next(
                (
                    row
                    for row in self.store.rows[model]
                    if (row.user_id, row.notification_key) == key
                ),
                None,
            )
        if model is User:
            return self.store.user if key == USER else None
        return None


@pytest.fixture
def store(monkeypatch):
    value = PolicyStore()

    async def previous(session, *args):
        return value.snapshot

    monkeypatch.setattr(db, "_bound_previous", previous)
    return value


async def test_actual_policy_accepts_current_tuple_and_captures_final_fence(store):
    fences = []
    assert await db.weather_delivery_policy(
        Session(store), store.row, NOW, lock=True, expected_recipient=store.recipient, fences=fences
    )
    assert len(fences) == 1 and fences[0].valid_at(NOW)
    assert not fences[0].valid_at(datetime.fromisoformat(store.row.payload["expires_at"]))


@pytest.mark.parametrize(
    "case",
    [
        "config_off",
        "terms_off",
        "config_revision",
        "master_off",
        "pref_revision",
        "territory_switch",
        "location_disabled",
        "point_unapproved",
        "user_deleted",
        "prefs_deleted",
        "missing_snapshot",
        "stale",
        "expired",
        "different_point",
        "different_label",
        "delivery_missing",
        "already_delivered",
        "outbox_mismatch",
        "namespace",
        "unknown_kind",
    ],
)
async def test_actual_policy_rejects_revoked_or_stale_tuple(store, case):
    config = store.rows[RuntimeSetting][0].value
    if case == "config_off":
        config["enabled"] = False
    elif case == "terms_off":
        config["terms_approved"] = False
    elif case == "config_revision":
        config["revision"] = 2
    elif case == "master_off":
        store.pref.weather_enabled = False
    elif case == "pref_revision":
        store.pref.revision = 2
    elif case == "territory_switch":
        store.user.confirmed_region_id = "h2"
    elif case == "location_disabled":
        store.location.enabled = False
    elif case == "point_unapproved":
        store.location.approved_at = None
    elif case == "user_deleted":
        store.rows[User] = []
    elif case == "prefs_deleted":
        store.rows[UserWeatherPreference] = []
    elif case == "missing_snapshot":
        store.snapshot = None
    elif case == "stale":
        store.snapshot = replace(store.snapshot, status=ForecastStatus.STALE)
    elif case == "expired":
        store.row.payload["expires_at"] = NOW.isoformat()
    elif case == "different_point":
        store.row.payload["point_id"] = str(uuid4())
    elif case == "different_label":
        store.row.payload["locality"] = "Other"
    elif case == "delivery_missing":
        store.rows[WeatherNotificationDelivery] = []
    elif case == "already_delivered":
        store.delivery.delivered_at = NOW
    elif case == "outbox_mismatch":
        store.delivery.outbox_id = uuid4()
    elif case == "namespace":
        store.row.aggregate_type = "power_map"
    else:
        store.row.event_type = "WEATHER_OFFICIAL_CLEAR"
    assert not await db.weather_delivery_policy(
        Session(store), store.row, NOW, expected_recipient=store.recipient
    )


@pytest.mark.parametrize(
    "recipient",
    [
        DeliveryRecipient(999, "h1", "Synthetic"),
        DeliveryRecipient(ACTOR, "h2", "Synthetic"),
        DeliveryRecipient(ACTOR, "h1", "Other"),
    ],
)
async def test_recipient_is_revalidated_under_policy_locks(store, recipient):
    assert not await db.weather_delivery_policy(
        Session(store), store.row, NOW, expected_recipient=recipient, lock=True
    )


async def test_latest_event_revision_supersedes_claimed_event(store):
    changed = replace(
        store.snapshot,
        hours=(ForecastHour(NOW + timedelta(hours=2), wind_gust_ms=25),),
        provider_updated_at=NOW + timedelta(minutes=1),
    )
    correction = WeatherChangeDetector().detect(
        store.snapshot, changed, NOW + timedelta(minutes=1)
    )[0]
    store.rows[WeatherChangeEvent].append(store.orm_event(correction))
    store.snapshot = changed
    assert not await db.weather_delivery_policy(
        Session(store), store.row, NOW + timedelta(minutes=1)
    )


async def test_unrelated_snapshot_hash_change_preserves_relevant_event(store):
    store.snapshot = replace(
        store.snapshot,
        hours=store.snapshot.hours + (ForecastHour(NOW + timedelta(hours=8), temperature_c=7),),
    )
    assert await db.weather_delivery_policy(Session(store), store.row, NOW)


async def test_subthreshold_drift_preserves_pending_claim(store):
    store.snapshot = replace(
        store.snapshot, hours=(ForecastHour(NOW + timedelta(hours=2), wind_gust_ms=20.2),)
    )
    assert await db.weather_delivery_policy(Session(store), store.row, NOW)


async def test_claimed_fact_tampering_rejected_even_with_valid_key(store):
    store.row.payload["events"][0]["current_value"] = 99
    assert not await db.weather_delivery_policy(Session(store), store.row, NOW)


async def test_real_policy_correction_requires_prior_delivery_not_merely_pending(store):
    old = store.snapshot
    newer = replace(
        old,
        hours=(ForecastHour(NOW + timedelta(hours=2), wind_gust_ms=25),),
        provider_updated_at=NOW + timedelta(minutes=1),
    )
    corrections = WeatherChangeDetector().detect(old, newer, NOW + timedelta(minutes=1))
    event = corrections[0]
    store.snapshot = newer
    store.rows[WeatherChangeEvent] = [store.orm_event(event)]
    claim = store.row.payload["events"][0]
    claim.update(
        event_key=event.event_key,
        kind=event.kind,
        initial=False,
        previous_value=event.previous_value,
        current_value=event.current_value,
    )
    store.row.payload["event_keys"] = [event.event_key]
    store.delivery.event_keys = [event.event_key]
    assert not await db.weather_delivery_policy(
        Session(store), store.row, NOW + timedelta(minutes=1)
    )
    prior = WeatherNotificationDelivery(
        user_id=USER,
        notification_key="a" * 64,
        outbox_id=uuid4(),
        event_keys=[store.events[0].event_key],
        created_at=NOW,
        delivered_at=NOW,
    )
    store.rows[WeatherNotificationDelivery].append(prior)
    assert await db.weather_delivery_policy(Session(store), store.row, NOW + timedelta(minutes=1))
    store.pref.weather_changes_enabled = False
    assert not await db.weather_delivery_policy(
        Session(store), store.row, NOW + timedelta(minutes=1)
    )


async def test_real_policy_corrected_initial_is_allowed_without_previous_delivery(store):
    old = store.snapshot
    newer = replace(
        old,
        hours=(ForecastHour(NOW + timedelta(hours=2), wind_gust_ms=25),),
        provider_updated_at=NOW + timedelta(minutes=1),
    )
    event = WeatherChangeDetector().detect(old, newer, NOW + timedelta(minutes=1))[0]
    store.snapshot = newer
    store.rows[WeatherChangeEvent] = [store.orm_event(event)]
    claim = store.row.payload["events"][0]
    claim.update(
        event_key=event.event_key,
        kind=event.kind,
        initial=True,
        previous_value=event.previous_value,
        current_value=event.current_value,
    )
    store.row.payload["event_keys"] = [event.event_key]
    store.delivery.event_keys = [event.event_key]
    store.pref.weather_changes_enabled = False
    assert await db.weather_delivery_policy(Session(store), store.row, NOW + timedelta(minutes=1))


async def test_actual_db_planner_creates_one_transactional_outbox_and_delivery_then_dedupes(store):
    store.rows[WeatherNotificationDelivery] = []
    planner = db.PostgresWeatherNotificationPlanner(store, enabled=True)
    assert await planner.plan_user(USER, NOW) == 1
    assert len(store.rows[OutboxMessage]) == len(store.rows[WeatherNotificationDelivery]) == 1
    row = store.rows[OutboxMessage][0]
    delivery = store.rows[WeatherNotificationDelivery][0]
    assert row.aggregate_type == "weather" and row.event_type == "WEATHER_EVENTS"
    assert delivery.outbox_id == row.id and delivery.user_id == USER
    assert not {"telegram_chat_id", "latitude", "longitude"} & set(row.payload)
    assert await planner.plan_user(USER, NOW) == 0
    assert len(store.rows[OutboxMessage]) == 1


async def test_disabled_db_planner_never_opens_session():
    def forbidden():
        raise AssertionError("disabled planner must not open DB")

    assert await db.PostgresWeatherNotificationPlanner(forbidden).plan_user(USER, NOW) == 0


async def test_safe_terminal_recovery_is_new_immutable_keychain_not_payload_rewrite(store):
    import hashlib

    store.rows[WeatherNotificationDelivery] = []
    planner = db.PostgresWeatherNotificationPlanner(store, enabled=True)
    assert await planner.plan_user(USER, NOW) == 1
    old = store.rows[OutboxMessage][0]
    original = deepcopy(old.payload)
    old.dead_at, old.claimed_at = NOW, None
    old.dispatch_started_at = old.indeterminate_at = None
    old.attempts, old.last_error_code = 0, "final_weather_expired_or_stale"
    assert await planner.plan_user(USER, NOW) == 1
    new = store.rows[OutboxMessage][1]
    expected = (
        "weather:" + hashlib.sha256((old.event_key + ":after:" + str(old.id)).encode()).hexdigest()
    )
    assert new.event_key == expected and new.id != old.id
    assert old.payload == original
    assert (
        await db.PostgresWeatherNotificationPlanner(store, enabled=True).plan_user(USER, NOW) == 0
    )
    assert len(store.rows[OutboxMessage]) == 2


@pytest.mark.parametrize(
    "unsafe", ["attempted", "dispatch", "indeterminate", "delivered", "claimed", "other_error"]
)
async def test_ambiguous_or_attempted_terminal_rows_never_recover(store, unsafe):
    store.rows[WeatherNotificationDelivery] = []
    planner = db.PostgresWeatherNotificationPlanner(store, enabled=True)
    assert await planner.plan_user(USER, NOW) == 1
    row = store.rows[OutboxMessage][0]
    row.dead_at, row.claimed_at = NOW, None
    row.dispatch_started_at = row.indeterminate_at = None
    row.attempts, row.last_error_code = 0, "delivery_policy_changed"
    if unsafe == "attempted":
        row.attempts = 1
    elif unsafe == "dispatch":
        row.dispatch_started_at = NOW
    elif unsafe == "indeterminate":
        row.indeterminate_at = NOW
    elif unsafe == "delivered":
        row.delivered_at = NOW
    elif unsafe == "claimed":
        row.claimed_at = NOW
    else:
        row.last_error_code = "telegram_forbidden"
    assert await planner.plan_user(USER, NOW) == 0
    assert len(store.rows[OutboxMessage]) == 1


def daily_claim(store):
    from datetime import time

    store.pref.daily_forecast_enabled = True
    store.pref.daily_forecast_time = time(12)  # NOW is 12:00 Kyiv.
    store.snapshot = replace(
        store.snapshot,
        hours=(
            ForecastHour(NOW + timedelta(days=1), temperature_c=4),
            ForecastHour(NOW + timedelta(days=1, hours=1), temperature_c=7),
        ),
    )
    plan = WeatherNotificationPlanner().plan(
        user_id=str(USER),
        region_id="h1",
        locality="Synthetic",
        snapshot=store.snapshot,
        events=(),
        preferences=db.preferences(store.pref),
        delivered_event_keys=frozenset(),
        known_notification_keys=frozenset(),
        now=NOW,
        configuration_revision=1,
    )[0]
    store.row.event_type, store.row.event_key, store.row.payload = (
        plan.kind,
        plan.key,
        deepcopy(plan.payload),
    )
    store.delivery.notification_key = plan.key.removeprefix("weather:")
    store.delivery.event_keys = []


async def test_daily_final_policy_accepts_unrelated_future_day_update(store):
    daily_claim(store)
    store.snapshot = replace(
        store.snapshot,
        hours=store.snapshot.hours + (ForecastHour(NOW + timedelta(days=2), temperature_c=25),),
    )
    assert store.snapshot.forecast_hash != store.row.payload["snapshot_hash"]
    assert await db.weather_delivery_policy(Session(store), store.row, NOW)


async def test_daily_final_policy_rejects_changed_selected_date_summary(store):
    daily_claim(store)
    store.snapshot = replace(
        store.snapshot,
        hours=(
            ForecastHour(NOW + timedelta(days=1), temperature_c=1),
            ForecastHour(NOW + timedelta(days=1, hours=1), temperature_c=7),
        ),
    )
    assert not await db.weather_delivery_policy(Session(store), store.row, NOW)


async def test_delivered_daily_date_never_repeats_after_config_epoch_change(store):
    daily_claim(store)
    store.rows[OutboxMessage] = [store.row]
    store.delivery.delivered_at = store.row.delivered_at = NOW
    store.pref.revision = 2
    store.rows[RuntimeSetting][0].value["revision"] = 2
    assert (
        await db.PostgresWeatherNotificationPlanner(store, enabled=True).plan_user(USER, NOW) == 0
    )
