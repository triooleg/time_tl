"""Consistent DB-derived selectors, synthetic SQL ports only and no external I/O."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.elements import TextClause

from tests.domain.test_geographic_coverage import CATALOG, NOW, VERSION, kat, point
from time_tl.domain.power_outages.models import PowerProvider
from time_tl.infrastructure.db.geographic_coverage import (
    PostgresActiveGeographicCoverage,
    read_coverage_in_session,
)
from time_tl.infrastructure.db.models import Territory, WeatherLocation


def user(number=1, *, power=None, weather=False, requested=False, region="h1", alert=False):
    return {
        "user_key": UUID(int=number),
        "confirmed_region_id": region,
        "mode": "ALERTS_ONLY" if alert else None,
        "alerts_enabled": alert,
        "changed_at": NOW,
        "scope_provider": "alerts.in.ua" if alert else None,
        "scope_version": VERSION if alert else None,
        "scope_selected": region if alert else None,
        "scope_regions": ["o1", "r1", region] if alert else None,
        "power_preference": power,
        "power_preference_key": "power_api_user_pref:" + str(UUID(int=number))
        if power is not None
        else None,
        "power_changed_at": NOW if power is not None else None,
        "weather_enabled": weather,
        "forecast_requested": requested,
        "weather_changed_at": NOW if weather or requested else None,
    }


def power(version=1, *, enabled=False, group="1.1"):
    value = {"version": version, "provider": "dtek_dnipro", "group": group, "revision": 1}
    if version == 2:
        value.update(
            enabled=enabled,
            outage_enabled=False,
            restoration_enabled=False,
            outage_lead=10,
            restoration_lead=10,
        )
    return value


class Result:
    def __init__(self, rows=(), scalar=None):
        self.rows, self.scalar = list(rows), scalar

    def scalar_one_or_none(self):
        return self.scalar

    def scalars(self):
        return self

    def mappings(self):
        return self

    def all(self):
        return self.rows


class Store:
    def __init__(self, users=()):
        self.users = list(users)
        self.version, self.queries, self.on_users_read = VERSION, [], None
        self.territories = [
            SimpleNamespace(
                region_id=row.region_id,
                external_code=row.katottg,
                name=row.name,
                kind=row.kind,
                parent_region_id=row.parent_region_id,
                is_selectable=row.selectable,
            )
            for row in CATALOG
        ]
        approved = point()
        self.points = [
            SimpleNamespace(
                id=UUID(approved.location_id),
                katottg_code=approved.katottg,
                region_id=approved.region_id,
                locality_name=approved.locality_name,
                latitude=approved.latitude,
                longitude=approved.longitude,
                coverage_type=approved.coverage_type,
                enabled=approved.enabled,
                provenance_url=approved.source.url,
                provenance_sha256=approved.source.sha256,
                provenance_version=approved.source.version,
                approved_at=approved.source.approved_at,
            )
        ]

    def __call__(self):
        return Session(self)


class Transaction:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        store = self.session.store
        self.session.visible = deepcopy(
            (store.users, store.territories, store.points, store.version)
        )

    async def __aexit__(self, *args):
        pass


class Session:
    def __init__(self, store):
        self.store, self.visible = store, None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def begin(self):
        return Transaction(self)

    async def execute(self, statement):
        sql = str(statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        self.store.queries.append(sql)
        users, territories, points, version = self.visible
        if isinstance(statement, TextClause):
            assert sql == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            return Result()
        assert sql.startswith("SELECT ")
        if "power_preference" in statement.selected_columns.keys():
            if self.store.on_users_read is not None:
                self.store.on_users_read(self.store)
            return Result(users)
        entity = statement.column_descriptions[0].get("entity")
        if entity is Territory:
            return Result(territories)
        if entity is WeatherLocation:
            return Result(points)
        return Result(scalar=version)


@pytest.fixture(autouse=True)
def forbid_http(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("coverage is a database projection, never per-user HTTP")

    monkeypatch.setattr("httpx.AsyncClient", forbidden)


async def test_hundred_subscribers_one_point_fixed_batch_queries_no_personal_projection():
    store = Store(
        [user(number, power=power(), requested=True, alert=True) for number in range(1, 101)]
    )
    snapshot = await PostgresActiveGeographicCoverage(store).snapshot(NOW)
    assert len(snapshot.weather_points) == 1 and snapshot.weather_points[0].user_count == 100
    assert snapshot.power_group_keys == {(PowerProvider.DTEK, "1.1")}
    assert len(store.queries) == 5
    assert store.queries[0].startswith("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    sql = "\n".join(store.queries)
    assert "telegram_user_id" not in sql and "region_name" not in sql
    assert "encrypted" not in sql and "token" not in sql and "coordinate" not in sql
    assert "weather_locations.katottg_code IN" in sql
    assert "alerts.in.ua" in sql and "alerts_in_ua" not in sql


@pytest.mark.parametrize("preference", [power(), power(2, enabled=False)])
async def test_legacy_and_disabled_master_remain_data_view_power_demand(preference):
    snapshot = await PostgresActiveGeographicCoverage(Store([user(power=preference)])).snapshot(NOW)
    assert snapshot.power_group_keys == {(PowerProvider.DTEK, "1.1")}
    assert snapshot.power_groups[0].notification_user_count == 0
    assert not snapshot.weather_points


async def test_zero_demand_skips_weather_query_and_restart_tracks_deletion():
    store = Store([user(requested=True)])
    first = await PostgresActiveGeographicCoverage(store).snapshot(NOW)
    assert len(first.weather_location_ids) == 1
    store.users.clear()
    store.queries.clear()
    second = await PostgresActiveGeographicCoverage(store).snapshot(NOW + timedelta(minutes=1))
    assert not second.weather_location_ids and not second.power_group_keys
    assert len(store.queries) == 4 and not any("FROM weather_locations" in q for q in store.queries)
    assert len(store.territories) == len(CATALOG)


async def test_region_group_switch_and_weather_optout_are_live_db_reads():
    store = Store([user(power=power(2), requested=True)])
    repository = PostgresActiveGeographicCoverage(store)
    first = await repository.snapshot(NOW)
    store.users[0].update(
        confirmed_region_id="h2",
        power_preference=power(2, group="3.2"),
        weather_enabled=False,
        forecast_requested=False,
    )
    second = await repository.snapshot(NOW)
    assert first.power_group_keys != second.power_group_keys
    assert second.power_group_keys == {(PowerProvider.DTEK, "3.2")}
    assert not second.weather_points
    assert second.territories[0].territory.katottg == kat(4)


async def test_one_transaction_does_not_mix_concurrent_optout_with_old_source_rows():
    store = Store([user(requested=True)])
    store.on_users_read = lambda current: current.users.clear()
    first = await PostgresActiveGeographicCoverage(store).snapshot(NOW)
    assert len(first.weather_points) == 1
    second = await PostgresActiveGeographicCoverage(store).snapshot(NOW)
    assert not second.weather_points


async def test_same_session_helper_never_changes_existing_write_transaction_mode():
    store = Store([user(requested=True)])
    async with store() as session, session.begin():
        snapshot = await read_coverage_in_session(session, NOW)
    assert len(snapshot.weather_points) == 1
    assert all(sql.startswith("SELECT ") for sql in store.queries)


@pytest.mark.parametrize(
    "preference",
    [{"version": True}, {**power(), "group": "unknown"}, {**power(), "provider": "unapproved"}],
)
async def test_invalid_power_json_is_excluded_and_surfaced_without_other_users_loss(preference):
    store = Store([user(1, power=preference), user(2, requested=True)])
    snapshot = await PostgresActiveGeographicCoverage(store).snapshot(NOW)
    assert not snapshot.power_groups and len(snapshot.weather_points) == 1
    assert {reason.reason: reason.user_count for reason in snapshot.excluded}[
        "INVALID_POWER_PREFERENCE"
    ] == 1


async def test_null_json_existing_preference_is_invalid_not_a_missing_row():
    malformed = user()
    malformed["power_preference_key"] = "power_api_user_pref:" + str(malformed["user_key"])
    snapshot = await PostgresActiveGeographicCoverage(Store([malformed])).snapshot(NOW)
    assert {reason.reason: reason.user_count for reason in snapshot.excluded}[
        "INVALID_POWER_PREFERENCE"
    ] == 1


async def test_missing_active_catalog_excludes_weather_but_preserves_manual_groups():
    store = Store([user(power=power(), requested=True)])
    store.version = None
    snapshot = await PostgresActiveGeographicCoverage(store).snapshot(NOW)
    assert snapshot.power_group_keys == {(PowerProvider.DTEK, "1.1")}
    assert not snapshot.territories and not snapshot.weather_points
    assert any(item.reason == "ACTIVE_CATALOG_UNAVAILABLE" for item in snapshot.excluded)
