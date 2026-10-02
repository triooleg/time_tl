"""Fail-closed off-host evidence builder; never contacts or mutates production."""

from __future__ import annotations

import argparse
import base64
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.deployment.verify_production_monitoring import (
    MONITORING_PUBLIC_KEY_HEX,
    VerificationError,
    verify_monitoring_environment,
    verify_monitoring_evidence,
)

REQUIRED_WORKERS = frozenset({"bot", "alert-worker", "schedule-worker", "sender-worker"})
MAX_SAMPLE_AGE_SECONDS = 60
MAX_BASELINE_INTERVAL_SECONDS = 900

# Caller executes read-only; no user IDs, message bodies, URLs, or credentials returned.
AGGREGATE_SQL = """
SELECT json_build_object(
 'provider_freshness_seconds', (SELECT EXTRACT(EPOCH FROM now()-last_success_at)
   FROM alert_poll_state WHERE singleton_id=1),
 'provider_circuit_healthy', (SELECT NOT circuit_open AND consecutive_failures=0
   FROM alert_poll_state WHERE singleton_id=1),
 'heartbeats', (SELECT json_object_agg(worker_name, json_build_object(
   'age_seconds', EXTRACT(EPOCH FROM now()-heartbeat_at), 'status', status))
   FROM worker_heartbeats WHERE worker_name IN
   ('bot','alert-worker','schedule-worker','sender-worker')),
 'outbox_open_count', count(*) FILTER (WHERE delivered_at IS NULL AND dead_at IS NULL),
 'outbox_oldest_open_age_seconds', COALESCE(max(EXTRACT(EPOCH FROM now()-created_at))
   FILTER (WHERE delivered_at IS NULL AND dead_at IS NULL),0),
 'outbox_dead_total', count(*) FILTER (WHERE dead_at IS NOT NULL),
 'outbox_retry_total', COALESCE(sum(attempts),0),
 'database_healthy', true
) FROM outbox_messages
"""


def number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VerificationError(f"invalid {label}")
    if not math.isfinite(value) or value < 0:
        raise VerificationError(f"invalid {label}")
    return float(value)


def delta(sample: dict[str, Any], baseline: dict[str, Any], field: str) -> float:
    current = number(sample.get(field), field)
    previous = number(baseline.get(field), field)
    if current < previous:
        raise VerificationError(f"counter reset or retention prevents evaluating {field}")
    return current - previous


