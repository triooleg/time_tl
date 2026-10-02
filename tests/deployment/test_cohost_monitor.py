"""Local-only colocated monitor fault injection; no production contacts."""

import copy
import io
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts.deployment import cohost_monitor as monitor
from scripts.deployment.verify_production_monitoring import VerificationError


@pytest.fixture
def config(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(monitor, "TRUSTED_STATE", tmp_path)
    return {
        "state_dir": str(tmp_path),
        "revision": 14,
        "image": (
            "localhost:5000/time-tl-bot@sha256:"
            "060e2ec4ce1e72cfe4984b6b0e790b14f93ccf82b1498958dd5d334a7c2ae391"
        ),
        "baseline": {"container_ids": dict.fromkeys(monitor.ALL_ROLES, "original")},
        "threshold_environment": {},
        "backup_verified_at": "2026-10-02T14:50:07+00:00",
        "tls_hostname": "example.test",
        "lib_dir": "/public/lib",
    }


def pulse(config, now):
    return dict(
        schema_version=1,
        mode="COLOCATED",
        stack=monitor.STACK,
        revision=14,
        image=config["image"],
        observed_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=120)).isoformat(),
        status="PASS",
    )


@pytest.mark.parametrize(
    "change",
    [
        {"mode": "OFFHOST"},
        {"status": "HOLD"},
        {"revision": True},
        {"revision": 13},
        {"image": "other"},
        {"schema_version": True},
        {"extra": 1},
    ],
)
def test_pulse_rejects_identity(config, change):
    now = datetime.now(UTC)
    value = pulse(config, now)
    value.update(change)
    with pytest.raises(ValueError):
        monitor.verify_pulse(value, config, now)


@pytest.mark.parametrize("age", [-1, 120, 121])
def test_pulse_future_expired(config, age):
    now = datetime.now(UTC)
    with pytest.raises(ValueError):
        monitor.verify_pulse(pulse(config, now - timedelta(seconds=age)), config, now)


def test_pulse_pass(config):
    now = datetime.now(UTC)
    monitor.verify_pulse(pulse(config, now), config, now)


def test_sender_first_continue_stop_on_timeout(monkeypatch):
    attempts = []

    def fake(args, **kwargs):
        attempts.append(args[-1])
        if len(attempts) == 1:
            raise TimeoutError
        return ""

    monkeypatch.setattr(monitor, "run", fake)
    stopped = monitor.stop_roles()
    assert attempts == [monitor.STACK + "-" + r + "-1" for r in monitor.ROLES]
    assert stopped == list(monitor.ROLES[1:])


@pytest.mark.parametrize("mode", ["once", "watchdog", "initialize"])
def test_collect_failure_hold_no_restart(config, monkeypatch, mode):
    monkeypatch.setattr(monitor, "validate_config", lambda c: None)
    monkeypatch.setattr(monitor, "collect", Mock(side_effect=TimeoutError))
    stop = Mock(return_value=[])
    notify = Mock()
    monkeypatch.setattr(monitor, "stop_roles", stop)
    monkeypatch.setattr(monitor, "notify", notify)
    assert monitor.execute(config, mode) == 2
    assert json.loads((Path(config["state_dir"]) / "hold.json").read_text())["status"] == "HOLD"
    stop.assert_called_once()
    notify.assert_called_once()


def test_shadow_failure_is_readonly(config, monkeypatch):
    monkeypatch.setattr(monitor, "validate_config", lambda c: None)
    monkeypatch.setattr(monitor, "collect", Mock(side_effect=ValueError))
    stop = Mock()
    monkeypatch.setattr(monitor, "stop_roles", stop)
    assert monitor.execute(config, "shadow") == 2
    stop.assert_not_called()
    assert not list(Path(config["state_dir"]).iterdir())


def test_io_failure_still_stops(config, monkeypatch):
    monkeypatch.setattr(monitor, "atomic_write", Mock(side_effect=OSError))
    stop = Mock(return_value=[])
    monkeypatch.setattr(monitor, "stop_roles", stop)
    monkeypatch.setattr(monitor, "notify", Mock(side_effect=OSError))
    assert monitor.hold(Path(config["state_dir"])) == 2
    stop.assert_called_once()


def test_hold_does_not_collect_or_recover(config, monkeypatch):
    state = Path(config["state_dir"])
    (state / "hold.json").touch()
    monkeypatch.setattr(monitor, "validate_config", lambda c: None)
    collect = Mock()
    monkeypatch.setattr(monitor, "collect", collect)
    monkeypatch.setattr(monitor, "stop_roles", Mock(return_value=[]))
    monkeypatch.setattr(monitor, "notify", Mock())
    assert monitor.execute(config, "once") == 2
    collect.assert_not_called()
    assert (state / "hold.json").exists()


