"""Process/DB liveness does not attest forecast freshness, coverage or accuracy."""

import json
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from time_tl.runtime import weather_health as module

NOW = datetime(2026, 11, 5, 6, tzinfo=UTC)
INSTANCE = str(UUID(int=1))


def marker(**changes):
    return {
        "version": 1,
        "role": "weather-worker",
        "instance_id": INSTANCE,
        "heartbeat_at": NOW.isoformat(),
        **changes,
    }


@pytest.mark.parametrize("role", sorted(module.ROLES))
def test_marker_roundtrip_exact_process_instance_without_weather_data(tmp_path, role):
    module.write_weather_marker(role, INSTANCE, NOW, directory=tmp_path)
    value = module.read_weather_marker(role, directory=tmp_path)
    assert value == marker(role=role)
    assert set(value) == {"version", "role", "instance_id", "heartbeat_at"}
    assert len(list(tmp_path.iterdir())) == 1
    assert not any(
        word in json.dumps(value) for word in ("forecast", "latitude", "longitude", "token")
    )


@pytest.mark.parametrize(
    "seconds,current", [(-6, False), (-5, True), (0, True), (89, True), (90, False), (1000, False)]
)
def test_exact_liveness_ttl_and_future_clock_skew(seconds, current):
    assert (
        module.health_is_current(
            marker(),
            instance=INSTANCE,
            heartbeat_at=NOW,
            status="WAITING",
            now=NOW + timedelta(seconds=seconds),
        )
        is current
    )


@pytest.mark.parametrize("status", ["PASS", "WAITING", "DISABLED", "PARTIAL"])
def test_live_disabled_or_partial_worker_is_liveness_only(status):
    # Deliberately no forecast/cache/electrical/all-clear proof supplied.
    assert module.health_is_current(
        marker(), instance=INSTANCE, heartbeat_at=NOW, status=status, now=NOW
    )


@pytest.mark.parametrize("status", ["FAILED", "UNKNOWN", "STALE", "RUNNING", ""])
def test_unrecognized_or_failed_worker_status_is_not_current(status):
    assert not module.health_is_current(
        marker(), instance=INSTANCE, heartbeat_at=NOW, status=status, now=NOW
    )


def test_wrong_process_old_db_heartbeat_or_naive_clock_rejects():
    assert not module.health_is_current(
        marker(), instance=str(UUID(int=2)), heartbeat_at=NOW, status="PASS", now=NOW
    )
    assert not module.health_is_current(
        marker(), instance=INSTANCE, heartbeat_at=NOW - timedelta(seconds=1), status="PASS", now=NOW
    )
    assert not module.health_is_current(
        marker(), instance=INSTANCE, heartbeat_at=NOW, status="PASS", now=NOW.replace(tzinfo=None)
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"version": True},
        {"version": 2},
        {"extra": True},
        {"role": "bot"},
        {"instance_id": "invalid"},
        {"heartbeat_at": 123},
    ],
)
def test_marker_schema_is_strict_and_bounded(tmp_path, changes):
    path = module._path("weather-worker", tmp_path)
    path.write_text(json.dumps(marker(**changes)))
    with pytest.raises((ValueError, TypeError)):
        module.read_weather_marker("weather-worker", directory=tmp_path)
    path.write_text(" " * 1025)
    with pytest.raises(ValueError):
        module.read_weather_marker("weather-worker", directory=tmp_path)


def test_duplicate_marker_fields_fail_closed(tmp_path):
    raw = json.dumps(marker())[:-1] + ',"version":1}'
    module._path("weather-worker", tmp_path).write_text(raw)
    with pytest.raises(ValueError):
        module.read_weather_marker("weather-worker", directory=tmp_path)


@pytest.mark.parametrize("role", ["bot", "../production", "weather-worker/../../x", ""])
def test_unrecognized_role_never_creates_file(tmp_path, role):
    with pytest.raises(ValueError):
        module.write_weather_marker(role, INSTANCE, NOW, directory=tmp_path)
    assert not list(tmp_path.iterdir())


def test_invalid_instance_and_naive_time_do_not_create_marker(tmp_path):
    for instance, now in (("invalid", NOW), (INSTANCE, NOW.replace(tzinfo=None))):
        with pytest.raises(ValueError):
            module.write_weather_marker("weather-worker", instance, now, directory=tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="native Linux nofollow boundary")
def test_symlink_marker_is_not_followed(tmp_path):
    target = tmp_path / "untrusted.json"
    target.write_text(json.dumps(marker()))
    module._path("weather-worker", tmp_path).symlink_to(target)
    with pytest.raises(OSError):
        module.read_weather_marker("weather-worker", directory=tmp_path)
