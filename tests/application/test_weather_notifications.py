from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta

import pytest

from time_tl.application.weather_notifications import (
    WeatherNotificationPlanner,
    WeatherNotificationPreferences,
    daily_summary,
    event_supported,
    render_weather_notification,
    validate_weather_notification_payload,
)
from time_tl.domain.weather import (
    ForecastHour,
    ForecastSnapshot,
    ForecastStatus,
    WeatherChangeDetector,
    WeatherThresholds,
)

NOW = datetime(2026, 11, 5, 10, tzinfo=UTC)
AT = NOW + timedelta(hours=2)
USER = "10000000-0000-0000-0000-000000000001"
POINT = "20000000-0000-0000-0000-000000000001"
PREFS = WeatherNotificationPreferences(weather_enabled=True, forecast_requested=True, revision=1)


def snap(gust: float = 20) -> ForecastSnapshot:
    return ForecastSnapshot(
        "MET Norway",
        POINT,
        NOW,
        NOW,
        NOW + timedelta(hours=8),
        (
            ForecastHour(
                AT,
                temperature_c=5,
                wind_gust_ms=gust,
                precipitation_mm=2,
                precipitation_probability=80,
            ),
        ),
        ForecastStatus.FRESH,
        "source",
    )


def plan(
    snapshot: ForecastSnapshot | None = None,
    *,
    prefs: WeatherNotificationPreferences = PREFS,
    now: datetime = NOW,
    known: frozenset[str] = frozenset(),
    delivered: frozenset[str] = frozenset(),
    events: tuple | None = None,
) -> tuple:
    snapshot = snapshot or snap()
    events = WeatherChangeDetector().detect(None, snapshot, now) if events is None else events
    return WeatherNotificationPlanner().plan(
        user_id=USER,
        region_id="123",
        locality="Locality",
        snapshot=snapshot,
        events=events,
        preferences=prefs,
        delivered_event_keys=delivered,
        known_notification_keys=known,
        now=now,
    )


def test_due_overlap_merges_rain_and_wind_into_one_deterministic_message() -> None:
    items = plan()
    assert len(items) == 1 and len(items[0].event_keys) == 2
    text = render_weather_notification(items[0].payload)
    assert "MET Norway" in text and "CC BY 4.0" in text and len(text) < 4096
    assert USER not in text and POINT not in text
    assert plan(known=frozenset({items[0].key})) == ()


def test_outside_due_window_no_catchup_or_early_send() -> None:
    assert plan(now=NOW - timedelta(minutes=1)) == ()
    assert plan(now=NOW + timedelta(minutes=10)) == ()


@pytest.mark.parametrize(
    "status", [ForecastStatus.STALE, ForecastStatus.PARTIAL, ForecastStatus.UNKNOWN]
)
def test_missing_or_stale_no_new_notifications(status: ForecastStatus) -> None:
    assert plan(replace(snap(), status=status)) == ()


def test_master_and_category_switches_independent() -> None:
    assert plan(prefs=replace(PREFS, weather_enabled=False)) == ()
    assert plan(prefs=replace(PREFS, rain_alert_enabled=False, wind_alert_enabled=False)) == ()
    assert len(plan(prefs=replace(PREFS, rain_alert_enabled=False))[0].event_keys) == 1


def test_correction_requires_previously_delivered_identity() -> None:
    old, new = snap(16), snap(20)
    initial = WeatherChangeDetector().detect(None, old, NOW)
    corrections = WeatherChangeDetector().detect(old, new, NOW)
    corrections = tuple(e for e in corrections if e.kind == "CORRECTION")
    initial_plan = plan(new, events=corrections)
    assert len(initial_plan) == 1
    assert initial_plan[0].payload["events"][0]["initial"] is True
    assert (
        len(plan(new, events=corrections, delivered=frozenset(e.event_key for e in initial))) == 1
    )


def test_cancellation_only_previously_delivered_and_still_supported() -> None:
    old, new = snap(20), snap(4)
    initial = WeatherChangeDetector().detect(None, old, NOW)
    cancellations = tuple(
        e for e in WeatherChangeDetector().detect(old, new, NOW) if e.kind == "CANCELLATION"
    )
    assert plan(new, events=cancellations) == ()
    assert (
        len(plan(new, events=cancellations, delivered=frozenset(e.event_key for e in initial))) == 1
    )
    assert not event_supported(snap(25), cancellations[0], WeatherThresholds(), NOW)


def test_event_dispatch_tolerates_unrelated_future_hash_update_only() -> None:
    original = snap()
    event = WeatherChangeDetector().detect(None, original, NOW)[0]
    unrelated = replace(
        original, hours=original.hours + (ForecastHour(AT + timedelta(hours=8), temperature_c=10),)
    )
    assert original.forecast_hash != unrelated.forecast_hash
    assert event_supported(unrelated, event, WeatherThresholds(), NOW)
    changed = replace(original, hours=(ForecastHour(AT, temperature_c=5, wind_gust_ms=2),))
    assert not event_supported(changed, event, WeatherThresholds(), NOW)


def test_daily_tomorrow_window_and_restart_dedupe() -> None:
    now = datetime(2026, 11, 5, 5, tzinfo=UTC)  # 07:00 Kyiv
    tomorrow = now + timedelta(days=1)
    snapshot = replace(
        snap(),
        fetched_at=now,
        provider_updated_at=now,
        expires_at=now + timedelta(hours=8),
        hours=(
            ForecastHour(tomorrow, temperature_c=4),
            ForecastHour(tomorrow + timedelta(hours=1), temperature_c=7),
        ),
    )
    prefs = replace(PREFS, daily_forecast_enabled=True, daily_forecast_time=time(7))
    items = plan(snapshot, prefs=prefs, now=now)
    assert len(items) == 1 and items[0].kind == "WEATHER_DAILY"
    assert items[0].payload["summary"]["forecast_date"] == "2026-11-06"
    assert plan(snapshot, prefs=prefs, now=now, known=frozenset({items[0].key})) == ()
    assert plan(snapshot, prefs=prefs, now=now + timedelta(minutes=10)) == ()


