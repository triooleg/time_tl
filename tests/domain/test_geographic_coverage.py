"""Derived demand never guesses geography or creates per-user weather work."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from time_tl.domain.coverage import (
    CoverageAlertScope,
    CoveragePowerSelection,
    CoverageSubscriber,
    CoverageTerritoryNode,
    CoverageWeatherLocation,
    WeatherSourceReference,
    derive_active_coverage,
)
from time_tl.domain.power_outages.models import PowerProvider

NOW = datetime(2026, 11, 5, 10, tzinfo=UTC)
VERSION = "synthetic-approved-v1"


def kat(number):
    return "UA" + str(number).zfill(17)


CATALOG = (
    CoverageTerritoryNode("o1", kat(1), "First region", "OBLAST", None),
    CoverageTerritoryNode("r1", kat(2), "First district", "RAION", "o1"),
    CoverageTerritoryNode("h1", kat(3), "Same public name", "HROMADA", "r1"),
    CoverageTerritoryNode("h2", kat(4), "Same public name", "HROMADA", "r1"),
    CoverageTerritoryNode("o2", kat(5), "Other region", "OBLAST", None),
    CoverageTerritoryNode("r2", kat(6), "Other district", "RAION", "o2"),
    CoverageTerritoryNode("h3", kat(7), "Other community", "HROMADA", "r2"),
)


def subscriber(number=1, *, region="h1", alert=False, power=False, weather=False, requested=False):
    path = ("o1", "r1", region) if region in {"h1", "h2"} else ("o2", "r2", region)
    return CoverageSubscriber(
        str(UUID(int=number)),
        region,
        "ALERTS_ONLY" if alert else None,
        alert,
        NOW,
        CoverageAlertScope("alerts.in.ua", VERSION, region, path) if alert else None,
        CoveragePowerSelection(PowerProvider.DTEK, "1.1", False) if power else None,
        NOW if power else None,
        weather,
        requested,
        NOW if weather or requested else None,
    )


def point(number=1000, *, code=None, region="h1", kind="BASE"):
    return CoverageWeatherLocation(
        str(UUID(int=number)),
        kat(3) if code is None else code,
        region,
        "Approved public centre",
        48.456789,
        34.987654,
        kind,
        True,
        WeatherSourceReference(
            "https://example.invalid/approved-points.csv",
            "a" * 64,
            "source-v1",
            NOW - timedelta(days=1),
        ),
    )


def derive(users=(), *, catalog=CATALOG, points=None, version=VERSION, now=NOW):
    return derive_active_coverage(
        now,
        catalog_version=version,
        territories=tuple(catalog),
        subscribers=tuple(users),
        weather_locations=tuple((point(),) if points is None else points),
    )


def reasons(snapshot):
    return {item.reason: item.user_count for item in snapshot.excluded}


def test_zero_users_produces_no_work_and_preserves_input_catalog():
    snapshot = derive()
    assert not snapshot.territories and not snapshot.power_group_keys
    assert not snapshot.weather_location_ids and not snapshot.active_alert_region_ids
    assert snapshot.active_user_count == snapshot.requested_user_count == 0
    assert len(CATALOG) == 7


def test_hundred_users_share_one_point_one_power_group_and_verified_hierarchy():
    users = [subscriber(number, alert=True, power=True, requested=True) for number in range(1, 101)]
    snapshot = derive(users)
    assert snapshot.active_user_count == snapshot.requested_user_count == 100
    assert (
        len(snapshot.territories) == len(snapshot.weather_points) == len(snapshot.power_groups) == 1
    )
    assert snapshot.weather_points[0].user_count == 100
    assert snapshot.weather_points[0].notification_user_count == 0
    area = snapshot.territories[0]
    assert area.territory.katottg == kat(3) and area.oblast.region_id == "o1"
    assert area.raion.region_id == "r1" and area.hromada.region_id == "h1"
    assert area.alert_user_count == area.power_user_count == area.weather_user_count == 100
    assert snapshot.active_alert_region_ids == {"o1", "r1", "h1"}


def test_forecast_interest_remains_when_notification_master_is_off():
    assert len(derive([subscriber(requested=True)]).weather_points) == 1
    assert derive([subscriber(requested=True)]).weather_points[0].notification_user_count == 0
    assert derive([subscriber(weather=True)]).weather_points[0].notification_user_count == 1
    assert not derive([subscriber(weather=False, requested=False)]).weather_points


def test_manual_graph_only_power_survives_missing_or_unrelated_air_geography():
    unknown = subscriber(region="unknown", power=True)
    snapshot = derive([unknown])
    assert snapshot.power_group_keys == {(PowerProvider.DTEK, "1.1")}
    assert snapshot.power_groups[0].notification_user_count == 0
    assert not snapshot.territories and reasons(snapshot)["UNMAPPED_CONFIRMED_TERRITORY"] == 1
    unrelated = derive([subscriber(region="h3", power=True)])
    assert unrelated.power_group_keys == snapshot.power_group_keys
    assert unrelated.territories[0].oblast.region_id == "o2"


def test_region_group_optout_delete_and_restart_are_reflected_without_events():
    original = subscriber(power=True, requested=True)
    first = derive([original])
    moved = replace(
        original,
        confirmed_region_id="h3",
        changed_at=NOW + timedelta(minutes=1),
        power=CoveragePowerSelection(PowerProvider.CEK, "2.1"),
    )
    second = derive([moved], points=(point(1001, code=kat(7), region="h3"),))
    assert first.power_group_keys != second.power_group_keys
    assert second.weather_location_ids == {str(UUID(int=1001))}
    assert second.territories[0].territory.region_id == "h3"
    assert (
        derive(
            [moved],
            points=(point(1001, code=kat(7), region="h3"),),
            now=NOW + timedelta(minutes=10),
        ).fingerprint
        == second.fingerprint
    )
    assert not derive([replace(moved, forecast_requested=False)]).weather_points
    assert not derive([]).weather_points and not derive([]).power_group_keys


def test_names_are_never_weather_matching_keys():
    snapshot = derive([subscriber(region="h2", requested=True)])
    assert not snapshot.weather_points
    assert reasons(snapshot)["NO_APPROVED_WEATHER_POINT"] == 1
    assert snapshot.territories[0].territory.name == CATALOG[2].name


@pytest.mark.parametrize(
    "change",
    [
        {"enabled": False},
        {"source": None},
        {"region_id": "wrong"},
        {"katottg": kat(999)},
        {"latitude": float("nan")},
        {"latitude": 60.0},
        {"longitude": 50.0},
        {"latitude": True},
    ],
)
def test_unapproved_incompatible_or_invalid_weather_points_never_become_work(change):
    snapshot = derive([subscriber(requested=True)], points=(replace(point(), **change),))
    assert not snapshot.weather_location_ids
    assert reasons(snapshot)["NO_APPROVED_WEATHER_POINT"] == 1


@pytest.mark.parametrize(
    "source",
    [
        WeatherSourceReference("https://user:secret@example.invalid/x", "a" * 64, "v", NOW),
        WeatherSourceReference("https://example.invalid/x?api_key=secret", "a" * 64, "v", NOW),
        WeatherSourceReference("https://example.invalid/x", "invalid", "v", NOW),
        WeatherSourceReference("https://example.invalid/x", "a" * 64, "v", NOW + timedelta(days=1)),
    ],
)
def test_source_provenance_must_be_approved_public_and_current(source):
    assert not derive(
        [subscriber(requested=True)], points=(replace(point(), source=source),)
    ).weather_points


def test_base_and_extended_points_remain_distinct_and_never_mixed_by_name():
    snapshot = derive([subscriber(weather=True)], points=(point(), point(1001, kind="EXTENDED")))
    assert len(snapshot.weather_points) == 2
    assert {item.location.coverage_type for item in snapshot.weather_points} == {"BASE", "EXTENDED"}
    assert all(item.user_count == 1 for item in snapshot.weather_points)


@pytest.mark.parametrize(
    "catalog",
    [
        (replace(CATALOG[0], katottg=None), *CATALOG[1:]),
        (CATALOG[0], replace(CATALOG[1], parent_region_id="h1"), *CATALOG[2:]),
        (*CATALOG[:3], replace(CATALOG[3], katottg=kat(3)), *CATALOG[4:]),
    ],
)
def test_bad_mapping_or_ancestor_excludes_geography_without_dropping_manual_power(catalog):
    snapshot = derive([subscriber(alert=True, power=True, requested=True)], catalog=catalog)
    assert not snapshot.territories and not snapshot.weather_points
    assert snapshot.power_group_keys == {(PowerProvider.DTEK, "1.1")}
    assert not snapshot.active_alert_region_ids


def test_catalog_absence_or_empty_catalog_is_surfaced_without_new_geography():
    for snapshot in (
        derive([subscriber(requested=True)], version=None),
        derive([subscriber(requested=True)], catalog=()),
    ):
        assert not snapshot.weather_points and not snapshot.territories
        assert reasons(snapshot)["ACTIVE_CATALOG_UNAVAILABLE"] == 1


def test_scope_is_actual_consented_complete_provider_hierarchy():
    user = subscriber(alert=True)
    for scope in (
        None,
        replace(user.alert_scope, catalog_version="retired"),
        replace(user.alert_scope, provider="fake"),
        replace(user.alert_scope, applicable_region_ids=("h1",)),
        replace(user.alert_scope, applicable_region_ids=("o1", "r1", "h1", "h2")),
    ):
        snapshot = derive([replace(user, alert_scope=scope)])
        assert (
            not snapshot.active_alert_region_ids
            and reasons(snapshot)["UNVERIFIED_ALERT_SCOPE"] == 1
        )
    assert not derive([replace(user, mode=None)]).active_alert_region_ids
    assert not derive([replace(user, alerts_enabled=False)]).active_alert_region_ids


def test_selected_parent_keeps_official_unmapped_child_state_in_alert_closure():
    hidden = CoverageTerritoryNode("hidden", None, "Hidden official child", "HROMADA", "r1", False)
    parent = replace(
        subscriber(alert=True),
        confirmed_region_id="r1",
        alert_scope=CoverageAlertScope(
            "alerts.in.ua", VERSION, "r1", ("o1", "r1", "h1", "h2", "hidden")
        ),
    )
    snapshot = derive([parent], catalog=(*CATALOG, hidden))
    assert snapshot.active_alert_region_ids == {"o1", "r1", "h1", "h2", "hidden"}
    assert len(snapshot.territories) == 1 and snapshot.territories[0].territory.region_id == "r1"


def test_duplicate_rows_dedupe_but_conflicting_observation_does_not_pick_a_region():
    user = subscriber(requested=True)
    assert derive([user, user]).weather_points[0].user_count == 1
    snapshot = derive([user, replace(user, confirmed_region_id="h2")])
    assert not snapshot.weather_points
    assert reasons(snapshot)["CONFLICTING_USER_OBSERVATION"] == 1


def test_snapshot_representation_contains_no_subscriber_ids_or_coordinates():
    user = subscriber(requested=True)
    snapshot = derive([user])
    public = repr(snapshot)
    assert user.user_key not in public
    assert "48.456789" not in public and "34.987654" not in public
    assert "https://example.invalid" not in public
