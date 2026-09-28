from __future__ import annotations

import base64
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "deployment" / "verify_production_monitoring.py"


def load_module():
    spec = importlib.util.spec_from_file_location("verify_production_monitoring", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def environment() -> dict[str, str]:
    return {
        "ALERT_STALE_AFTER_SECONDS": "120",
        "MONITOR_PROVIDER_FRESHNESS_HOLD_SECONDS": "120",
        "MONITOR_HEARTBEAT_HOLD_SECONDS": "90",
        "MONITOR_CONTAINER_RESTART_HOLD_COUNT": "1",
        "MONITOR_OUTBOX_OLDEST_HOLD_SECONDS": "60",
        "MONITOR_OUTBOX_OPEN_HOLD_COUNT": "100",
        "MONITOR_OUTBOX_DEAD_HOLD_COUNT": "1",
        "MONITOR_OUTBOX_RETRY_HOLD_COUNT": "1",
        "MONITOR_DISK_FREE_HOLD_PERCENT": "15",
        "MONITOR_MEMORY_AVAILABLE_HOLD_PERCENT": "10",
        "MONITOR_BACKUP_MAX_AGE_SECONDS": "86400",
        "MONITOR_TLS_MIN_REMAINING_DAYS": "14",
    }


def evidence(now: datetime) -> dict[str, object]:
    return {
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "notification_route_exercised_at": (now - timedelta(seconds=30)).isoformat(),
        "notification_route_receipt_sha256": "a" * 64,
        "observations": {
            "provider_freshness_seconds": 10,
            "max_worker_heartbeat_age_seconds": 20,
            "container_restart_count_increase": 0,
            "outbox_oldest_open_age_seconds": 5,
            "outbox_open_count": 0,
            "outbox_new_dead_count": 0,
            "outbox_new_retry_count": 0,
            "database_healthy": True,
            "disk_free_percent": 50,
            "memory_available_percent": 40,
            "verified_backup_age_seconds": 300,
            "tls_remaining_days": 60,
        },
        "stop_authority": "on-call operator",
        "rollback_authority": "release owner",
    }


def signed_files(tmp_path: Path, value: dict[str, object]):
    private_key = Ed25519PrivateKey.generate()
    public_hex = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    ).hex()
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    evidence_path = tmp_path / "monitoring-evidence.json"
    signature_path = tmp_path / "monitoring-evidence.ed25519"
    evidence_path.write_bytes(raw)
    signature_path.write_bytes(base64.b64encode(private_key.sign(raw)))
    return evidence_path, signature_path, public_hex, raw


def test_monitoring_contract_accepts_signed_numeric_evidence(tmp_path: Path) -> None:
    module = load_module()
    now = datetime(2026, 9, 28, 20, 0, tzinfo=UTC)
    thresholds = module.verify_monitoring_environment(environment())
    evidence_path, signature_path, public_hex, raw = signed_files(tmp_path, evidence(now))
    signed_raw, parsed = module.read_signed_evidence(
        evidence_path,
        signature_path,
        public_key_hex=public_hex,
    )
    assert signed_raw == raw
    assert module.verify_monitoring_evidence(parsed, thresholds, now=now) == "a" * 64


def test_pinned_monitoring_public_key_is_exact() -> None:
    module = load_module()
    assert (
        module.MONITORING_PUBLIC_KEY_HEX
        == "1a52a9d96acb88d89be3de992efb75d968620f642c1240dd1121b96ca74f320c"
    )


def test_documented_production_environment_has_valid_monitoring_thresholds() -> None:
    module = load_module()
    values = module.read_environment(ROOT / ".env.production.example")
    module.verify_monitoring_environment(values)


def test_monitoring_gate_is_documented_but_not_leaked_to_application_roles() -> None:
    runbook = (ROOT / "docs" / "deployment" / "PORTAINER.md").read_text(
        encoding="utf-8"
    )
    compose = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
    assert "--signature-file" in runbook
    assert "--receipt-state-dir" in runbook
    assert "external off-host collector" in runbook
    assert "MONITOR_PROVIDER_FRESHNESS_HOLD_SECONDS" not in compose


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MONITOR_PROVIDER_FRESHNESS_HOLD_SECONDS", "121"),
        ("MONITOR_HEARTBEAT_HOLD_SECONDS", "0"),
        ("MONITOR_CONTAINER_RESTART_HOLD_COUNT", "2"),
        ("MONITOR_OUTBOX_DEAD_HOLD_COUNT", "2"),
        ("MONITOR_BACKUP_MAX_AGE_SECONDS", "86401"),
    ],
)
def test_monitoring_contract_rejects_unsafe_thresholds(name: str, value: str) -> None:
    module = load_module()
    values = environment()
    values[name] = value
    with pytest.raises(module.VerificationError):
        module.verify_monitoring_environment(values)