@pytest.mark.parametrize(
    "mutation",
    ["extra", "bad_uuid", "bad_hash", "bad_event", "bad_keys", "nan", "naive", "wrong_kind"],
)
def test_payload_strict_rejects_malformed_or_unknown(mutation: str) -> None:
    payload = deepcopy(plan()[0].payload)
    if mutation == "extra":
        payload["token"] = "forbidden"
    elif mutation == "bad_uuid":
        payload["user_id"] = "notuuid"
    elif mutation == "bad_hash":
        payload["snapshot_hash"] = "bad"
    elif mutation == "bad_event":
        payload["events"][0]["unknown"] = True
    elif mutation == "bad_keys":
        payload["event_keys"] = [{}]
    elif mutation == "nan":
        payload["events"][0]["current_value"] = float("nan")
    elif mutation == "naive":
        payload["expires_at"] = "2026-11-05T12:00:00"
    else:
        payload["kind"] = "OFFICIAL_WARNING"
    with pytest.raises(ValueError):
        validate_weather_notification_payload(payload)


def test_correction_before_first_delivery_remains_initial_due_after_restart() -> None:
    old, new = snap(16), snap(20)
    corrections = tuple(
        e for e in WeatherChangeDetector().detect(old, new, NOW) if e.kind == "CORRECTION"
    )
    assert plan(new, events=corrections, now=NOW - timedelta(minutes=1)) == ()
    prefs = replace(PREFS, weather_changes_enabled=False)
    planned = plan(new, prefs=prefs, events=corrections)
    assert len(planned) == 1
    item = planned[0].payload["events"][0]
    assert item["kind"] == "CORRECTION" and item["initial"] is True
    assert item["event_key"] == corrections[0].event_key
    assert plan(new, prefs=prefs, events=corrections, known=frozenset({planned[0].key})) == ()


def test_subthreshold_active_drift_keeps_pending_claim_but_large_change_requires_revision() -> None:
    snapshot = snap(15)
    event = next(
        e for e in WeatherChangeDetector().detect(None, snapshot, NOW) if e.event_type == "WIND"
    )
    changed = snap(15.2)
    assert event_supported(changed, event, WeatherThresholds(), NOW)
    assert len(plan(changed, events=(event,))) == 1
    assert not event_supported(snap(16), event, WeatherThresholds(), NOW)
    assert not event_supported(snap(14.9), event, WeatherThresholds(), NOW)


def test_configuration_epoch_is_explicit_and_strict() -> None:
    payload = deepcopy(plan()[0].payload)
    assert payload["configuration_revision"] == 0
    payload["configuration_revision"] = -1
    with pytest.raises(ValueError):
        validate_weather_notification_payload(payload)


def test_immutable_keys_bind_preference_and_configuration_generations() -> None:
    first = plan()[0]
    changed_pref = plan(prefs=replace(PREFS, revision=2))[0]
    assert first.key != changed_pref.key
    snapshot = snap()
    events = WeatherChangeDetector().detect(None, snapshot, NOW)
    changed_config = WeatherNotificationPlanner().plan(
        user_id=USER,
        region_id="123",
        locality="Locality",
        snapshot=snapshot,
        events=events,
        preferences=PREFS,
        delivered_event_keys=frozenset(),
        known_notification_keys=frozenset(),
        now=NOW,
        configuration_revision=2,
    )[0]
    assert first.key != changed_config.key
    assert plan(events=events, delivered=frozenset(e.event_key for e in events)) == ()


def test_daily_relevant_date_summary_ignores_day_plus_two_but_changes_for_selected_day() -> None:
    now = datetime(2026, 11, 5, 5, tzinfo=UTC)
    tomorrow = now + timedelta(days=1)
    snapshot = replace(
        snap(),
        fetched_at=now,
        provider_updated_at=now,
        expires_at=now + timedelta(hours=8),
        hours=(
            ForecastHour(tomorrow, temperature_c=4),
            ForecastHour(tomorrow + timedelta(hours=1), temperature_c=7),
        ),
    )
    prefs = replace(PREFS, daily_forecast_enabled=True)
    first = plan(snapshot, prefs=prefs, now=now)[0]
    unrelated = replace(
        snapshot, hours=snapshot.hours + (ForecastHour(now + timedelta(days=2), temperature_c=25),)
    )
    assert snapshot.forecast_hash != unrelated.forecast_hash
    assert daily_summary(snapshot, "2026-11-06") == daily_summary(unrelated, "2026-11-06")
    assert plan(unrelated, prefs=prefs, now=now)[0].key == first.key
    changed = replace(
        snapshot,
        hours=(
            ForecastHour(tomorrow, temperature_c=1),
            ForecastHour(tomorrow + timedelta(hours=1), temperature_c=7),
        ),
    )
    assert daily_summary(snapshot, "2026-11-06") != daily_summary(changed, "2026-11-06")
    assert plan(changed, prefs=prefs, now=now)[0].key != first.key
    assert (
        WeatherNotificationPlanner().plan(
            user_id=USER,
            region_id="123",
            locality="Locality",
            snapshot=changed,
            events=(),
            preferences=replace(prefs, revision=2),
            delivered_event_keys=frozenset(),
            known_notification_keys=frozenset(),
            now=now,
            delivered_forecast_dates=frozenset({"2026-11-06"}),
        )
        == ()
    )