def observed_at(packet: dict[str, Any]) -> datetime:
    value = packet.get("observed_at")
    if not isinstance(value, str):
        raise VerificationError("observation acquisition timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise VerificationError("observation acquisition timestamp is invalid") from None
    if parsed.tzinfo is None:
        raise VerificationError("observation acquisition timestamp requires timezone")
    return parsed.astimezone(UTC)


def observations(sample: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """Evaluate actual aggregate observations, rejecting missing or unhealthy sources."""
    if sample.get("provider_circuit_healthy") is not True:
        raise VerificationError("provider circuit is unhealthy or unavailable")
    heartbeats = sample.get("heartbeats")
    if not isinstance(heartbeats, dict) or not REQUIRED_WORKERS <= heartbeats.keys():
        raise VerificationError("required worker heartbeat is missing")
    ages = []
    for role in REQUIRED_WORKERS:
        heartbeat = heartbeats[role]
        if not isinstance(heartbeat, dict) or heartbeat.get("status") != "healthy":
            raise VerificationError("required worker is unhealthy")
        ages.append(number(heartbeat.get("age_seconds"), "heartbeat age"))
    restarts = sample.get("restarts")
    previous = baseline.get("restarts")
    if not isinstance(restarts, dict) or not isinstance(previous, dict):
        raise VerificationError("restart baseline is unavailable")
    if set(restarts) != set(previous) or not REQUIRED_WORKERS <= restarts.keys():
        raise VerificationError("restart roles changed or are incomplete")
    result = {
        field: sample.get(field)
        for field in (
            "provider_freshness_seconds", "outbox_oldest_open_age_seconds",
            "outbox_open_count", "database_healthy", "disk_free_percent",
            "memory_available_percent", "verified_backup_age_seconds", "tls_remaining_days",
        )
    }
    result.update(
        max_worker_heartbeat_age_seconds=max(ages),
        container_restart_count_increase=sum(delta(restarts, previous, role) for role in restarts),
        outbox_new_dead_count=delta(sample, baseline, "outbox_dead_total"),
        outbox_new_retry_count=delta(sample, baseline, "outbox_retry_total"),
    )
    return result


def build_evidence(
    sample: dict[str, Any], baseline: dict[str, Any], environment: dict[str, str],
    route: dict[str, Any], *, now: datetime, stop_authority: str, rollback_authority: str,
) -> dict[str, Any]:
    """Route result must be supplied by an actual external notification exercise."""
    if now.tzinfo is None:
        raise VerificationError("evaluation timestamp requires timezone")
    sample_at = observed_at(sample)
    baseline_at = observed_at(baseline)
    age = (now.astimezone(UTC) - sample_at).total_seconds()
    interval = (sample_at - baseline_at).total_seconds()
    if age < 0 or age > MAX_SAMPLE_AGE_SECONDS:
        raise VerificationError("sample acquisition is stale or from the future")
    if interval < 0 or interval > MAX_BASELINE_INTERVAL_SECONDS:
        raise VerificationError("baseline acquisition interval is invalid")
    if route.get("delivered") is not True:
        raise VerificationError("notification route delivery is unconfirmed")
    observed = observations(sample, baseline)
    for field in (
        "provider_freshness_seconds", "max_worker_heartbeat_age_seconds",
        "verified_backup_age_seconds",
    ):
        observed[field] = number(observed[field], field) + age
    if number(observed["outbox_open_count"], "outbox_open_count") > 0:
        observed["outbox_oldest_open_age_seconds"] = number(
            observed["outbox_oldest_open_age_seconds"], "outbox_oldest_open_age_seconds"
        ) + age
    observed["tls_remaining_days"] = number(
        observed["tls_remaining_days"], "tls_remaining_days"
    ) - age / 86400
    if observed["tls_remaining_days"] < 0:
        raise VerificationError("TLS certificate expired since acquisition")
    evidence = {
        "schema_version": 1,
        "generated_at": now.astimezone(UTC).isoformat(),
        "notification_route_exercised_at": route.get("exercised_at"),
        "notification_route_receipt_sha256": route.get("receipt_sha256"),
        "observations": observed,
        "stop_authority": stop_authority,
        "rollback_authority": rollback_authority,
    }
    verify_monitoring_evidence(evidence, verify_monitoring_environment(environment), now=now)
    return evidence


def sign_evidence(evidence: dict[str, Any], existing_pem: bytes) -> tuple[bytes, bytes]:
    """Sign with the existing pinned key only; key creation and rotation are forbidden."""
    try:
        key = serialization.load_pem_private_key(existing_pem, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError
        public = key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw,
        )
        if public.hex() != MONITORING_PUBLIC_KEY_HEX:
            raise ValueError
    except (ValueError, TypeError):
        raise VerificationError(
            "existing monitoring signing key does not match pinned key"
        ) from None
    raw = json.dumps(evidence, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return raw, base64.b64encode(key.sign(raw)) + b"\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-json", type=Path, required=True)
    parser.add_argument("--existing-key", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-signature", type=Path, required=True)
    args = parser.parse_args()
    try:
        packet = json.loads(args.input_json.read_text(encoding="utf-8"))
        evidence = build_evidence(
            packet["sample"], packet["baseline"], packet["threshold_environment"],
            packet["route"], now=datetime.now(UTC), stop_authority=packet["stop_authority"],
            rollback_authority=packet["rollback_authority"],
        )
        raw, signature = sign_evidence(evidence, args.existing_key.read_bytes())
        with args.output_json.open("xb") as stream:
            stream.write(raw)
        with args.output_signature.open("xb") as stream:
            stream.write(signature)
    except (OSError, ValueError, KeyError, TypeError):
        print("offhost monitoring: REJECTED (invalid packet, key, or output)")
        return 2
    print("offhost monitoring: PASS (signed supplied observations; no remote mutations)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
