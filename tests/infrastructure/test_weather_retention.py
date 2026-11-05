"""Offline retention boundaries; no real database or external requests."""

from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from tests.infrastructure.test_weather_storage import NOW, POINT, Result, Session, Store
from time_tl.infrastructure.db import weather_retention as retention
from time_tl.infrastructure.db.models import (
    ActiveTerritoryCatalog,
    RuntimeSetting,
    User,
    UserWeatherPreference,
    WeatherChangeEvent,
    WeatherForecastSnapshot,
    WeatherLocation,
)
from time_tl.infrastructure.db.weather_configuration import WeatherConfiguration

CONFIG = WeatherConfiguration()


def snapshot(*, days=20, expires_days=19):
    return SimpleNamespace(
        id=uuid4(),
        weather_location_id=POINT,
        fetched_at=NOW - timedelta(days=days),
        expires_at=NOW - timedelta(days=expires_days),
    )


@pytest.mark.parametrize(
    "latest,unused_days,expected",
    [
        (True, None, False),
        (False, None, True),
        (True, 0, False),
        (True, 6, False),
        (True, 7, True),
        (False, 7, True),
        (True, 100, True),
    ],
)
def test_latest_retained_until_observed_loss_ages_seven_days(latest, unused_days, expected):
    assert (
        retention.snapshot_expired_for_retention(
            now=NOW,
            fetched_at=NOW - timedelta(days=100),
            expires_at=NOW - timedelta(days=99),
            latest=latest,
            unused_at=None if unused_days is None else NOW - timedelta(days=unused_days),
            config=CONFIG,
        )
        is expected
    )


def test_fresh_or_recent_history_never_deleted():
    assert not retention.snapshot_expired_for_retention(
        now=NOW,
        fetched_at=NOW - timedelta(days=13),
        expires_at=NOW - timedelta(days=12),
        latest=False,
        unused_at=None,
        config=CONFIG,
    )
    assert not retention.snapshot_expired_for_retention(
        now=NOW,
        fetched_at=NOW - timedelta(days=100),
        expires_at=NOW + timedelta(hours=1),
        latest=False,
        unused_at=NOW - timedelta(days=100),
        config=CONFIG,
    )


@pytest.mark.parametrize(
    "value",
    [
        {},
        None,
        {"version": True, "since": NOW.isoformat()},
        {"version": 1, "since": NOW.replace(tzinfo=None).isoformat()},
        {"version": 1, "since": NOW.isoformat(), "extra": True},
    ],
)
def test_invalid_loss_marker_fails_closed(value):
    with pytest.raises((ValueError, TypeError)):
        retention.unused_since(value)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        None,
        {"point_id": str(POINT), "event_keys": ["token"]},
        {"point_id": "not-a-point", "event_keys": []},
        {"point_id": str(POINT), "event_keys": ("a" * 64,)},
    ],
)
def test_invalid_pending_reference_holds_all_deletions(payload):
    assert retention.pending_references([payload]).uncertain


def test_pending_references_exact_uuid_event_keys_only():
    result = retention.pending_references([{"point_id": str(POINT), "event_keys": ["a" * 64]}])
    assert result.points == {POINT} and result.event_keys == {"a" * 64} and not result.uncertain


class RetentionStore(Store):
    def __init__(self):
        super().__init__()
        self.now = NOW
        self.active = set()
        self.pending = retention.PendingWeatherReferences()
        self.deleted = []
        self.last_seen = []

    def __call__(self):
        return RetentionSession(self)


class RetentionSession(Session):
    async def get(self, model, key, **_kwargs):
        return await super().get(model, key)

    async def delete(self, row):
        self.store.deleted.append(row)
        # SimpleNamespace snapshot fixtures use the forecast table explicitly.
        model = WeatherForecastSnapshot if hasattr(row, "weather_location_id") else type(row)
        self.store.rows[model].remove(row)

    async def scalar(self, statement):
        sql = str(statement)
        if "EXISTS (" in sql:
            table = (
                WeatherForecastSnapshot
                if "weather_forecast_snapshots" in sql
                else WeatherChangeEvent
            )
            return bool(self.store.rows[table])
        entity = statement.column_descriptions[0]["entity"]
        if entity is WeatherForecastSnapshot:
            values = sorted(self.store.rows[entity], key=lambda r: r.fetched_at, reverse=True)
            return values[0].id if values else None
        return await super().scalar(statement)

    async def execute(self, statement):
        self.store.queries.append(str(statement.compile(dialect=postgresql.dialect())))
        entity = statement.column_descriptions[0]["entity"]
        if entity in {User, UserWeatherPreference, ActiveTerritoryCatalog}:
            return Result()
        if entity is WeatherLocation:
            return Result([POINT])
        if entity is WeatherForecastSnapshot:
            params = statement.compile().params
            rows = list(self.store.rows[entity])
            if "expires_at_1" in params:
                rows = [r for r in rows if r.expires_at <= params["expires_at_1"]]
            if "fetched_at_1" in params:
                rows = [r for r in rows if r.fetched_at <= params["fetched_at_1"]]
            if "id_1" in params:
                rows = [r for r in rows if r.id != params["id_1"]]
            rows.sort(key=lambda r: r.fetched_at, reverse=True)
            if statement._limit_clause is not None:
                rows = rows[: statement._limit_clause.value]
            return Result(rows)
        if entity is WeatherChangeEvent:
            params = statement.compile().params
            rows = [
                r
                for r in self.store.rows[entity]
                if r.expected_end <= params["expected_end_1"]
                and r.detected_at <= params["detected_at_1"]
                and r.event_key not in self.store.pending.event_keys
            ]
            return Result(rows[: statement._limit_clause.value])
        raise AssertionError("unexpected retention SQL")


