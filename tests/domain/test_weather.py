from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from time_tl.domain.weather import (
    ForecastHour,
    ForecastSnapshot,
    ForecastStatus,
    WeatherChangeDetector,
    WeatherPoint,
    next_weather_collection,
    render_weather_event,
)

NOW = datetime(2026, 11, 5, 10, tzinfo=UTC)
AT = NOW + timedelta(hours=2)


def snapshot(hours: tuple[ForecastHour, ...]) -> ForecastSnapshot:
    return ForecastSnapshot(
        "MET Norway",
        "p1",
        NOW,
        NOW,
        NOW + timedelta(hours=6),
        hours,
        ForecastStatus.FRESH,
        "https://api.met.no",
    )


def test_expected_stable_event_is_detected_without_revision() -> None:
    item = snapshot((ForecastHour(AT, precipitation_mm=2, precipitation_probability=80),))
    detector = WeatherChangeDetector()
    events = detector.detect(item, item, NOW)
    assert len(events) == 1 and events[0].kind == "UPCOMING"
    assert detector.detect(item, item, NOW, frozenset({events[0].event_key})) == ()
    text = render_weather_event(events[0], "Locality")
    assert "MET Norway" in text and "CC BY 4.0" in text
    assert "https://" in text


def test_same_interval_revision_and_explicit_cancellation() -> None:
    old = snapshot((ForecastHour(AT, wind_gust_ms=16),))
    new = snapshot((ForecastHour(AT, wind_gust_ms=21),))
    assert WeatherChangeDetector().detect(old, new, NOW)[0].kind == "CORRECTION"
    calm = snapshot((ForecastHour(AT, wind_gust_ms=4),))
    assert WeatherChangeDetector().detect(new, calm, NOW)[0].kind == "CANCELLATION"


def test_missing_parameter_never_cancels() -> None:
    old = snapshot((ForecastHour(AT, wind_gust_ms=16),))
    missing = snapshot((ForecastHour(AT),))
    assert WeatherChangeDetector().detect(old, missing, NOW) == ()


@pytest.mark.parametrize("mm,prob", [(1, None), (None, 90), (0.9, 80), (2, 69)])
def test_rain_requires_both_parameters(mm: float | None, prob: float | None) -> None:
    item = snapshot((ForecastHour(AT, precipitation_mm=mm, precipitation_probability=prob),))
    assert WeatherChangeDetector().detect(None, item, NOW) == ()


def test_thresholds_and_temperature_same_interval_history() -> None:
    item = snapshot(
        (
            ForecastHour(AT - timedelta(hours=24), temperature_c=10),
            ForecastHour(AT - timedelta(hours=1), temperature_c=1),
            ForecastHour(AT, temperature_c=-1, snowfall_cm=0.2, wind_gust_ms=15),
        )
    )
    assert {e.event_type for e in WeatherChangeDetector().detect(None, item, NOW)} == {
        "TEMPERATURE",
        "FROST",
        "SNOW",
        "WIND",
    }


@pytest.mark.parametrize(
    "status", [ForecastStatus.STALE, ForecastStatus.PARTIAL, ForecastStatus.UNKNOWN]
)
def test_nonfresh_is_not_absence_or_cancellation(status: ForecastStatus) -> None:
    old = snapshot((ForecastHour(AT, wind_gust_ms=20),))
    assert WeatherChangeDetector().detect(old, replace(old, status=status), NOW) == ()


def test_expired_or_different_source_cannot_drive_events() -> None:
    item = snapshot((ForecastHour(AT, wind_gust_ms=20),))
    assert WeatherChangeDetector().detect(None, replace(item, expires_at=NOW), NOW) == ()
    assert WeatherChangeDetector().detect(replace(item, provider="other"), item, NOW) == ()


@pytest.mark.parametrize("raw", [float("nan"), float("inf"), -1, True])
def test_invalid_values(raw: float) -> None:
    with pytest.raises(ValueError):
        ForecastHour(AT, precipitation_mm=raw)


def test_immutable_and_hash_dedup() -> None:
    item = snapshot((ForecastHour(AT, temperature_c=4),))
    assert replace(item, fetched_at=NOW + timedelta(minutes=1)).forecast_hash == item.forecast_hash
    with pytest.raises(ValueError):
        snapshot((ForecastHour(AT), ForecastHour(AT)))
    with pytest.raises(ValueError):
        WeatherPoint("p", float("nan"), 20)


@pytest.mark.parametrize(
    "date", [datetime(2026, 3, 29, 0, tzinfo=UTC), datetime(2026, 10, 25, 0, tzinfo=UTC)]
)
@pytest.mark.parametrize("frequency", [4, 6])
def test_dst_and_restart_next_slot_never_catchup(date: datetime, frequency: int) -> None:
    nxt = next_weather_collection(date, frequency)
    assert date < nxt <= date + timedelta(hours=6)
    assert next_weather_collection(nxt, frequency) > nxt


def test_correction_then_stable_and_small_drift_do_not_repeat_upcoming() -> None:
    detector = WeatherChangeDetector()
    first = snapshot((ForecastHour(AT, wind_gust_ms=16),))
    event = detector.detect(None, first, NOW)[0]
    known = {event.event_key}
    corrected = replace(
        snapshot((ForecastHour(AT, wind_gust_ms=20),)),
        provider_updated_at=NOW + timedelta(minutes=1),
    )
    correction = detector.detect(first, corrected, NOW + timedelta(minutes=1), frozenset(known))[0]
    assert correction.kind == "CORRECTION"
    known.add(correction.event_key)
    assert detector.detect(corrected, corrected, NOW + timedelta(minutes=1), frozenset(known)) == ()
    drift = replace(corrected, hours=(ForecastHour(AT, wind_gust_ms=20.1),))
    assert detector.detect(corrected, drift, NOW + timedelta(minutes=1), frozenset(known)) == ()
    # Even a correction first observed after restart carries the stable identity.
    assert (
        detector.detect(
            corrected, drift, NOW + timedelta(minutes=1), frozenset({correction.event_key})
        )
        == ()
    )


def test_cancel_and_reappearance_get_new_revision_without_stale_cancellation() -> None:
    detector = WeatherChangeDetector()
    active = snapshot((ForecastHour(AT, wind_gust_ms=20),))
    initial = detector.detect(None, active, NOW)[0]
    calm = replace(
        snapshot((ForecastHour(AT, wind_gust_ms=3),)),
        provider_updated_at=NOW + timedelta(minutes=1),
    )
    cancel = detector.detect(active, calm, NOW + timedelta(minutes=1))[0]
    back = replace(active, provider_updated_at=NOW + timedelta(minutes=2))
    reappearance = detector.detect(
        calm, back, NOW + timedelta(minutes=2), frozenset({initial.event_key, cancel.event_key})
    )[0]
    assert reappearance.kind == "CORRECTION"
    assert reappearance.event_key not in {initial.event_key, cancel.event_key}
    assert detector.detect(active, replace(calm, status=ForecastStatus.PARTIAL), NOW) == ()