def test_notify_claim_prevents_repeat(config, monkeypatch):
    monkeypatch.setattr(os, "O_NOFOLLOW", 0, raising=False)
    state = Path(config["state_dir"])
    (state / "notify.claim").touch()
    run = Mock()
    monkeypatch.setattr(monitor, "run", run)
    with pytest.raises(FileExistsError):
        monitor.notify(state)
    run.assert_not_called()


def test_evaluator_retains_baseline(config, monkeypatch):
    run = Mock(return_value="PASS\n")
    monkeypatch.setattr(monitor, "run", run)
    sample = {"some": "aggregate"}
    monitor.evaluate(config, sample)
    packet = json.loads(run.call_args.kwargs["payload"])
    assert packet["baseline"] == config["baseline"]
    assert "--network" in run.call_args.args[0]
    assert "none" in run.call_args.args[0]


@pytest.mark.skipif(os.name == "nt", reason="Linux protected state semantics")
def test_state_atomic_private_and_symlink_rejected(tmp_path):
    path = tmp_path / "state.json"
    monitor.atomic_write(path, {"status": "PASS"})
    assert path.stat().st_mode & 0o777 == 0o600
    assert monitor.protected_read(path) == {"status": "PASS"}
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        monitor.protected_read(link)


def test_duplicate_json_rejected():
    with pytest.raises(ValueError):
        json.loads('{"status":"PASS","status":"HOLD"}', object_pairs_hook=monitor.unique)


@pytest.mark.parametrize("value", [[], {}, None, {"wrong": "schema"}])
@pytest.mark.parametrize("mode", ["once", "watchdog"])
def test_malformed_config_stops(config, monkeypatch, value, mode):
    stop = Mock(return_value=[])
    monkeypatch.setattr(monitor, "stop_roles", stop)
    monkeypatch.setattr(monitor, "notify", Mock())
    assert monitor.execute(value, mode) == 2
    stop.assert_called_once()
    assert (Path(config["state_dir"]) / "hold.json").exists()


def test_route_test_does_not_stop_or_pulse(config, monkeypatch):
    monkeypatch.setattr(monitor, "validate_config", lambda c: None)
    notify = Mock()
    stop = Mock()
    monkeypatch.setattr(monitor, "notify", notify)
    monkeypatch.setattr(monitor, "stop_roles", stop)
    assert monitor.execute(config, "route-test") == 0
    notify.assert_called_once_with(Path(config["state_dir"]), route_test=True)
    stop.assert_not_called()
    assert not (Path(config["state_dir"]) / "pulse.json").exists()


def test_unreadable_config_main_stops(config, monkeypatch):
    monkeypatch.setattr("sys.argv", ["monitor", "--config", "missing", "watchdog"])
    monkeypatch.setattr(monitor, "protected_read", Mock(side_effect=OSError))
    stop = Mock(return_value=[])
    monkeypatch.setattr(monitor, "stop_roles", stop)
    monkeypatch.setattr(monitor, "notify", Mock())
    assert monitor.main() == 2
    stop.assert_called_once()


def evaluation_packet(config):
    from scripts.deployment.verify_production_monitoring import INTEGER_RULES

    environment = {name: str(bounds[1]) for name, bounds in INTEGER_RULES.items()}
    environment["ALERT_STALE_AFTER_SECONDS"] = "120"
    environment["MONITOR_NEW_RETRY_HOLD_COUNT"] = "1"
    base = dict(
        restarts=dict.fromkeys(monitor.ALL_ROLES, 0),
        container_ids=dict.fromkeys(monitor.ALL_ROLES, "original"),
        outbox_dead_total=37,
        outbox_retry_total=0,
    )
    sample = {
        **copy.deepcopy(base),
        "images": dict.fromkeys(monitor.ALL_ROLES, config["image"]),
        "observed_at": datetime.now(UTC).isoformat(),
        "provider_circuit_healthy": True,
        "provider_freshness_seconds": 1,
        "heartbeats": {r: {"age_seconds": 1, "status": "healthy"} for r in monitor.ROLES},
        "outbox_open_count": 0,
        "outbox_oldest_open_age_seconds": 0,
        "database_healthy": True,
        "disk_free_percent": 70,
        "memory_available_percent": 80,
        "verified_backup_age_seconds": 1,
        "tls_remaining_days": 90,
    }
    return dict(
        sample=sample, baseline=base, environment=environment,
        image=config["image"], role_images=monitor.expected_role_images(config),
    )


