"""Externally signed, fail-closed production release attestation.

The artifact is built first. Release automation then signs a detached manifest
containing the artifact and deployment identities. Runtime trusts only the
baked Ed25519 public key and fixed read-only manifest/signature paths. The
private key never belongs in this repository or image.

The signed ``image_digest`` is the approved deployment target. Runtime cannot
prove the digest of the container that is already executing; observing the
actual deployed ``image@sha256`` remains an external deployment/canary gate.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import text

from time_tl.config import Environment, OwnerConfigSource, Settings

from .contracts import (
    ProductionReleaseManifest,
    RuntimeContractError,
    validate_production_release_manifest,
)

# Exact release preparation sets only this reviewed public key. It cannot be
# overridden by env/admin; the corresponding private key remains external.
PRODUCTION_RELEASE_PUBLIC_KEY_HEX: str | None = (
    "65439f2f53e1a08e9dd15b80d07bbdb39c32f83ba482dbebe9c83311991e7772"
)
_ATTESTATION_PATH = Path("/run/time-tl-release/release-manifest.json")
_SIGNATURE_PATH = Path("/run/time-tl-release/release-manifest.ed25519")
_MAX_ATTESTATION_BYTES = 4_096
_MAX_SIGNATURE_BYTES = 256


@dataclass(frozen=True)
class ProductionRuntimeGate:
    manifest: ProductionReleaseManifest
    manifest_fingerprint: str
    active_payload: dict[str, Any]


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeContractError("production release attestation contains duplicate keys")
        result[key] = value
    return result


def _parse_manifest(payload: bytes) -> ProductionReleaseManifest:
    if len(payload) > _MAX_ATTESTATION_BYTES:
        raise RuntimeContractError("production release attestation exceeds the size limit")
    try:
        value = json.loads(payload, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeContractError("production release attestation is malformed") from None
    fields = {
        "tag",
        "commit_sha",
        "tree_sha",
        "archive_sha256",
        "image_digest",
        "config_revision",
        "provider",
        "catalog_sha256",
        "migration_head",
        "replicas",
        "territory_selection",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise RuntimeContractError("production release attestation has unexpected fields")
    string_fields = fields - {"config_revision", "replicas"}
    if any(not isinstance(value[field], str) for field in string_fields):
        raise RuntimeContractError("production release attestation has invalid field types")
    if any(
        not isinstance(value[field], int) or isinstance(value[field], bool)
        for field in {"config_revision", "replicas"}
    ):
        raise RuntimeContractError("production release attestation has invalid field types")
    return ProductionReleaseManifest(**value)


def _public_key() -> Ed25519PublicKey:
    if PRODUCTION_RELEASE_PUBLIC_KEY_HEX is None:
        raise RuntimeContractError("production release verification key is not compiled")
    try:
        key_bytes = bytes.fromhex(PRODUCTION_RELEASE_PUBLIC_KEY_HEX)
        if len(key_bytes) != 32:
            raise ValueError
        return Ed25519PublicKey.from_public_bytes(key_bytes)
    except ValueError:
        raise RuntimeContractError("production release verification key is invalid") from None


def _read_bounded(path: Path, maximum: int) -> bytes:
    with path.open("rb") as stream:
        return stream.read(maximum + 1)


def load_verified_release_attestation(
    *,
    expected_config_revision: int | None = None,
    read_manifest_bytes: Callable[[], bytes] | None = None,
    read_signature_bytes: Callable[[], bytes] | None = None,
) -> tuple[ProductionReleaseManifest, str]:
    """Verify the fixed detached attestation before trusting any release claim."""

    try:
        payload = (
            read_manifest_bytes()
            if read_manifest_bytes is not None
            else _read_bounded(_ATTESTATION_PATH, _MAX_ATTESTATION_BYTES)
        )
        encoded_signature = (
            read_signature_bytes()
            if read_signature_bytes is not None
            else _read_bounded(_SIGNATURE_PATH, _MAX_SIGNATURE_BYTES)
        )
    except OSError:
        raise RuntimeContractError("production release attestation is unavailable") from None
    if len(payload) > _MAX_ATTESTATION_BYTES:
        raise RuntimeContractError("production release attestation exceeds the size limit")
    if len(encoded_signature) > _MAX_SIGNATURE_BYTES:
        raise RuntimeContractError("production release signature exceeds the size limit")
    try:
        signature = base64.b64decode(encoded_signature.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise RuntimeContractError("production release signature is malformed") from None
    if len(signature) != 64:
        raise RuntimeContractError("production release signature is malformed")
    try:
        _public_key().verify(signature, payload)
    except InvalidSignature:
        raise RuntimeContractError("production release signature verification failed") from None
    manifest = _parse_manifest(payload)
    fingerprint = validate_production_release_manifest(
        manifest,
        expected_config_revision=(
            manifest.config_revision
            if expected_config_revision is None
            else expected_config_revision
        ),
    )
    return manifest, fingerprint


def production_preflight_correlation(revision: int, manifest_fingerprint: str) -> str:
    """Create a sanitized audit label; this is not proof that preflight ran."""

    if isinstance(revision, bool) or revision <= 0:
        raise RuntimeContractError("production preflight revision is invalid")
    if len(manifest_fingerprint) != 64 or any(
        character not in "0123456789abcdef" for character in manifest_fingerprint
    ):
        raise RuntimeContractError("production manifest fingerprint is invalid")
    return f"production-preflight-v1-r{revision}-{manifest_fingerprint}"


async def _read_atomic_runtime_facts(
    settings: Settings,
) -> tuple[list[str], dict[str, Any], dict[str, Any]]:
    """Read migration/state/ACTIVE payload in one PostgreSQL statement/snapshot."""

    from time_tl.infrastructure.db.engine import create_engine

    engine = None
    try:
        engine = create_engine(settings.database_url.get_secret_value())
        async with engine.connect() as connection:
            async with connection.begin():
                result = await connection.execute(
                    text(
                        "SELECT "
                        "(SELECT array_agg(version_num ORDER BY version_num) "
                        "FROM alembic_version) AS migration_heads, "
                        "(SELECT value FROM runtime_settings "
                        "WHERE key = 'owner_configuration_state') AS owner_state, "
                        "(SELECT value FROM runtime_settings "
                        "WHERE key = 'owner_configuration_active') AS owner_active"
                    )
                )
                row = result.mappings().one()
    except Exception:
        raise RuntimeContractError("production database release facts are unavailable") from None
    finally:
        if engine is not None:
            await engine.dispose()
    heads = row.get("migration_heads")
    state = row.get("owner_state")
    active = row.get("owner_active")
    if (
        not isinstance(heads, list)
        or any(not isinstance(item, str) for item in heads)
        or not isinstance(state, dict)
        or not isinstance(active, dict)
    ):
        raise RuntimeContractError("production database release facts are unavailable")
    return list(heads), dict(state), dict(active)


async def validate_production_database_migration_head(
    settings: Settings, manifest: ProductionReleaseManifest
) -> None:
    """Read the actual database head for a pending production operator action."""

    from time_tl.infrastructure.db.engine import create_engine

    engine = None
    try:
        engine = create_engine(settings.database_url.get_secret_value())
        async with engine.connect() as connection:
            result = await connection.execute(
                text("SELECT array_agg(version_num ORDER BY version_num) FROM alembic_version")
            )
            heads = result.scalar_one()
    except Exception:
        raise RuntimeContractError(
            "production database migration head is unavailable"
        ) from None
    finally:
        if engine is not None:
            await engine.dispose()
    if heads != [manifest.migration_head]:
        raise RuntimeContractError("production database migration head mismatch")


async def validate_production_runtime_release(
    settings: Settings,
) -> ProductionRuntimeGate | None:
    """Return the one trusted ACTIVE payload used by a production role startup."""

    if settings.environment is not Environment.PRODUCTION:
        return None
    if settings.owner_config_source is not OwnerConfigSource.DATABASE:
        raise RuntimeContractError("production runtime requires database-managed owner config")
    if settings.test_chat_allowlist:
        raise RuntimeContractError("production runtime requires empty STAGING allowlist")
    manifest, fingerprint = load_verified_release_attestation(
        expected_config_revision=settings.owner_config_revision
    )
    heads, state, active = await _read_atomic_runtime_facts(settings)
    state_revision = state.get("active_revision")
    active_revision = active.get("revision")
    if heads != [manifest.migration_head]:
        raise RuntimeContractError("production database migration head mismatch")
    if (
        not isinstance(state_revision, int)
        or isinstance(state_revision, bool)
        or state_revision != manifest.config_revision
        or not isinstance(active_revision, int)
        or isinstance(active_revision, bool)
        or active_revision != manifest.config_revision
        or active.get("environment") != Environment.PRODUCTION.value
        or active.get("alert_provider") != "alerts_in_ua"
        or active.get("telegram_transport") != "live"
        or active.get("staging_allowlist") != []
    ):
        raise RuntimeContractError("production ACTIVE configuration does not match release")
    return ProductionRuntimeGate(manifest, fingerprint, active)