@pytest.fixture
def store(monkeypatch):
    store = RetentionStore()

    async def coverage(session, now):
        assert session.store is store and now == store.now
        return SimpleNamespace(weather_location_ids=frozenset(store.active))

    async def pending(session):
        return session.store.pending

    monkeypatch.setattr(retention, "read_coverage_in_session", coverage)
    monkeypatch.setattr(retention, "_pending", pending)
    return store


async def test_old_fetch_does_not_start_with_already_expired_unused_window(store):
    original = snapshot(days=100, expires_days=99)
    store.rows[WeatherForecastSnapshot] = [original]
    result = await retention.PostgresWeatherRetention(store)._point(POINT, NOW, CONFIG, 200)
    assert result["markers_started"] == 1 and result["snapshots_deleted"] == 0
    marker = store.setting(retention.UNUSED_PREFIX + str(POINT))
    assert retention.unused_since(marker.value) == NOW
    assert store.rows[WeatherForecastSnapshot] == [original]


async def test_restart_seven_days_after_observed_loss_purges_cache_and_private_point_state(store):
    store.rows[WeatherForecastSnapshot] = [snapshot(days=100, expires_days=99)]
    cleaner = retention.PostgresWeatherRetention(store)
    await cleaner._point(POINT, NOW, CONFIG, 200)
    for prefix in retention.POINT_PREFIXES:
        store.rows[RuntimeSetting].append(RuntimeSetting(key=prefix + str(POINT), value={}))
    store.now = NOW + timedelta(days=7)
    restarted = retention.PostgresWeatherRetention(store)
    result = await restarted._point(POINT, store.now, CONFIG, 200)
    assert result["snapshots_deleted"] == 1 and result["metadata_deleted"] == 4
    assert not store.rows[WeatherForecastSnapshot]
    assert len(store.rows[WeatherLocation]) == 1
    assert store.setting(retention.UNUSED_PREFIX + str(POINT)) is None


async def test_reactivation_resets_loss_clock_and_preserves_latest(store):
    store.rows[WeatherForecastSnapshot] = [snapshot()]
    cleaner = retention.PostgresWeatherRetention(store)
    await cleaner._point(POINT, NOW, CONFIG, 200)
    store.active.add(str(POINT))
    store.now = NOW + timedelta(days=8)
    result = await cleaner._point(POINT, store.now, CONFIG, 200)
    assert result["markers_cleared"] == 1 and result["snapshots_deleted"] == 0
    store.active.clear()
    store.now += timedelta(days=1)
    await cleaner._point(POINT, store.now, CONFIG, 200)
    assert (
        retention.unused_since(store.setting(retention.UNUSED_PREFIX + str(POINT)).value)
        == store.now
    )


@pytest.mark.parametrize("uncertain", [False, True])
async def test_pending_or_uncertain_queue_protects_entire_point(store, uncertain):
    store.rows[WeatherForecastSnapshot] = [snapshot(days=100, expires_days=99)]
    store.rows[RuntimeSetting].append(
        RuntimeSetting(
            key=retention.UNUSED_PREFIX + str(POINT),
            value={"version": 1, "since": (NOW - timedelta(days=100)).isoformat()},
        )
    )
    store.pending = retention.PendingWeatherReferences(
        points=frozenset({POINT}), uncertain=uncertain
    )
    result = await retention.PostgresWeatherRetention(store)._point(POINT, NOW, CONFIG, 200)
    assert result["pending_protected"] == 1 and not store.deleted