def test_admin_draft_pin_fails_closed(config, monkeypatch):
    monkeypatch.setattr(monitor, "RC25_ADMIN_IMAGE", None)
    config["admin_image"] = "localhost:5000/time-tl-bot@sha256:" + "a" * 64
    with pytest.raises(ValueError, match="Admin image"):
        monitor.expected_admin_image(config)


def test_compiled_release_pin_is_exact(config):
    pin = (
        "localhost:5000/time-tl-bot@sha256:"
        "2edc2a0347b565ba49834d87a289b62f0b9f83c559c61db49af67301df55c9b0"
    )
    assert monitor.RC25_ADMIN_IMAGE == pin
    config["admin_image"] = pin
    assert monitor.expected_admin_image(config) == pin
    assert monitor.expected_role_images(config) == {
        **dict.fromkeys(monitor.ROLES, config["image"]), "admin-web": pin,
    }
    config["admin_image"] = pin[:-1] + "1"
    with pytest.raises(ValueError, match="Admin image identity"):
        monitor.expected_admin_image(config)


@pytest.mark.parametrize("fault", ["healthy", "admin", *monitor.ROLES, "missing"])
def test_admin_only_evaluator_identity(config, monkeypatch, fault):
    pin = "localhost:5000/time-tl-bot@sha256:" + "a" * 64
    monkeypatch.setattr(monitor, "RC25_ADMIN_IMAGE", pin)
    config["admin_image"] = pin
    packet = evaluation_packet(config)
    packet["sample"]["images"]["admin-web"] = pin
    if fault == "admin":
        packet["sample"]["images"]["admin-web"] = config["image"]
    elif fault == "missing":
        del packet["sample"]["images"]["admin-web"]
    elif fault in monitor.ROLES:
        packet["sample"]["images"][fault] = pin
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(packet)))
    if fault == "healthy":
        exec(monitor.EVALUATOR, {})
    else:
        with pytest.raises(ValueError, match="Image drift"):
            exec(monitor.EVALUATOR, {})


def test_admin_pulse_binds_new_pin(config, monkeypatch):
    pin = "localhost:5000/time-tl-bot@sha256:" + "a" * 64
    monkeypatch.setattr(monitor, "RC25_ADMIN_IMAGE", pin)
    now = datetime.now(UTC)
    old = pulse(config, now)
    config["admin_image"] = pin
    with pytest.raises(ValueError, match="Pulse schema"):
        monitor.verify_pulse(old, config, now)
    old["admin_image"] = "wrong"
    with pytest.raises(ValueError, match="Pulse admin"):
        monitor.verify_pulse(old, config, now)
    old["admin_image"] = pin
    monitor.verify_pulse(old, config, now)


@pytest.mark.parametrize(
    "fault", ["healthy", "wrong_pin", "missing_id", "bad_id", "missing_restarts"]
)
def test_admin_config_requires_complete_baseline(config, monkeypatch, fault):
    import stat
    from types import SimpleNamespace

    pin = "localhost:5000/time-tl-bot@sha256:" + "a" * 64
    monkeypatch.setattr(monitor, "RC25_ADMIN_IMAGE", pin)
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(Path, "lstat", lambda path: SimpleNamespace(
        st_mode=stat.S_IFDIR | 0o700, st_uid=1000,
    ))
    config["lib_dir"] = "/home/trio/time-tl-monitor-rc24/lib"
    config["admin_image"] = pin
    config["baseline"]["container_ids"]["admin-web"] = "b" * 64
    config["baseline"]["restarts"] = dict.fromkeys(monitor.ALL_ROLES, 0)
    if fault == "wrong_pin":
        config["admin_image"] = config["image"]
    elif fault == "missing_id":
        del config["baseline"]["container_ids"]["admin-web"]
    elif fault == "bad_id":
        config["baseline"]["container_ids"]["admin-web"] = "draft"
    elif fault == "missing_restarts":
        del config["baseline"]["restarts"]["admin-web"]
    if fault == "healthy":
        monitor.validate_config(config)
        del config["admin_image"]
        monitor.validate_config(config)  # RC24 remains supported unchanged.
    else:
        with pytest.raises(ValueError):
            monitor.validate_config(config)