def test_signature_binds_exact_raw_bytes_and_rejects_unsigned_or_modified(
    tmp_path: Path,
) -> None:
    module = load_module()
    now = datetime(2026, 9, 28, 20, 0, tzinfo=UTC)
    evidence_path, signature_path, public_hex, _raw = signed_files(tmp_path, evidence(now))
    with pytest.raises(module.VerificationError):
        module.read_signed_evidence(
            evidence_path,
            tmp_path / "missing-signature",
            public_key_hex=public_hex,
        )
    evidence_path.write_bytes(evidence_path.read_bytes() + b"\n")
    with pytest.raises(module.VerificationError):
        module.read_signed_evidence(
            evidence_path,
            signature_path,
            public_key_hex=public_hex,
        )


def test_signed_evidence_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    module = load_module()
    private_key = Ed25519PrivateKey.generate()
    public_hex = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    ).hex()
    raw = b'{"schema_version":1,"schema_version":1}'
    evidence_path = tmp_path / "duplicate.json"
    signature_path = tmp_path / "duplicate.ed25519"
    evidence_path.write_bytes(raw)
    signature_path.write_bytes(base64.b64encode(private_key.sign(raw)))
    with pytest.raises(module.VerificationError):
        module.read_signed_evidence(
            evidence_path,
            signature_path,
            public_key_hex=public_hex,
        )


def test_evidence_rejects_stale_future_replayed_and_unexpected_schema() -> None:
    module = load_module()
    now = datetime(2026, 9, 28, 20, 0, tzinfo=UTC)
    thresholds = module.verify_monitoring_environment(environment())
    stale = evidence(now - timedelta(seconds=901))
    with pytest.raises(module.VerificationError):
        module.verify_monitoring_evidence(stale, thresholds, now=now)
    future = evidence(now + timedelta(seconds=1))
    with pytest.raises(module.VerificationError):
        module.verify_monitoring_evidence(future, thresholds, now=now)
    stale_route = evidence(now)
    stale_route["notification_route_exercised_at"] = (
        now - timedelta(seconds=901)
    ).isoformat()
    with pytest.raises(module.VerificationError):
        module.verify_monitoring_evidence(stale_route, thresholds, now=now)
    with pytest.raises(module.VerificationError):
        module.verify_monitoring_evidence(
            evidence(now), thresholds, now=now, seen_receipts={"a" * 64}
        )
    unexpected = evidence(now)
    unexpected["extra"] = True
    with pytest.raises(module.VerificationError):
        module.verify_monitoring_evidence(unexpected, thresholds, now=now)
    bool_schema = evidence(now)
    bool_schema["schema_version"] = True
    with pytest.raises(module.VerificationError):
        module.verify_monitoring_evidence(bool_schema, thresholds, now=now)


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    [
        ("provider_freshness_seconds", 121),
        ("max_worker_heartbeat_age_seconds", 91),
        ("container_restart_count_increase", 1),
        ("outbox_oldest_open_age_seconds", 61),
        ("outbox_open_count", 100),
        ("outbox_new_dead_count", 1),
        ("outbox_new_retry_count", 1),
        ("disk_free_percent", 14.9),
        ("memory_available_percent", 9.9),
        ("verified_backup_age_seconds", 86401),
        ("tls_remaining_days", 13.9),
    ],
)
def test_numeric_observations_trigger_hold(field: str, unsafe_value: float) -> None:
    module = load_module()
    now = datetime(2026, 9, 28, 20, 0, tzinfo=UTC)
    value = evidence(now)
    observations = value["observations"]
    assert isinstance(observations, dict)
    observations[field] = unsafe_value
    thresholds = module.verify_monitoring_environment(environment())
    with pytest.raises(module.VerificationError):
        module.verify_monitoring_evidence(value, thresholds, now=now)


def test_database_failure_and_invalid_receipt_trigger_hold() -> None:
    module = load_module()
    now = datetime(2026, 9, 28, 20, 0, tzinfo=UTC)
    thresholds = module.verify_monitoring_environment(environment())
    unhealthy = evidence(now)
    observations = unhealthy["observations"]
    assert isinstance(observations, dict)
    observations["database_healthy"] = False
    with pytest.raises(module.VerificationError):
        module.verify_monitoring_evidence(unhealthy, thresholds, now=now)
    bad_receipt = evidence(now)
    bad_receipt["notification_route_receipt_sha256"] = "not-a-digest"
    with pytest.raises(module.VerificationError):
        module.verify_monitoring_evidence(bad_receipt, thresholds, now=now)


def test_receipt_consumption_is_atomic_and_rejects_replay(tmp_path: Path) -> None:
    module = load_module()
    state_dir = tmp_path / "receipts"
    state_dir.mkdir()
    module.consume_receipt(state_dir, "a" * 64, "b" * 64)
    assert (state_dir / ("a" * 64)).read_text(encoding="ascii") == "b" * 64 + "\n"
    with pytest.raises(module.VerificationError):
        module.consume_receipt(state_dir, "a" * 64, "c" * 64)