async def test_active_history_preserves_latest_future_and_pending_event_identity(store):
    newest = snapshot(days=20, expires_days=19)
    old = snapshot(days=40, expires_days=39)
    store.rows[WeatherForecastSnapshot] = [newest, old]
    store.active.add(str(POINT))
    past = WeatherChangeEvent(
        id=uuid4(),
        location_id=POINT,
        event_key="a" * 64,
        expected_end=NOW - timedelta(days=80),
        detected_at=NOW - timedelta(days=100),
    )
    future = WeatherChangeEvent(
        id=uuid4(),
        location_id=POINT,
        event_key="b" * 64,
        expected_end=NOW + timedelta(hours=1),
        detected_at=NOW - timedelta(days=100),
    )
    protected = WeatherChangeEvent(
        id=uuid4(),
        location_id=POINT,
        event_key="c" * 64,
        expected_end=NOW - timedelta(days=80),
        detected_at=NOW - timedelta(days=100),
    )
    store.rows[WeatherChangeEvent] = [past, future, protected]
    store.pending = retention.PendingWeatherReferences(event_keys=frozenset({"c" * 64}))
    result = await retention.PostgresWeatherRetention(store)._point(POINT, NOW, CONFIG, 200)
    assert result["snapshots_deleted"] == 1 and store.rows[WeatherForecastSnapshot] == [newest]
    assert result["events_deleted"] == 1 and store.rows[WeatherChangeEvent] == [future, protected]
    sql = "\n".join(store.queries)
    assert "NOWAIT" in sql and "SKIP LOCKED" in sql
    assert sql.index("FROM users") < sql.index("FROM weather_locations")
    assert not any("DELETE" in item and "territor" in item for item in store.queries)


async def test_deletion_budget_includes_metadata_and_markers(store):
    store.active.add(str(POINT))
    store.rows[WeatherForecastSnapshot] = [
        snapshot(days=n, expires_days=n - 1) for n in range(20, 30)
    ]
    result = await retention.PostgresWeatherRetention(store)._point(POINT, NOW, CONFIG, 2)
    assert result["snapshots_deleted"] == 2 and len(store.deleted) == 2


@pytest.mark.parametrize("limit", [0, 201, True, -1])
async def test_invalid_limit_never_opens_transaction(limit):
    def forbidden():
        raise AssertionError("validation must happen before DB")

    with pytest.raises(ValueError):
        await retention.PostgresWeatherRetention(forbidden).cleanup(NOW, limit)


async def test_active_marker_clear_counts_toward_same_total_budget(store):
    store.active.add(str(POINT))
    store.rows[RuntimeSetting].append(
        RuntimeSetting(
            key=retention.UNUSED_PREFIX + str(POINT),
            value={"version": 1, "since": (NOW - timedelta(days=100)).isoformat()},
        )
    )
    store.rows[WeatherForecastSnapshot] = [
        snapshot(days=n, expires_days=n - 1) for n in range(20, 30)
    ]
    result = await retention.PostgresWeatherRetention(store)._point(POINT, NOW, CONFIG, 2)
    assert result["metadata_deleted"] == 1 and result["snapshots_deleted"] == 1
    assert len(store.deleted) == 2


async def test_pending_scan_is_bounded_and_overflow_protects_unknown_references():
    class QueueSession:
        async def execute(self, statement):
            assert statement._limit_clause.value == 1001
            return Result([{"point_id": str(POINT), "event_keys": []}] * 1001)

    assert (await retention._pending(QueueSession())).uncertain


async def test_pagination_restart_covers_beyond_first_two_hundred_without_starvation(monkeypatch):
    class PagingSession(Session):
        async def execute(self, statement):
            assert statement.column_descriptions[0]["entity"] is WeatherLocation
            assert statement._limit_clause.value == 200
            after = statement.compile().params.get("id_1", UUID(int=0))
            return Result([UUID(int=i) for i in range(1, 501) if UUID(int=i) > after][:200])

    class PagingStore(Store):
        def __call__(self):
            return PagingSession(self)

    store = PagingStore()
    observed = []

    async def point(_self, identifier, now, config, budget):
        observed.append(identifier)
        return {"points_examined": 1}

    async def runs(*_args):
        return 0

    monkeypatch.setattr(retention.PostgresWeatherRetention, "_point", point)
    monkeypatch.setattr(retention.PostgresWeatherRetention, "_runs", runs)
    for _ in range(3):
        await retention.PostgresWeatherRetention(store).cleanup(NOW)
    assert observed == [UUID(int=i) for i in range(1, 501)]
    assert (await retention.PostgresWeatherRetention(store).cleanup(NOW)).points_examined == 0
    await retention.PostgresWeatherRetention(store).cleanup(NOW)
    assert len(observed) == 700 and observed[-1] == UUID(int=200)
