"""SYNTHETIC national wire map: no provider-ID guessing or notification authority."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from time_tl.domain.power_outages.map import MapArea, RegionMap
from time_tl.domain.power_outages.models import SourceStatus
from time_tl.infrastructure.power_outages.alerts_energy_api import AlertsEnergyApiClient
from time_tl.infrastructure.power_outages.base import PowerFetchError, PowerFetchErrorCode
from time_tl.infrastructure.power_outages.map_api import AlertsEnergyMapApi

NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)
AREA = MapArea("synthetic-other-region", "UA" + "1" * 17, "Synthetic region")


def row(operator="unknown-national-operator", group="Група А-11"):
    return {"initiator": operator, "queue": group, "updated": "2026-11-04T15:00:00Z",
            "today_date": "2026-11-05", "tomorrow_date": "2026-11-06",
            "today": [0, 1, 2, 3] + [0] * 20, "tomorrow": [],
            "today_status": "ScheduleApplies", "tomorrow_status": "WaitingForSchedule",
            "today_checked_at": "2026-11-05T11:58:00Z"}


def region(rows=None, now=NOW):
    return RegionMap(AREA, tuple([row()] if rows is None else rows), now)


def api(handler):
    return AlertsEnergyMapApi(AlertsEnergyApiClient(
        api_key=SecretStr("SYNTHETIC-key-not-real"), transport=httpx.MockTransport(handler),
    ), clock=lambda: NOW)


async def test_overview_collects_all_regions_not_only_known_provider_enum():
    calls = []
    areas = [{"slug": "synthetic-region-" + str(i), "katottg": "UA" + str(i).zfill(17),
              "name": "Synthetic " + str(i), "mode": "scheduled"} for i in range(1, 26)]
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=areas)
    result = await api(handler).overview()
    assert len(result) == 25
    assert calls == ["/api/v1/source-registry/areas/overview"]
    assert {area.slug for area in result} == {area["slug"] for area in areas}


async def test_one_region_request_returns_all_operator_groups_without_registry_or_offset():
    calls = []
    rows = [row("operator-a", "1.1"), row("operator-b", "1.1"), row("operator-c", "5001")]
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=rows)
    result = await api(handler).region(AREA)
    assert [(item["initiator"], item["queue"]) for item in result.rows] == [
        ("operator-a", "1.1"), ("operator-b", "1.1"), ("operator-c", "5001"),
    ]
    assert calls == [f"/api/v1/source-registry/areas/{AREA.slug}/shutdowns"]


def test_old_publication_recent_source_check_is_fresh_wire_data():
    value = region()
    assert value.status is SourceStatus.FRESH
    assert value.source_updated_at < NOW - timedelta(hours=20)
    assert value.checked_at == NOW - timedelta(minutes=2)
    assert value.status_at(NOW + timedelta(minutes=40)) is SourceStatus.STALE


@pytest.mark.parametrize("change", ["publication", "check", "fetch", "row_order"])
def test_acquisition_publication_and_check_only_changes_do_not_advance_semantic_hash(change):
    source = [row("a"), row("b")]
    initial = region(source)
    modified = deepcopy(source)
    stamp = NOW
    if change == "publication":
        modified[0]["updated"] = "2026-11-04T16:00:00Z"
    elif change == "check":
        modified[0]["today_checked_at"] = "2026-11-05T11:59:00Z"
    elif change == "fetch":
        stamp += timedelta(seconds=10)
    else:
        modified.reverse()
    assert region(modified, stamp).semantic_hash == initial.semantic_hash


@pytest.mark.parametrize("change", ["slot", "group", "operator", "status", "emergency"])
def test_meaningful_data_changes_advance_hash(change):
    original = region()
    value = row()
    if change == "slot":
        value["today"][0] = 1
    elif change == "group":
        value["queue"] = "Інша група"
    elif change == "operator":
        value["initiator"] = "another-operator"
    elif change == "status":
        value["today_status"] = "EmergencyShutdowns"
    else:
        value["today_announcements"] = [{"kind": "Emergency", "state": "active",
                                         "scope": "regional"}]
    assert region([value]).semantic_hash != original.semantic_hash


def test_empty_map_is_no_schedule_unavailable_and_never_power_expected():
    value = region([])
    assert value.status is SourceStatus.UNAVAILABLE and value.error_code == "NO_SCHEDULE"
    assert value.checked_at is None and not value.rows
    assert "POWER_EXPECTED" not in repr(value)


def test_frozen_nested_wire_and_roundtrip_cannot_mutate_canonical_cache():
    value = region()
    with pytest.raises(TypeError):
        value.rows[0]["queue"] = "changed"
    with pytest.raises(TypeError):
        value.rows[0]["today"][0] = 1
    wire = value.to_wire()
    assert RegionMap.from_wire(wire).semantic_hash == value.semantic_hash
    wire["rows"][0]["today"][0] = 3
    assert value.rows[0]["today"][0] == 0


@pytest.mark.parametrize("change", [
    {"today_date": "2026-11-04"}, {"tomorrow_date": "2027-11-06"},
    {"today_status": "NewUndocumentedStatus"}, {"today": [0] * 23},
    {"today": [False] * 24}, {"today": [4] * 24}, {"today": []},
    {"today_status": "WaitingForSchedule"}, {"today_status": "NoShutdowns"},
    {"updated": "2026-11-05T12:01:00Z"}, {"today_checked_at": "2026-11-05T12:01:00Z"},
    {"updated": "2026-11-05T11:00:00"}, {"new_status_field": "ignored-danger"},
    {"queue": "x" * 65}, {"queue": "group\nprivate"},
    {"today_announcements": [{"kind": "Emergency", "state": "unknown", "scope": "local"}]},
])
def test_unknown_structural_values_or_dates_invalidate_entire_region(change):
    value = row()
    value.update(change)
    with pytest.raises(ValueError):
        region([value])


@pytest.mark.parametrize("checked", [None, "2026-11-05T10:00:00Z"])
def test_missing_or_old_planned_check_is_stale_even_when_fetch_recent(checked):
    value = row()
    value["today_checked_at"] = checked
    assert region([value]).status is SourceStatus.STALE


def test_duplicate_operator_group_rows_fail_instead_of_last_write_wins():
    with pytest.raises(ValueError, match="duplicate"):
        region([row(), row()])


def test_published_old_plan_is_preserved_but_never_replaces_emergency_status():
    value = row()
    value["today_status"] = "EmergencyShutdowns"
    value["today_published_schedule"] = {"hours": [0] * 24,
        "status": "NoShutdowns", "date_slug": "2026-11-04"}
    result = region([value])
    assert result.rows[0]["today_status"] == "EmergencyShutdowns"
    assert result.rows[0]["today_published_schedule"]["date_slug"] == "2026-11-04"
    assert result.status is SourceStatus.FRESH


def test_emergency_effective_expiry_changes_are_semantic_but_notice_order_is_not():
    value = row()
    value["today_announcements"] = [
        {"kind": "Emergency", "state": "active", "scope": "local",
         "effective_at": "2026-11-05T12:00:00Z", "expires_at": "2026-11-05T15:00:00Z"},
        {"kind": "GPV", "state": "cancelled", "scope": "supplier"},
    ]
    original = region([value]).semantic_hash
    value["today_announcements"].reverse()
    assert region([value]).semantic_hash == original
    value["today_announcements"][1]["expires_at"] = "2026-11-05T16:00:00Z"
    assert region([value]).semantic_hash != original


@pytest.mark.parametrize("change", ["slug", "katottg"])
async def test_duplicate_overview_identities_invalidate_map_cycle(change):
    area = {"slug": AREA.slug, "katottg": AREA.katottg, "name": AREA.name}
    other = {**area, "slug": "other", "katottg": "UA" + "2" * 17}
    other[change] = area[change]
    with pytest.raises(PowerFetchError):
        await api(lambda request: httpx.Response(200, json=[area, other])).overview()


@pytest.mark.parametrize("count", [0, 65])
async def test_empty_or_oversized_overview_is_not_country_fresh(count):
    area = {"slug": AREA.slug, "katottg": AREA.katottg, "name": AREA.name}
    with pytest.raises(PowerFetchError):
        await api(lambda request: httpx.Response(200, json=[area] * count)).overview()


async def test_invalid_region_error_is_safe_closed_not_partial_fresh():
    rows = [row(), {**row("other"), "today_status": "Unknown"}]
    with pytest.raises(PowerFetchError) as caught:
        await api(lambda request: httpx.Response(200, json=rows)).region(AREA)
    assert caught.value.code is PowerFetchErrorCode.INVALID_RESPONSE


@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_map_fetch_preserves_typed_http_errors_no_fallback_or_retry(status):
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(status, headers={"Retry-After": "300"}, text="private")
    with pytest.raises(PowerFetchError) as caught:
        await api(handler).region(AREA)
    assert caught.value.status_code == status and len(calls) == 1
    assert "private" not in str(caught.value)


def test_rows_and_public_wire_fields_bounded_and_unknown_cache_fields_rejected():
    with pytest.raises(ValueError):
        region([row()] * 1537)
    wire = region().to_wire()
    wire["secret"] = "not-a-real-secret"
    with pytest.raises(ValueError):
        RegionMap.from_wire(wire)


@pytest.mark.parametrize("status", ["EmergencyShutdowns", "WaitingForSchedule"])
def test_recent_check_can_confirm_collection_without_claiming_active_plan(status):
    value = row()
    value.update(today_status=status, today=[])
    result = region([value])
    assert result.status is SourceStatus.FRESH
    assert result.rows[0]["today_status"] == status and not result.rows[0]["today"]


def test_bad_other_operator_does_not_poison_healthy_cached_record():
    healthy, stale = row("healthy"), row("stale")
    stale["today_checked_at"] = "2026-11-05T10:00:00Z"
    result = region([healthy, stale])
    assert result.status is SourceStatus.STALE
    assert result.record_status("healthy", "Група А-11", NOW) is SourceStatus.FRESH
    assert result.record_status("stale", "Група А-11", NOW) is SourceStatus.STALE
    assert result.record_status("absent", "Група А-11", NOW) is SourceStatus.UNAVAILABLE
    assert result.record_checked_at("healthy", "Група А-11") == NOW - timedelta(minutes=2)


def test_record_day_freshness_does_not_infer_tomorrow_check_from_today():
    value = row()
    result = region([value])
    today = NOW.date()
    assert result.record_status("unknown-national-operator", "Група А-11", NOW,
                                day=today) is SourceStatus.FRESH
    assert result.record_status("unknown-national-operator", "Група А-11", NOW,
                                day=today + timedelta(days=1)) is SourceStatus.STALE
    assert result.record_status("unknown-national-operator", "Група А-11", NOW,
                                day=today + timedelta(days=2)) is SourceStatus.UNAVAILABLE


def test_announcement_publication_timestamp_does_not_churn_semantics():
    value = row()
    value["today_announcements"] = [{"kind": "Emergency", "state": "active",
        "scope": "local", "published_at": "2026-11-05T10:00:00Z"}]
    before = region([value]).semantic_hash
    value["today_announcements"][0]["published_at"] = "2026-11-05T11:00:00Z"
    assert region([value]).semantic_hash == before


def test_slug_database_bound_is_enforced_before_any_http():
    with pytest.raises(ValueError):
        MapArea("a" * 84, AREA.katottg, "Synthetic")
    assert MapArea("a" * 83, AREA.katottg, "Synthetic").slug == "a" * 83


def test_ignored_notice_publication_times_do_not_change_canonical_notice_order():
    value = row()
    value["today_announcements"] = [
        {"kind": "Emergency", "scope": "local", "state": "active",
         "published_at": "2026-11-05T10:00:00Z"},
        {"kind": "Emergency", "scope": "local", "state": "cancelled",
         "published_at": "2026-11-05T11:00:00Z"},
    ]
    before = region([value]).semantic_hash
    value["today_announcements"][0]["published_at"] = "2026-11-05T11:01:00Z"
    value["today_announcements"][1]["published_at"] = "2026-11-05T10:01:00Z"
    assert region([value]).semantic_hash == before
