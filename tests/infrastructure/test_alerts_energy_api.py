"""SYNTHETIC API fixtures from public v1 schema, never live grid evidence."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from time_tl.domain.power_outages.models import (
    GROUPS,
    PowerProvider,
    PowerPublicationStatus,
    PowerSourceKind,
    PowerState,
    SourceStatus,
)
from time_tl.infrastructure.power_outages.alerts_energy_api import (
    AlertsEnergyApiClient,
    AlertsEnergyApiProvider,
)
from time_tl.infrastructure.power_outages.alerts_energy_api_normalizer import (
    MAX_API_BYTES,
    normalize_api_schedules,
    parse_api_json,
    validate_supplier_registry,
)
from time_tl.infrastructure.power_outages.base import PowerFetchError, PowerFetchErrorCode

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)
KEY = SecretStr("SYNTHETIC-not-a-real-api-key")
# Identity strings from public registry; all rows/timestamps/plans are SYNTHETIC.
AREA, SUPPLIER = "dnipropetrovska-oblast", "dnipropetrovska_oblast_oblenergo"


def rows():
    return [
        {
            "initiator": SUPPLIER,
            "queue": group,
            "updated": NOW.isoformat(),
            "today": [0, 1, 2, 3] + [0] * 20,
            "tomorrow": [],
            "today_date": "2026-11-04",
            "tomorrow_date": "2026-11-05",
            "today_checked_at": NOW.isoformat(),
            "today_status": "ScheduleApplies",
            "tomorrow_status": "WaitingForSchedule",
        }
        for group in GROUPS
    ]


def normalize(value, now=NOW):
    return normalize_api_schedules(
        value, provider=PowerProvider.DTEK, area_slug=AREA, supplier_key=SUPPLIER, fetched_at=now
    )


def provider(handler, **options):
    return AlertsEnergyApiProvider(
        PowerProvider.DTEK,
        api_key=KEY,
        area_slug=AREA,
        supplier_key=SUPPLIER,
        clock=lambda: NOW,
        transport=httpx.MockTransport(handler),
        **options,
    )


def good_handler(request):
    assert request.url.scheme == "https" and request.url.host == "alerts.energy"
    assert request.headers["Authorization"] == "Bearer " + KEY.get_secret_value()
    if request.url.path.endswith("/suppliers"):
        return httpx.Response(200, json=[{"supplier_key": SUPPLIER, "area_slug": AREA}])
    if request.url.path.endswith("/check"):
        return httpx.Response(200, json={"ok": True})
    return httpx.Response(200, json=rows())


def test_documented_half_hour_slots_and_waiting_no_schedule():
    snapshot = normalize(rows())
    assert snapshot.source_kind is PowerSourceKind.ALERTS_ENERGY_API
    assert snapshot.status is SourceStatus.FRESH and len(snapshot.days) == 24
    day = snapshot.days[0]
    lower = day.intervals[0].starts_at
    expected = (
        [PowerState.POWER_EXPECTED] * 2
        + [PowerState.OUTAGE_CONFIRMED] * 3
        + [
            PowerState.POWER_EXPECTED,
            PowerState.POWER_EXPECTED,
            PowerState.OUTAGE_CONFIRMED,
        ]
    )
    for half, state in enumerate(expected):
        stamp = lower + timedelta(minutes=half * 30)
        assert (
            next(item.state for item in day.intervals if item.starts_at <= stamp < item.ends_at)
            is state
        )
    assert snapshot.days[1].publication_status is PowerPublicationStatus.NO_SCHEDULE
    assert snapshot.days[1].intervals == ()


@pytest.mark.parametrize("status", ["EmergencyShutdowns", "ScheduleApplies"])
def test_emergency_and_active_local_announcement_suppress_published_plan(status):
    value = rows()
    for row in value:
        row["today_status"] = status
        row["today_published_schedule"] = {"hours": [0] * 24, "status": "NoShutdowns"}
        if status == "ScheduleApplies":
            row["today_announcements"] = [
                {"kind": "Emergency", "state": "active", "scope": "local"}
            ]
    day = normalize(value).days[0]
    assert day.publication_status is (
        PowerPublicationStatus.EMERGENCY
        if status == "EmergencyShutdowns"
        else PowerPublicationStatus.UNKNOWN
    )
    assert len(day.intervals) == 1 and day.intervals[0].state is PowerState.UNKNOWN


def test_possible_not_documented_subdivision_becomes_unknown():
    value = rows()
    for row in value:
        row["today_possible"] = [2] + [0] * 23
    assert normalize(value).days[0].intervals[0].state is PowerState.UNKNOWN


@pytest.mark.parametrize("stamp", [None])
def test_missing_publication_timestamp_remains_conservative(stamp):
    value = rows()
    value[0]["updated"] = stamp
    result = normalize(value)
    assert result.status is SourceStatus.STALE and result.days == ()


@pytest.mark.parametrize(
    "change",
    [
        {"today": [False] * 24},
        {"today": [4] * 24},
        {"today": [0] * 23},
        {"today_status": "UnknownNewStatus"},
        {"today_date": "2026-11-03"},
        {"tomorrow_date": "2027-11-05"},
        {"updated": "2026-11-04T12:01:00Z"},
        {"updated": "2026-11-04T12:00:00"},
        {"queue": "7.1"},
        {"queue": ["1.1"]},
        {"today_status": "WaitingForSchedule"},
        {"today_status": "NoShutdowns"},
        {"today_possible": [7] * 24},
        {"today_announcements": "bad"},
        {"today_announcements": [{"kind": "Emergency", "state": "other", "scope": "local"}]},
    ],
)
def test_invalid_date_status_slots_and_group_fail_closed(change):
    value = rows()
    value[0].update(change)
    with pytest.raises(ValueError):
        normalize(value)


@pytest.mark.parametrize("alteration", ["partial", "duplicate", "missing_operator", "empty"])
def test_complete_selected_supplier_group_registry_required(alteration):
    value = rows()
    if alteration == "partial":
        value.pop()
    elif alteration == "duplicate":
        value[1] = deepcopy(value[0])
    elif alteration == "missing_operator":
        for row in value:
            row["initiator"] = "other"
    else:
        value = []
    with pytest.raises(ValueError):
        normalize(value)


@pytest.mark.parametrize("day", ["2026-03-29", "2026-10-25"])
def test_dst_day_never_guesses_24_civil_hour_mapping(day):
    value = rows()
    moment = datetime.fromisoformat(day + "T08:00:00+00:00")
    for row in value:
        row.update(
            updated=moment.isoformat(),
            today_date=day,
            today_checked_at=moment.isoformat(),
            tomorrow_date=(moment + timedelta(days=1)).date().isoformat(),
        )
    with pytest.raises(ValueError, match="dst"):
        normalize(value, moment)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"ok":true,"ok":false}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b"\xff",
        b'{"x":1}junk',
        b"[" * 1100 + b"]" * 1100,
        b"[" * 20 + b"0" + b"]" * 20,
        b"x" * (MAX_API_BYTES + 1),
    ],
    ids=["duplicate", "nan", "infinity", "utf8", "trailing", "recursion", "depth", "size"],
)
def test_strict_bounded_json(payload):
    with pytest.raises(ValueError):
        parse_api_json(payload)


async def test_disabled_default_never_constructs_request():
    def forbidden(request):
        raise AssertionError("disabled provider must not use transport")

    result = await provider(forbidden).fetch_schedules()
    assert result.status is SourceStatus.UNAVAILABLE and not result.days


async def test_enabled_fetch_verifies_supplier_and_documented_routes():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return good_handler(request)

    result = await provider(handler, enabled=True).fetch_schedules()
    assert result.status is SourceStatus.FRESH
    assert calls == [
        f"/api/v1/source-registry/areas/{AREA}/suppliers",
        f"/api/v1/source-registry/areas/{AREA}/shutdowns",
    ]


async def test_standalone_check_access_no_mapping_required():
    client = AlertsEnergyApiClient(api_key=KEY, transport=httpx.MockTransport(good_handler))
    assert await client.check_access() is True
    assert KEY.get_secret_value() not in repr(client)


@pytest.mark.parametrize("value", [{"ok": False}, {"ok": 1}, {"ok": True, "extra": 1}, []])
async def test_check_requires_exact_true(value):
    with pytest.raises(PowerFetchError) as caught:
        await provider(lambda request: httpx.Response(200, json=value)).check_access()
    assert caught.value.code is PowerFetchErrorCode.INVALID_RESPONSE


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502, 302])
async def test_http_error_safe_no_redirect_or_retry(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status,
            headers={"Location": "https://evil.invalid/", "Retry-After": "30"},
            text="PRIVATE BODY " + KEY.get_secret_value(),
        )

    with pytest.raises(PowerFetchError) as caught:
        await provider(handler).check_access()
    assert len(calls) == 1 and KEY.get_secret_value() not in str(caught.value)
    assert "PRIVATE" not in str(caught.value)
    assert caught.value.permanent is (status in {401, 403})
    if status == 429:
        assert caught.value.retry_after_seconds == 30


@pytest.mark.parametrize("retry", [None, "-1", "86401", "NaN", "PRIVATE", ""])
async def test_invalid_retry_after_preserves_quota_and_conservative_cooldown(retry):
    with pytest.raises(PowerFetchError) as caught:
        await provider(
            lambda request: httpx.Response(
                429, headers={} if retry is None else {"Retry-After": retry}
            )
        ).check_access()
    assert caught.value.code is PowerFetchErrorCode.RATE_LIMITED
    assert caught.value.status_code == 429
    assert caught.value.retry_after_seconds == 86400


@pytest.mark.parametrize(
    "failure,code",
    [
        (httpx.ReadTimeout, PowerFetchErrorCode.TIMEOUT),
        (httpx.ConnectError, PowerFetchErrorCode.CONNECTION_ERROR),
    ],
)
async def test_transport_exception_does_not_expose_request_key(failure, code):
    def handler(request):
        raise failure(KEY.get_secret_value(), request=request)

    with pytest.raises(PowerFetchError) as caught:
        await provider(handler).check_access()
    assert caught.value.code is code
    assert caught.value.__suppress_context__ is True
    assert KEY.get_secret_value() not in repr(caught.value)


@pytest.mark.parametrize(
    "headers,body",
    [
        ({"Content-Type": "text/html"}, b"<html>not JSON</html>"),
        ({"Content-Type": "application/json", "Content-Length": "9999999"}, b"{}"),
        ({"Content-Type": "application/json", "Content-Length": "-1"}, b"{}"),
        ({"Content-Type": "application/json"}, b"x" * (MAX_API_BYTES + 1)),
    ],
    ids=["html", "declared-size", "negative-size", "stream-size"],
)
async def test_http_body_size_and_content_type_bounded(headers, body):
    with pytest.raises(PowerFetchError):
        await provider(
            lambda request: httpx.Response(200, headers=headers, content=body)
        ).check_access()


@pytest.mark.parametrize(
    "registry",
    [
        [],
        [{"supplier_key": "other", "area_slug": AREA}],
        [{"supplier_key": SUPPLIER, "area_slug": "wrong"}],
        [{"supplier_key": SUPPLIER, "area_slug": AREA}] * 2,
    ],
)
def test_supplier_binding_not_inferred_from_schedule(registry):
    with pytest.raises(ValueError):
        validate_supplier_registry(registry, area_slug=AREA, supplier_key=SUPPLIER)


async def test_cancellation_propagates():
    async def handler(request):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await provider(handler).check_access()


@pytest.mark.parametrize("key", ["plaintext", SecretStr(""), SecretStr("bad\nkey")])
def test_api_key_requires_secret_wrapper_and_valid_header(key):
    with pytest.raises(ValueError):
        AlertsEnergyApiClient(api_key=key)


@pytest.mark.parametrize("area,supplier", [("../evil", SUPPLIER), (AREA, "x?key=bad")])
def test_explicit_binding_cannot_change_origin_or_path(area, supplier):
    with pytest.raises(ValueError):
        AlertsEnergyApiProvider(
            PowerProvider.DTEK, api_key=KEY, area_slug=area, supplier_key=supplier
        )


@pytest.mark.parametrize(
    "target",
    [
        PowerProvider.DTEK_KYIV,
        PowerProvider.DTEK_KYIV_REGION,
        PowerProvider.DTEK_ODESA,
        PowerProvider.DTEK_DONETSK,
        PowerProvider.CEK,
    ],
)
def test_regional_operator_identity_cannot_be_relabelled(target):
    with pytest.raises(ValueError, match="binding"):
        normalize_api_schedules(
            rows(), provider=target, area_slug=AREA, supplier_key=SUPPLIER, fetched_at=NOW
        )


async def test_cek_uses_own_supplier_without_dtek_fallback():
    supplier = "dnipropetrovska_oblast_cek"

    def handler(request):
        if request.url.path.endswith("/suppliers"):
            return httpx.Response(200, json=[{"supplier_key": supplier, "area_slug": AREA}])
        value = rows()
        for row in value:
            row["initiator"] = supplier
        return httpx.Response(200, json=value)

    result = await AlertsEnergyApiProvider(
        PowerProvider.CEK,
        api_key=KEY,
        area_slug=AREA,
        supplier_key=supplier,
        clock=lambda: NOW,
        transport=httpx.MockTransport(handler),
        enabled=True,
    ).fetch_schedules()
    assert result.provider is PowerProvider.CEK
    assert all(day.provider is PowerProvider.CEK for day in result.days)


async def test_supplier_registry_failure_stops_before_schedule_request():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=[{"supplier_key": "wrong", "area_slug": AREA}])

    with pytest.raises(PowerFetchError):
        await provider(handler, enabled=True).fetch_schedules()
    assert calls == [f"/api/v1/source-registry/areas/{AREA}/suppliers"]


async def test_retry_after_http_date_supported_without_poll_or_sleep():
    with pytest.raises(PowerFetchError) as caught:
        await provider(
            lambda request: httpx.Response(
                429,
                headers={"Retry-After": "Wed, 04 Nov 2026 12:01:00 GMT"},
            )
        ).check_access()
    assert caught.value.retry_after_seconds == 60


def test_documented_no_shutdowns_with_full_zero_plan_is_expected_not_telemetry():
    value = rows()
    for row in value:
        row["today_status"], row["today"] = "NoShutdowns", [0] * 24
    day = normalize(value).days[0]
    assert day.publication_status is PowerPublicationStatus.PLANNED
    assert len(day.intervals) == 1 and day.intervals[0].state is PowerState.POWER_EXPECTED


def test_empty_no_shutdowns_does_not_prove_expected_power():
    value = rows()
    for row in value:
        row["today_status"], row["today"] = "NoShutdowns", []
    day = normalize(value).days[0]
    assert day.publication_status is PowerPublicationStatus.NO_SCHEDULE
    assert not day.intervals


@pytest.mark.parametrize("kind", ["GPV", "UndocumentedPlanKind"])
def test_cancelled_plan_announcement_cannot_resurrect_old_published_hours(kind):
    value = rows()
    for row in value:
        row["today_announcements"] = [{"kind": kind, "state": "cancelled", "scope": "supplier"}]
        row["today_published_schedule"] = {"hours": [0] * 24, "status": "NoShutdowns"}
    day = normalize(value).days[0]
    assert day.publication_status is PowerPublicationStatus.UNKNOWN
    assert len(day.intervals) == 1 and day.intervals[0].state is PowerState.UNKNOWN


def with_notices(notices):
    value = rows()
    for row in value:
        row["today_announcements"] = deepcopy(notices)
    return value


def test_active_supplier_gpv_keeps_direct_complete_checked_plan_not_false_emergency():
    value = with_notices([{"kind": "GPV", "state": "active", "scope": "supplier"}])
    for row in value:
        row["updated"] = (NOW - timedelta(hours=20)).isoformat()
        row["today_published_schedule"] = None
    result = normalize(value)
    assert result.status is SourceStatus.FRESH
    assert result.source_updated_at == NOW - timedelta(hours=20)
    assert result.source_checked_at == NOW
    selected = next(
        day for day in result.days if day.group_id == "6.1" and day.local_date == NOW.date()
    )
    assert selected.publication_status is PowerPublicationStatus.PLANNED
    assert any(item.state is PowerState.OUTAGE_CONFIRMED for item in selected.intervals)
    assert selected.intervals[0].state is PowerState.POWER_EXPECTED


@pytest.mark.parametrize("kind", ["Emergency", "GAV", "SGAV", "NewRestriction", "gpv", "ГПВ"])
@pytest.mark.parametrize("state", ["active", "possible", "cancelled"])
def test_unmapped_notice_kind_never_guesses_emergency_or_restores_plan(kind, state):
    day = normalize(with_notices([{"kind": kind, "state": state, "scope": "supplier"}])).days[0]
    assert day.publication_status is PowerPublicationStatus.UNKNOWN
    assert len(day.intervals) == 1 and day.intervals[0].state is PowerState.UNKNOWN


@pytest.mark.parametrize(
    "state,scope",
    [
        ("active", "local"),
        ("active", "regional"),
        ("cancelled", "supplier"),
        ("possible", "supplier"),
        ("cancelled", "local"),
        ("possible", "regional"),
    ],
)
def test_gpv_exception_does_not_guess_state_or_geographic_applicability(state, scope):
    day = normalize(with_notices([{"kind": "GPV", "state": state, "scope": scope}])).days[0]
    assert day.publication_status is PowerPublicationStatus.UNKNOWN
    assert all(item.state is PowerState.UNKNOWN for item in day.intervals)


@pytest.mark.parametrize(
    "other",
    [
        {"kind": "GPV", "state": "cancelled", "scope": "supplier"},
        {"kind": "UnknownRestriction", "state": "active", "scope": "local"},
    ],
)
@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_notice_combination_blocks_in_either_order(other, reverse):
    notices = [{"kind": "GPV", "state": "active", "scope": "supplier"}, other]
    day = normalize(with_notices(list(reversed(notices)) if reverse else notices)).days[0]
    assert day.publication_status is PowerPublicationStatus.UNKNOWN
    assert all(item.state is PowerState.UNKNOWN for item in day.intervals)


def test_duplicate_identical_gpv_does_not_change_direct_verified_plan():
    notice = {"kind": "GPV", "state": "active", "scope": "supplier"}
    assert (
        normalize(with_notices([notice, notice])).days[0].publication_status
        is PowerPublicationStatus.PLANNED
    )


def test_explicit_emergency_status_wins_over_ordinary_or_unmapped_notices():
    value = with_notices(
        [
            {"kind": "GPV", "state": "active", "scope": "supplier"},
            {"kind": "UnmappedRestriction", "state": "active", "scope": "regional"},
        ]
    )
    for row in value:
        row["today_status"] = "EmergencyShutdowns"
    day = normalize(value).days[0]
    assert day.publication_status is PowerPublicationStatus.EMERGENCY
    assert all(item.state is PowerState.UNKNOWN for item in day.intervals)


@pytest.mark.parametrize(
    "effective,expires,blocked",
    [
        ("2026-11-03T00:00:00+02:00", "2026-11-04T00:00:00+02:00", False),
        ("2026-11-05T00:00:00+02:00", "2026-11-06T00:00:00+02:00", False),
        ("2026-11-04T00:00:00+02:00", "2026-11-04T10:00:00+02:00", True),
        ("2026-11-04T20:00:00+02:00", "2026-11-05T00:00:00+02:00", True),
        (None, None, True),
    ],
)
def test_notice_bounds_must_be_disjoint_to_stop_blocking_selected_day(effective, expires, blocked):
    value = with_notices(
        [
            {
                "kind": "UnmappedRestriction",
                "state": "active",
                "scope": "supplier",
                "effective_at": effective,
                "expires_at": expires,
            }
        ]
    )
    day = normalize(value).days[0]
    assert day.publication_status is (
        PowerPublicationStatus.UNKNOWN if blocked else PowerPublicationStatus.PLANNED
    )


@pytest.mark.parametrize(
    "change",
    [
        {"kind": None},
        {"state": []},
        {"scope": {}},
        {"kind": ""},
        {"kind": "GPV\n"},
        {"published_at": "2026-11-04T12:01:00Z"},
        {"effective_at": "2026-11-04T12:00:00"},
        {"expires_at": 42},
        {"effective_at": "2026-11-05T00:00:00Z", "expires_at": "2026-11-04T00:00:00Z"},
        {"undocumented_restriction": True},
    ],
)
def test_malformed_notice_does_not_admit_gpv_plan(change):
    value = with_notices([{"kind": "GPV", "state": "active", "scope": "supplier", **change}])
    with pytest.raises(ValueError):
        normalize(value)


@pytest.mark.parametrize("checked", [None, "2026-11-04T11:29:59+00:00"])
def test_gpv_needs_own_recent_checked_timestamp_not_fresh_download(checked):
    value = with_notices([{"kind": "GPV", "state": "active", "scope": "supplier"}])
    for row in value:
        row["today_checked_at"] = checked
    result = normalize(value)
    assert result.status is SourceStatus.STALE and result.days == ()


def test_one_gpv_group_without_check_cannot_borrow_other_group_checks_for_planned_intervals():
    value = with_notices([{"kind": "GPV", "state": "active", "scope": "supplier"}])
    value[0]["today_checked_at"] = None
    result = normalize(value)
    assert result.days[0].publication_status is PowerPublicationStatus.UNKNOWN
    assert all(item.state is PowerState.UNKNOWN for item in result.days[0].intervals)


def test_empty_gpv_hours_are_unknown_not_a_zero_outage_plan():
    value = with_notices([{"kind": "GPV", "state": "active", "scope": "supplier"}])
    for row in value:
        row["today"] = []
    day = normalize(value).days[0]
    assert day.publication_status is PowerPublicationStatus.UNKNOWN
    assert all(item.state is PowerState.UNKNOWN for item in day.intervals)


@pytest.mark.parametrize("hours", [[0] * 23, [False] * 24, [7] * 24])
def test_gpv_never_bypasses_incomplete_or_invalid_hour_validation(hours):
    value = with_notices([{"kind": "GPV", "state": "active", "scope": "supplier"}])
    value[0]["today"] = hours
    with pytest.raises(ValueError):
        normalize(value)


def test_gpv_future_check_timestamp_is_rejected_not_a_fresh_plan():
    value = with_notices([{"kind": "GPV", "state": "active", "scope": "supplier"}])
    value[0]["today_checked_at"] = (NOW + timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError):
        normalize(value)


def test_gpv_with_no_shutdowns_status_is_outside_narrow_observed_conjunction():
    value = with_notices([{"kind": "GPV", "state": "active", "scope": "supplier"}])
    for row in value:
        row["today_status"], row["today"] = "NoShutdowns", [0] * 24
    assert normalize(value).days[0].publication_status is PowerPublicationStatus.UNKNOWN


async def test_chunked_response_stops_at_limit_without_reading_full_stream():
    class ChunkedBody(httpx.AsyncByteStream):
        count = 0
        closed = False

        async def __aiter__(self):
            for _ in range(40):
                self.count += 1
                yield b"x" * 65536

        async def aclose(self):
            self.closed = True

    stream = ChunkedBody()
    response = httpx.Response(200, headers={"Content-Type": "application/json"}, stream=stream)
    original = response.aiter_bytes

    async def bounded_chunks(chunk_size=None):
        assert chunk_size == 65536
        async for part in original(chunk_size=chunk_size):
            yield part

    response.aiter_bytes = bounded_chunks
    with pytest.raises(PowerFetchError) as caught:
        await provider(lambda request: response).check_access()
    assert caught.value.code is PowerFetchErrorCode.INVALID_RESPONSE
    assert stream.count == 33 and stream.closed is True