@pytest.mark.parametrize(
    "fault", ["healthy", "stale", "elapsed", "circuit", "image", "replacement", "reset", "retry"]
)
def test_real_evaluator_local(config, monkeypatch, fault):
    packet = evaluation_packet(config)
    sample = packet["sample"]
    if fault == "stale":
        sample["observed_at"] = (datetime.now(UTC) - timedelta(seconds=61)).isoformat()
    if fault == "elapsed":
        sample["observed_at"] = (datetime.now(UTC) - timedelta(seconds=50)).isoformat()
        sample["provider_freshness_seconds"] = 100
    if fault == "circuit":
        sample["provider_circuit_healthy"] = False
    if fault == "image":
        sample["images"]["bot"] = "other"
    if fault == "replacement":
        sample["container_ids"]["bot"] = "other"
    if fault == "reset":
        sample["outbox_dead_total"] = 36
    if fault == "retry":
        sample["outbox_retry_total"] = 1
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(packet)))
    # Existing interpreter imports official local public evaluator; never shell/network.
    if fault == "healthy":
        exec(monitor.EVALUATOR, {})
    else:
        with pytest.raises((ValueError, VerificationError)):
            exec(monitor.EVALUATOR, {})


@pytest.mark.parametrize(
    "fault", ["empty", "partial", "active", "applied", "health", "mount", "missing"]
)
def test_collector_faults_before_network(config, monkeypatch, fault):
    data = {
        "active_revision": 14,
        "state_active_revision": 14,
        "heartbeats": {r: {"applied_revision": 14} for r in monitor.ROLES},
    }
    inspections = [
        {
            "State": {"Running": True, "Health": {"Status": "healthy"}},
            "Mounts": [{"Destination": "/run/time-tl-release", "RW": False}],
            "RestartCount": 0,
            "Id": "original",
            "Config": {"Image": config["image"]},
        }
        for _ in monitor.ALL_ROLES
    ]
    if fault == "empty":
        data = None
    if fault == "partial":
        data = {}
    if fault == "active":
        data["active_revision"] = 13
    if fault == "applied":
        data["heartbeats"]["bot"]["applied_revision"] = 13
    if fault == "health":
        inspections[0]["State"]["Health"]["Status"] = "unhealthy"
    if fault == "mount":
        inspections[0]["Mounts"][0]["RW"] = True
    if fault == "missing":
        inspections.pop()
    monkeypatch.setattr(
        monitor, "run", Mock(side_effect=[json.dumps(data), json.dumps(inspections)])
    )
    connection = Mock(side_effect=AssertionError("No network allowed"))
    monkeypatch.setattr(monitor.socket, "create_connection", connection)
    with pytest.raises((ValueError, TypeError, KeyError, AttributeError)):
        monitor.collect(config)
    connection.assert_not_called()


def test_collector_local_tls_preserves_hostname_validation(config, monkeypatch):
    from types import SimpleNamespace

    data = {
        "active_revision": 14,
        "state_active_revision": 14,
        "heartbeats": {r: {"applied_revision": 14} for r in monitor.ROLES},
    }
    inspections = [
        {
            "State": {"Running": True, "Health": {"Status": "healthy"}},
            "Mounts": [{"Destination": "/run/time-tl-release", "RW": False}],
            "RestartCount": 0,
            "Id": "original",
            "Config": {"Image": config["image"]},
        }
        for _ in monitor.ALL_ROLES
    ]
    monkeypatch.setattr(
        monitor, "run", Mock(side_effect=[json.dumps(data), json.dumps(inspections)])
    )
    monkeypatch.setattr(
        monitor.shutil, "disk_usage", lambda path: SimpleNamespace(free=50, total=100)
    )
    monkeypatch.setattr(Path, "read_text", lambda path: "MemTotal: 100 kB\nMemAvailable: 60 kB\n")
    connection = Mock()
    socket_object = Mock()
    connection.return_value.__enter__ = Mock(return_value=socket_object)
    connection.return_value.__exit__ = Mock(return_value=False)
    context = Mock()
    context.wrap_socket.return_value.__enter__ = Mock(
        return_value=Mock(getpeercert=Mock(return_value={"notAfter": "Oct  2 00:00:00 2030 GMT"}))
    )
    context.wrap_socket.return_value.__exit__ = Mock(return_value=False)
    default_context = Mock(return_value=context)
    monkeypatch.setattr(monitor.socket, "create_connection", connection)
    monkeypatch.setattr(monitor.ssl, "create_default_context", default_context)
    sample = monitor.collect(config)
    connection.assert_called_once_with(("127.0.0.1", 443), timeout=10)
    default_context.assert_called_once_with()
    context.wrap_socket.assert_called_once_with(
        socket_object, server_hostname=config["tls_hostname"]
    )
    assert sample["tls_remaining_days"] > 0
