"""Offline transaction-port tests; these are not live PostgreSQL evidence."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects.postgresql.dml import Insert
from sqlalchemy.sql.dml import Update

from time_tl.domain.weather import (
    ForecastHour,
    ForecastRun,
    ForecastSnapshot,
    ForecastStatus,
    WeatherPoint,
)
from time_tl.infrastructure.db import weather as db
from time_tl.infrastructure.db.models import (
    RuntimeSetting,
    User,
    UserWeatherPreference,
    WeatherChangeEvent,
    WeatherForecastHour,
    WeatherForecastRun,
    WeatherForecastSnapshot,
    WeatherLocation,
)
from time_tl.infrastructure.db.weather_configuration import SETTING, WeatherConfiguration
from time_tl.infrastructure.weather.met_norway import WeatherProviderError

NOW = datetime(2026, 11, 5, 10, tzinfo=UTC)
POINT = UUID(int=100)
ACTOR = 123
USER = UUID(int=200)


def forecast(*, fetched=NOW, wind=20, cache=None):
    return ForecastSnapshot(
        db.PROVIDER,
        str(POINT),
        fetched,
        fetched,
        fetched + timedelta(hours=8),
        (
            ForecastHour(
                NOW + timedelta(hours=2),
                wind_gust_ms=wind,
                cloud_cover_percent=25,
                feels_like_c=None,
            ),
        ),
        ForecastStatus.FRESH,
        "https://api.met.no/weatherapi/locationforecast/2.0/compact",
        cache_expires_at=cache,
    )


class Result:
    def __init__(self, values=()):
        self.values = list(values)

    def scalars(self):
        return self

    def all(self):
        return self.values


class Store:
    def __init__(self):
        self.rows = {
            model: []
            for model in (
                RuntimeSetting,
                User,
                UserWeatherPreference,
                WeatherChangeEvent,
                WeatherForecastHour,
                WeatherForecastRun,
                WeatherForecastSnapshot,
                WeatherLocation,
            )
        }
        self.rows[User] = [User(id=USER, telegram_user_id=ACTOR)]
        self.rows[WeatherLocation] = [
            WeatherLocation(
                id=POINT,
                region_id="h1",
                katottg_code="UA00000000000000003",
                locality_name="Synthetic",
                latitude=48.5,
                longitude=35.0,
                coverage_type="BASE",
                enabled=True,
                provenance_url="https://example.org/approved",
                provenance_sha256="a" * 64,
                provenance_version="v1",
                approved_at=NOW - timedelta(days=1),
            )
        ]
        self.rows[RuntimeSetting] = [
            RuntimeSetting(
                key=SETTING,
                value=WeatherConfiguration(
                    enabled=True, terms_approved=True, revision=1
                ).model_dump(),
                updated_by="test",
            )
        ]
        self.demand, self.flushes, self.queries = True, 0, []

    def __call__(self):
        return Session(self)

    def setting(self, key):
        return next((r for r in self.rows[RuntimeSetting] if r.key == key), None)


class Transaction:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        self.original = deepcopy(self.session.store.rows)

    async def __aexit__(self, typ, *_):
        if typ is not None:
            self.session.store.rows = self.original


class Session:
    def __init__(self, store):
        self.store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    def begin(self):
        return Transaction(self)

    async def get(self, model, key):
        attr = (
            "key"
            if model is RuntimeSetting
            else "user_id"
            if model is UserWeatherPreference
            else "id"
        )
        return next((r for r in self.store.rows[model] if getattr(r, attr) == key), None)

    def add(self, row):
        self.store.rows[type(row)].append(row)

    async def flush(self):
        self.store.flushes += 1
        for row in self.store.rows[WeatherForecastSnapshot]:
            # Real PG rejects an incomplete INSERT on the first flush.
            for name in ("fetched_at", "provider_updated_at", "expires_at", "status", "source_url"):
                assert getattr(row, name) is not None, name
            if row.id is None:
                row.id = uuid4()
        for row in self.store.rows[UserWeatherPreference]:
            assert row.revision >= 1

    def selected(self, statement):
        model = statement.column_descriptions[0]["entity"]
        rows = list(self.store.rows.get(model, ()))
        params = statement.compile().params
        for key, value in params.items():
            attr = key.rsplit("_", 1)[0]
            if attr == "param":
                continue
            if rows and hasattr(rows[0], attr):
                rows = [r for r in rows if getattr(r, attr) == value]
        if model is WeatherForecastSnapshot:
            rows.sort(key=lambda r: (r.fetched_at, str(r.id)), reverse=True)
            if statement._limit_clause is not None:
                rows = rows[:1]
        if model is WeatherForecastHour:
            rows.sort(key=lambda r: r.forecast_at)
        column = statement.column_descriptions[0]["expr"]
        if column is not model and hasattr(column, "key"):
            return [getattr(r, column.key) for r in rows]
        return rows

    async def scalar(self, statement):
        self.store.queries.append(str(statement))
        return next(iter(self.selected(statement)), None)

    async def execute(self, statement):
        self.store.queries.append(str(statement))
        if isinstance(statement, Insert):
            model = next(m for m in self.store.rows if m.__tablename__ == statement.table.name)
            values = {k.key: v.value for k, v in statement._values.items()}
            key = (
                "key"
                if model is RuntimeSetting
                else "event_key"
                if model is WeatherChangeEvent
                else "id"
            )
            existing = next(
                (r for r in self.store.rows[model] if getattr(r, key) == values.get(key)), None
            )
            conflict = statement._post_values_clause
            if existing is not None:
                if hasattr(conflict, "update_values_to_set"):
                    for name, value in conflict.update_values_to_set:
                        setattr(existing, name, value)
                return Result()
            row = model(**values)
            if model is WeatherChangeEvent:
                row.id = uuid4()
            self.add(row)
            return Result()
        if isinstance(statement, Update):
            params = statement.compile().params
            for row in self.store.rows[WeatherChangeEvent]:
                if all(
                    getattr(row, k.rsplit("_", 1)[0]) == v
                    for k, v in params.items()
                    if k != "status"
                ):
                    row.status = params["status"]
            return Result()
        return Result(self.selected(statement))


@pytest.fixture
def store(monkeypatch):
    store = Store()

    async def demand(session, identifier, now):
        assert session.store is store and identifier == POINT and now.tzinfo
        if not store.demand:
            raise WeatherProviderError("DEMAND_REMOVED")
        location = store.rows[WeatherLocation][0]
        return WeatherPoint(str(location.id), location.latitude, location.longitude)

    monkeypatch.setattr(db, "_lock_demand", demand)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("no external I/O in persistence")

    monkeypatch.setattr("httpx.AsyncClient", forbidden)
    return store


def storage(store, **kwargs):
    return db.PostgresWeatherStorage(store, clock=lambda: NOW, **kwargs)


async def seed_run(repo):
    identifier = str(uuid4())
    await repo.begin_run(identifier, NOW, 1)
    return identifier


async def claim(repo, store, now=NOW):
    row = store.rows[WeatherLocation][0]
    point = WeatherPoint(str(row.id), row.latitude, row.longitude)
    await repo.claim_source_request(point, now)
    return point


async def test_absent_preferences_are_read_only_and_do_not_enable_notifications(store):
    repo = db.PostgresWeatherPreferences(store)
    assert await repo.get(ACTOR) == db.WeatherPreferences()
    assert not store.rows[UserWeatherPreference]
    result = await repo.request_forecast(ACTOR)
    assert result.forecast_requested and not result.weather_enabled and result.revision == 1
    assert await repo.request_forecast(ACTOR) == result
    assert len(store.rows[UserWeatherPreference]) == 1


async def test_preference_cas_master_off_retains_data_interest_and_full_disable_removes_it(store):
    repo = db.PostgresWeatherPreferences(store)
    await repo.request_forecast(ACTOR)
    enabled = await repo.toggle_notifications(ACTOR, True, expected_revision=1)
    assert enabled.weather_enabled and enabled.forecast_requested
    with pytest.raises(ValueError, match="revision_conflict"):
        await repo.update(ACTOR, expected_revision=1, notification_lead_minutes=10)
    disabled = await repo.toggle_notifications(ACTOR, False, expected_revision=2)
    assert disabled.forecast_requested and not disabled.weather_enabled
    result = await repo.disable_weather(ACTOR, expected_revision=3)
    assert not result.forecast_requested and not result.weather_enabled


@pytest.mark.parametrize(
    "changes",
    [
        {"weather_enabled": 1},
        {"revision": 2},
        {"notification_lead_minutes": True},
        {"notification_lead_minutes": 1441},
        {"daily_forecast_time": time(7, 0, 1)},
        {"daily_forecast_time": time(7, tzinfo=UTC)},
        {"unknown": False},
    ],
)
async def test_invalid_preferences_never_write(store, changes):
    with pytest.raises(ValueError):
        await db.PostgresWeatherPreferences(store).update(ACTOR, expected_revision=0, **changes)
    assert not store.rows[UserWeatherPreference]


@pytest.mark.parametrize("actor", [True, 0, -1, "123"])
async def test_invalid_or_deleted_account_never_recreated(store, actor):
    with pytest.raises(ValueError):
        await db.PostgresWeatherPreferences(store).request_forecast(actor)
    store.rows[User] = []
    with pytest.raises(ValueError, match="account_missing"):
        await db.PostgresWeatherPreferences(store).request_forecast(ACTOR)


async def test_durable_global_spacing_and_retry_after_cannot_be_shortened(store):
    repo = storage(store)
    await claim(repo, store)
    restarted = storage(store)
    with pytest.raises(WeatherProviderError, match="REQUEST_DEFERRED") as error:
        await claim(restarted, store, NOW + timedelta(milliseconds=999))
    assert error.value.retry_at == NOW + timedelta(seconds=1)
    await repo.record_source_retry(db.PROVIDER, NOW + timedelta(minutes=10))
    await restarted.record_source_retry(db.PROVIDER, NOW + timedelta(seconds=30))
    assert await storage(store).source_retry_at(db.PROVIDER) == NOW + timedelta(minutes=10)
    with pytest.raises(WeatherProviderError, match="REQUEST_DEFERRED"):
        await claim(restarted, store, NOW + timedelta(seconds=2))


async def test_final_permission_rechecks_retry_demand_config_and_consumes_grant(store):
    repo = storage(store, expected_configuration_revision=1)
    point = await claim(repo, store)
    store.demand = False
    with pytest.raises(WeatherProviderError, match="DEMAND_REMOVED"):
        await repo.authorize_source_request(point, NOW)
    store.demand = True
    store.setting(SETTING).value["revision"] = 2
    with pytest.raises(WeatherProviderError, match="CONFIGURATION_CHANGED"):
        await repo.authorize_source_request(point, NOW)
    store.setting(SETTING).value["revision"] = 1
    await repo.record_source_retry(db.PROVIDER, NOW + timedelta(minutes=1))
    with pytest.raises(WeatherProviderError, match="RATE_LIMITED"):
        await repo.authorize_source_request(point, NOW)


async def test_diagnostic_http_expiry_shared_after_restart_and_grant_cannot_replay(store):
    repo = storage(store)
    point = await claim(repo, store)
    await repo.authorize_source_request(point, NOW)
    with pytest.raises(WeatherProviderError, match="GRANT_EXPIRED"):
        await repo.authorize_source_request(point, NOW)
    await repo.record_source_response(point, NOW + timedelta(hours=1), NOW)
    with pytest.raises(WeatherProviderError, match="REQUEST_DEFERRED") as error:
        await claim(storage(store), store, NOW + timedelta(seconds=2))
    assert error.value.retry_at == NOW + timedelta(hours=1)
    assert not store.rows[WeatherForecastSnapshot]


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"version": True, "retry_at": None, "next_request_at": None},
        {"version": 1, "retry_at": "2026-11-05T10:00:00", "next_request_at": None},
        {"version": 1, "retry_at": False, "next_request_at": None},
    ],
)
async def test_corrupt_source_permission_fails_closed(store, value):
    store.rows[RuntimeSetting].append(RuntimeSetting(key=db.SOURCE_STATE_KEY, value=value))
    with pytest.raises(WeatherProviderError, match="INVALID_SOURCE_PERMISSION"):
        await claim(storage(store), store)


async def test_snapshot_requires_persisted_run_and_initial_insert_complete_nulls_preserved(store):
    repo = storage(store)
    with pytest.raises(ValueError, match="run_required"):
        await repo.save(forecast())
    run = await seed_run(repo)
    await claim(repo, store)
    await repo.save(forecast())
    assert len(store.rows[WeatherForecastSnapshot]) == 1
    assert store.rows[WeatherForecastSnapshot][0].forecast_run_id == UUID(run)
    hour = store.rows[WeatherForecastHour][0]
    assert (
        hour.feels_like_c is None
        and hour.precipitation_mm is None
        and hour.cloud_cover_percent == 25
    )
    restored = await storage(store).previous(str(POINT))
    assert restored == forecast()


async def test_repeated_poll_same_hash_refreshes_metadata_without_duplicate_events_or_hours(store):
    repo = storage(store)
    await seed_run(repo)
    await claim(repo, store)
    await repo.save(forecast())
    keys = {row.event_key for row in store.rows[WeatherChangeEvent]}
    assert len(keys) == 1
    original_revision = store.rows[WeatherChangeEvent][0].source_revision
    await repo.save(forecast(fetched=NOW + timedelta(minutes=1)))
    assert len(store.rows[WeatherForecastSnapshot]) == 1
    assert len(store.rows[WeatherForecastHour]) == 1
    assert {row.event_key for row in store.rows[WeatherChangeEvent]} == keys
    assert store.rows[WeatherChangeEvent][0].source_revision == original_revision


async def test_corrections_restart_and_cancellation_do_not_repeat_or_clear_on_missing(store):
    repo = storage(store)
    await seed_run(repo)
    await claim(repo, store)
    await repo.save(forecast())
    await repo.save(forecast(fetched=NOW + timedelta(minutes=1), wind=25))
    assert [r.kind for r in store.rows[WeatherChangeEvent]] == ["UPCOMING", "CORRECTION"]
    restart = storage(store)
    await seed_run(restart)
    await restart.previous(str(POINT))
    await restart.save(forecast(fetched=NOW + timedelta(minutes=2), wind=25))
    assert len(store.rows[WeatherChangeEvent]) == 2
    await restart.save(forecast(fetched=NOW + timedelta(minutes=3), wind=None))
    assert all(r.status == "ACTIVE" for r in store.rows[WeatherChangeEvent])
    # Missing previous parameters cannot manufacture a cancellation either.
    await restart.save(forecast(fetched=NOW + timedelta(minutes=4), wind=25))
    await restart.save(forecast(fetched=NOW + timedelta(minutes=5), wind=5))
    assert store.rows[WeatherChangeEvent][-1].kind == "CANCELLATION"
    assert all(r.status == "CANCELLED" for r in store.rows[WeatherChangeEvent])


async def test_optout_or_coordinate_change_between_http_and_commit_rejects_cache(store):
    repo = storage(store)
    await seed_run(repo)
    await claim(repo, store)
    store.demand = False
    with pytest.raises(WeatherProviderError, match="DEMAND_REMOVED"):
        await repo.save(forecast())
    assert not store.rows[WeatherForecastSnapshot]
    store.demand = True
    store.rows[WeatherLocation][0].latitude += 0.1
    with pytest.raises(WeatherProviderError, match="POINT_CHANGED"):
        await repo.save(forecast())
    assert not store.rows[WeatherForecastSnapshot]


async def test_coordinate_rebind_cannot_read_old_cache_or_carry_http_expiry(store):
    repo = storage(store)
    await seed_run(repo)
    point = await claim(repo, store)
    await repo.authorize_source_request(point, NOW)
    await repo.record_source_response(point, NOW + timedelta(hours=2), NOW)
    await repo.save(forecast(cache=NOW + timedelta(hours=2)))
    store.rows[WeatherLocation][0].latitude += 0.1
    restarted = storage(store)
    assert await restarted.previous(str(POINT)) is None
    await claim(restarted, store, NOW + timedelta(seconds=2))


async def test_failure_never_overwrites_good_cache_and_lifecycle_finish_idempotent(store):
    repo = storage(store)
    identifier = await seed_run(repo)
    await claim(repo, store)
    await repo.save(forecast())
    await repo.record_failure(str(POINT), "HTTP_UNAVAILABLE", NOW)
    assert await repo.previous(str(POINT)) == forecast()
    run = ForecastRun(identifier, NOW, NOW + timedelta(seconds=3), 1, 1, 0)
    await repo.finish_run(run)
    await repo.finish_run(run)
    assert store.rows[WeatherForecastRun][0].status == "PASS"
    with pytest.raises(ValueError, match="already_finished"):
        await repo.finish_run(replace(run, status="PARTIAL"))


async def test_authority_guard_before_demand_and_after_write_rollback_on_revoke(store):
    calls = []

    async def guard(session, now):
        calls.append((session, now))
        if len(calls) == 2:
            raise WeatherProviderError("AUTHORITY_REVOKED")

    repo = storage(store, authority_guard=guard)
    with pytest.raises(WeatherProviderError, match="AUTHORITY_REVOKED"):
        await claim(repo, store)
    assert store.setting(db.SOURCE_STATE_KEY) is None
    assert not repo._request_grants


@pytest.mark.parametrize(
    "status", [ForecastStatus.UNKNOWN, ForecastStatus.STALE, ForecastStatus.PARTIAL]
)
async def test_incomplete_or_stale_snapshot_never_replaces_facts(store, status):
    repo = storage(store)
    await seed_run(repo)
    await claim(repo, store)
    with pytest.raises(WeatherProviderError, match="INVALID_FORECAST"):
        await repo.save(replace(forecast(), status=status))
    assert not store.rows[WeatherForecastSnapshot]


async def test_opt_in_real_postgres_transactions_and_first_insert():
    """No connection without explicit passwordless localhost weathercheck DSN.

    Creates/drops only its newly owned UUID schema; metadata checks are separate
    from the migration rehearsal owned by the release integrator.
    """
    import asyncio
    import os

    from sqlalchemy import select, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from tests.domain.test_geographic_coverage import CATALOG, VERSION
    from time_tl.infrastructure.db.base import Base
    from time_tl.infrastructure.db.models import (
        ActiveTerritoryCatalog,
        Territory,
        TerritoryCatalogVersion,
    )

    raw = os.environ.get("WEATHER_STORAGE_TEST_DATABASE_URL")
    if not raw:
        pytest.skip("WEATHER_STORAGE_TEST_DATABASE_URL unset; no DB connection attempted")
    url = make_url(raw)
    assert (
        url.drivername == "postgresql+asyncpg"
        and url.host == "127.0.0.1"
        and url.port is not None
        and 1024 < url.port < 65536
        and url.database == "weathercheck"
        and url.username == "weathercheck"
        and url.password is None
        and not url.query
    ), "Disposable local weathercheck DB only"
    schema = "weather_storage_" + uuid4().hex
    owner_engine = create_async_engine(raw)
    engine = None
    created = False
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
        engine = create_async_engine(raw, connect_args={"server_settings": {"search_path": schema}})
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            session.add(
                TerritoryCatalogVersion(
                    provider="alerts.in.ua",
                    catalog_version=VERSION,
                    status="ACTIVE",
                    source_revision=VERSION,
                    source_sha256="a" * 64,
                    record_count=len(CATALOG),
                    selectable_count=len(CATALOG),
                    hidden_nonselectable_count=0,
                    unresolved_total=0,
                )
            )
            await session.flush()
            for node in CATALOG:
                session.add(
                    Territory(
                        provider="alerts.in.ua",
                        catalog_version=VERSION,
                        region_id=node.region_id,
                        name=node.name,
                        kind=node.kind,
                        parent_region_id=node.parent_region_id,
                        external_code=node.katottg,
                        sort_key=node.name,
                        is_selectable=True,
                    )
                )
            session.add(ActiveTerritoryCatalog(provider="alerts.in.ua", catalog_version=VERSION))
            session.add(
                User(
                    id=USER, telegram_user_id=ACTOR, confirmed_region_id="h1", alerts_enabled=False
                )
            )
            session.add(Store().rows[WeatherLocation][0])
            session.add(Store().setting(SETTING))
        prefs = db.PostgresWeatherPreferences(factory)
        assert not (await prefs.get(ACTOR)).weather_enabled
        await prefs.request_forecast(ACTOR)
        repo = db.PostgresWeatherStorage(
            factory, clock=lambda: NOW, expected_configuration_revision=1
        )
        await seed_run(repo)
        point = WeatherPoint(str(POINT), 48.5, 35.0)
        competitors = [db.PostgresWeatherStorage(factory, clock=lambda: NOW) for _ in range(2)]
        results = await asyncio.gather(
            *(r.claim_source_request(point, NOW) for r in competitors), return_exceptions=True
        )
        assert sum(value is None for value in results) == 1
        assert sum(isinstance(value, WeatherProviderError) for value in results) == 1
        # Use winner's original grant/evidence, while preserving actual run FK.
        winner = competitors[next(i for i, value in enumerate(results) if value is None)]
        winner._run_id = repo._run_id
        await winner.authorize_source_request(point, NOW)
        await winner.save(forecast())
        assert await db.PostgresWeatherStorage(factory).previous(str(POINT)) == forecast()
        await winner.save(forecast(fetched=NOW + timedelta(minutes=1)))
        async with factory() as session:
            assert len((await session.scalars(select(WeatherForecastSnapshot))).all()) == 1
            assert len((await session.scalars(select(WeatherChangeEvent))).all()) == 1
        await prefs.disable_weather(ACTOR, expected_revision=1)
        with pytest.raises(WeatherProviderError, match="DEMAND_REMOVED"):
            await winner.save(forecast(fetched=NOW + timedelta(minutes=2), wind=25))
    finally:
        if engine is not None:
            await engine.dispose()
        if created:
            async with owner_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await owner_engine.dispose()
