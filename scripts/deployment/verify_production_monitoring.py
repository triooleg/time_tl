"""Verify signed, fresh, one-use production monitoring evidence."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import math
import re
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

MONITORING_PUBLIC_KEY_HEX = (
    "1a52a9d96acb88d89be3de992efb75d968620f642c1240dd1121b96ca74f320c"
)
MAX_EVIDENCE_BYTES = 16_384
MAX_SIGNATURE_BYTES = 256
MAX_EVIDENCE_AGE_SECONDS = 900

_ENVIRONMENT_NAME = re.compile(r"[A-Z_][A-Z0-9_]*\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

INTEGER_RULES = {
    "MONITOR_PROVIDER_FRESHNESS_HOLD_SECONDS": (30, 120),
    "MONITOR_HEARTBEAT_HOLD_SECONDS": (30, 120),
    "MONITOR_CONTAINER_RESTART_HOLD_COUNT": (1, 1),
    "MONITOR_OUTBOX_OLDEST_HOLD_SECONDS": (15, 120),
    "MONITOR_OUTBOX_OPEN_HOLD_COUNT": (1, 100),
    "MONITOR_OUTBOX_DEAD_HOLD_COUNT": (1, 1),
    "MONITOR_OUTBOX_RETRY_HOLD_COUNT": (1, 1),
    "MONITOR_DISK_FREE_HOLD_PERCENT": (10, 30),
    "MONITOR_MEMORY_AVAILABLE_HOLD_PERCENT": (5, 25),
    "MONITOR_BACKUP_MAX_AGE_SECONDS": (3_600, 86_400),
    "MONITOR_TLS_MIN_REMAINING_DAYS": (7, 30),
}

OBSERVATION_FIELDS = {
    "provider_freshness_seconds",
    "max_worker_heartbeat_age_seconds",
    "container_restart_count_increase",
    "outbox_oldest_open_age_seconds",
    "outbox_open_count",
    "outbox_new_dead_count",
    "outbox_new_retry_count",
    "database_healthy",
    "disk_free_percent",
    "memory_available_percent",
    "verified_backup_age_seconds",
    "tls_remaining_days",
}


class VerificationError(ValueError):
    """Raised for a secret-free production monitoring contract failure."""


def read_environment(path: Path) -> dict[str, str]:
    """Parse dotenv values without expansion and without logging their contents."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        raise VerificationError("protected environment file is unavailable") from None
    result: dict[str, str] = {}
    for number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or not _ENVIRONMENT_NAME.fullmatch(name):
            raise VerificationError(f"protected environment line {number} is malformed")
        if name in result:
            raise VerificationError(f"protected environment contains duplicate {name}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        result[name] = value
    return result


def _positive_integer(environment: dict[str, str], name: str) -> int:
    raw = environment.get(name, "")
    try:
        value = int(raw)
    except ValueError:
        raise VerificationError(f"{name} must be an integer") from None
    if str(value) != raw or value <= 0:
        raise VerificationError(f"{name} must be a canonical positive integer")
    return value


def verify_monitoring_environment(environment: dict[str, str]) -> dict[str, int]:
    thresholds: dict[str, int] = {}
    for name, (minimum, maximum) in INTEGER_RULES.items():
        value = _positive_integer(environment, name)
        if not minimum <= value <= maximum:
            raise VerificationError(f"{name} is outside the fail-closed range")
        thresholds[name] = value
    stale_after = _positive_integer(environment, "ALERT_STALE_AFTER_SECONDS")
    if thresholds["MONITOR_PROVIDER_FRESHNESS_HOLD_SECONDS"] > stale_after:
        raise VerificationError(
            "provider freshness HOLD must not exceed ALERT_STALE_AFTER_SECONDS"
        )
    return thresholds


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise VerificationError("monitoring evidence contains duplicate keys")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise VerificationError("monitoring evidence contains a non-finite number")


def read_signed_evidence(
    evidence_path: Path,
    signature_path: Path,
    *,
    public_key_hex: str = MONITORING_PUBLIC_KEY_HEX,
) -> tuple[bytes, dict[str, Any]]:
    """Verify the signature over the exact evidence bytes, then parse strict JSON."""

    try:
        raw = evidence_path.read_bytes()
        encoded_signature = signature_path.read_bytes()
    except OSError:
        raise VerificationError("monitoring evidence or signature is unavailable") from None
    if not raw or len(raw) > MAX_EVIDENCE_BYTES:
        raise VerificationError("monitoring evidence size is invalid")
    if not encoded_signature or len(encoded_signature) > MAX_SIGNATURE_BYTES:
        raise VerificationError("monitoring evidence signature size is invalid")
    try:
        key_bytes = bytes.fromhex(public_key_hex)
        signature = base64.b64decode(encoded_signature.strip(), validate=True)
        if len(key_bytes) != 32 or len(signature) != 64:
            raise ValueError
        Ed25519PublicKey.from_public_bytes(key_bytes).verify(signature, raw)
    except (ValueError, binascii.Error, InvalidSignature):
        raise VerificationError("monitoring evidence signature is invalid") from None
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise VerificationError("monitoring evidence is malformed") from None
    if not isinstance(value, dict):
        raise VerificationError("monitoring evidence must be an object")
    return raw, value


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise VerificationError(f"monitoring evidence {field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise VerificationError(f"monitoring evidence {field} is invalid") from None
    if parsed.tzinfo is None:
        raise VerificationError(f"monitoring evidence {field} must include a timezone")
    return parsed.astimezone(UTC)


def _nonnegative_number(observations: dict[str, Any], field: str) -> float:
    value = observations[field]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VerificationError(f"monitoring observation {field} is invalid")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise VerificationError(f"monitoring observation {field} is invalid")
    return number


def verify_monitoring_evidence(
    value: dict[str, Any],
    thresholds: dict[str, int],
    *,
    now: datetime,
    seen_receipts: set[str] | None = None,
) -> str:
    expected_fields = {
        "schema_version",
        "generated_at",
        "notification_route_exercised_at",
        "notification_route_receipt_sha256",
        "observations",
        "stop_authority",
        "rollback_authority",
    }
    schema_version = value.get("schema_version")
    if (
        set(value) != expected_fields
        or isinstance(schema_version, bool)
        or schema_version != 1
    ):
        raise VerificationError("monitoring evidence has unexpected schema")
    generated_at = _timestamp(value["generated_at"], "generated_at")
    route_at = _timestamp(
        value["notification_route_exercised_at"],
        "notification_route_exercised_at",
    )
    current = now.astimezone(UTC)
    evidence_age = (current - generated_at).total_seconds()
    route_age = (current - route_at).total_seconds()
    if evidence_age < 0 or evidence_age > MAX_EVIDENCE_AGE_SECONDS:
        raise VerificationError("monitoring evidence is stale or from the future")
    if route_at > generated_at or route_age < 0 or route_age > MAX_EVIDENCE_AGE_SECONDS:
        raise VerificationError("notification-route exercise is stale or from the future")

    receipt = value["notification_route_receipt_sha256"]
    if not isinstance(receipt, str) or not _SHA256.fullmatch(receipt):
        raise VerificationError("notification-route receipt is invalid")
    if seen_receipts is not None and receipt in seen_receipts:
        raise VerificationError("monitoring evidence receipt was already consumed")

    observations = value["observations"]
    if not isinstance(observations, dict) or set(observations) != OBSERVATION_FIELDS:
        raise VerificationError("monitoring evidence observations are incomplete")
    if observations["database_healthy"] is not True:
        raise VerificationError("production database health check failed")
    observed = {
        field: _nonnegative_number(observations, field)
        for field in OBSERVATION_FIELDS - {"database_healthy"}
    }
    if observed["disk_free_percent"] > 100 or observed["memory_available_percent"] > 100:
        raise VerificationError("monitoring percentage observation is invalid")

    upper_limits = {
        "provider_freshness_seconds": "MONITOR_PROVIDER_FRESHNESS_HOLD_SECONDS",
        "max_worker_heartbeat_age_seconds": "MONITOR_HEARTBEAT_HOLD_SECONDS",
        "outbox_oldest_open_age_seconds": "MONITOR_OUTBOX_OLDEST_HOLD_SECONDS",
        "verified_backup_age_seconds": "MONITOR_BACKUP_MAX_AGE_SECONDS",
    }
    for field, threshold_name in upper_limits.items():
        if observed[field] > thresholds[threshold_name]:
            raise VerificationError(f"monitoring observation {field} requires HOLD")
    trip_at_or_above = {
        "container_restart_count_increase": "MONITOR_CONTAINER_RESTART_HOLD_COUNT",
        "outbox_open_count": "MONITOR_OUTBOX_OPEN_HOLD_COUNT",
        "outbox_new_dead_count": "MONITOR_OUTBOX_DEAD_HOLD_COUNT",
        "outbox_new_retry_count": "MONITOR_OUTBOX_RETRY_HOLD_COUNT",
    }
    for field, threshold_name in trip_at_or_above.items():
        if observed[field] >= thresholds[threshold_name]:
            raise VerificationError(f"monitoring observation {field} requires HOLD")
    lower_limits = {
        "disk_free_percent": "MONITOR_DISK_FREE_HOLD_PERCENT",
        "memory_available_percent": "MONITOR_MEMORY_AVAILABLE_HOLD_PERCENT",
        "tls_remaining_days": "MONITOR_TLS_MIN_REMAINING_DAYS",
    }
    for field, threshold_name in lower_limits.items():
        if observed[field] < thresholds[threshold_name]:
            raise VerificationError(f"monitoring observation {field} requires HOLD")
    for field in ("stop_authority", "rollback_authority"):
        authority = value[field]
        if not isinstance(authority, str) or not authority.strip() or len(authority) > 100:
            raise VerificationError(f"monitoring evidence {field} is invalid")
    return receipt


def consume_receipt(receipt_state_dir: Path, receipt: str, raw_sha256: str) -> None:
    """Claim the signed evidence receipt atomically so the gate cannot be replayed."""

    if not receipt_state_dir.is_dir():
        raise VerificationError("monitoring receipt state directory is unavailable")
    marker = receipt_state_dir / receipt
    try:
        with marker.open("x", encoding="ascii") as stream:
            stream.write(raw_sha256 + "\n")
    except FileExistsError:
        raise VerificationError("monitoring evidence receipt was already consumed") from None
    except OSError:
        raise VerificationError("monitoring evidence receipt cannot be consumed") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--evidence-json", type=Path, required=True)
    parser.add_argument("--signature-file", type=Path, required=True)
    parser.add_argument("--receipt-state-dir", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        thresholds = verify_monitoring_environment(read_environment(args.env_file))
        raw, evidence = read_signed_evidence(args.evidence_json, args.signature_file)
        receipt = verify_monitoring_evidence(evidence, thresholds, now=datetime.now(UTC))
        consume_receipt(args.receipt_state_dir, receipt, sha256(raw).hexdigest())
    except VerificationError as exc:
        print(f"production monitoring verification: REJECTED: {exc}")
        return 2
    print("production monitoring verification: PASS (signed one-use evidence)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
