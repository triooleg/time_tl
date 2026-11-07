"""Signed weather admission with ephemeral RAM-only Ed25519 unit-test keys.

No PEM/secret file, provider call, production key or deployment is involved.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from time_tl.runtime import contracts
from time_tl.runtime import weather_release as module
from time_tl.runtime.contracts import ProductionReleaseManifest, RuntimeContractError

NOW = datetime(2026, 11, 5, 6, tzinfo=UTC)
GEO_BYTES = (
    b"katottg,region_id,latitude,longitude,coverage_type\nUA00000000000000003,30,48.5,35,BASE\n"
)
PUBLISHER_COMMIT = "d" * 40
BASE = ProductionReleaseManifest(
    tag="time-tl-weather-unit",
    commit_sha="1" * 40,
    tree_sha="2" * 40,
    archive_sha256="3" * 64,
    image_digest="sha256:" + "4" * 64,
    config_revision=15,
    provider="alerts.in.ua/full-territory",
    catalog_sha256=contracts.PRODUCTION_RELEASE_MANIFEST["catalog_sha256"],
    migration_head="0015_alert_history",
    homework_enabled=True,
    replicas=1,
    territory_selection="manual-oblast-raion-hromada",
)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def settings(role="bot", **overrides):
    return SimpleNamespace(
        **{
            "environment": "production",
            "service_role": role,
            "owner_config_source": "database",
            "owner_config_revision": 15,
            "test_chat_allowlist": (),
            "weather_enabled": True,
            "weather_notifications_enabled": True,
            "weather_admin_enabled": True,
            "enabled": True,
            "dynamic_coverage": True,
            **overrides,
        }
    )


class Packet:
    def __init__(self):
        self.key = Ed25519PrivateKey.generate()  # Unit fixture only, never serialized.
        self.base = BASE
        package = Path(module.__file__).resolve().parents[1]
        names = {
            "time_tl/" + file.relative_to(package).as_posix() for file in package.rglob("*.py")
        }
        self.installed = {name: ("unit-source:" + name).encode() for name in names}
        self.geo = GEO_BYTES
        self.metadata = {
            "license": "ODbL-1.0",
            "coordinate_kind": "approximate-community-representative",
            "normalized_sha256": sha(self.geo),
            "catalog_version": "unit-exact-catalog",
            "catalog_sha256": "9" * 64,
            "catalog_source_sha256": "a" * 64,
            "publisher_commit": PUBLISHER_COMMIT,
            "normalized_url": "https://time.yoleotard.com/weather/geography/"
            + sha(self.geo)
            + "/geography.csv",
        }
        self.value = {
            "schema_version": 1,
            "kind": "time-tl-weather-geography-release",
            "profile": "primary",
            "mode": "public",
            "source_commit": BASE.commit_sha,
            "archive_sha256": BASE.archive_sha256,
            "image_digest": BASE.image_digest,
            "native_check_sha256": "6" * 64,
            "base_release_sha256": sha(encoded(asdict(BASE))),
            "migration_head": module.HEAD,
            "owner_revision": 15,
            "roles": sorted(module.ROLE_CAPABILITIES),
            "capabilities": dict.fromkeys(module.CAPABILITIES, True),
            "installed_files": {name: sha(raw) for name, raw in self.installed.items()},
            "geography_sha256": sha(self.geo),
            "geography_manifest_sha256": sha(encoded(self.metadata)),
            "source": "MET Norway",
        }
        self.resign()

    def resign(self):
        self.base_raw = encoded(asdict(self.base))
        self.raw = encoded(self.value)
        self.signature = base64.b64encode(self.key.sign(self.raw))
        self.base_signature = base64.b64encode(self.key.sign(self.base_raw))

    def load(self):
        return module.load_verified_weather_manifest(
            read_manifest=lambda: self.raw,
            read_signature=lambda: self.signature,
            read_installed=lambda name: self.installed[name],
            read_base_manifest=lambda: self.base_raw,
            read_base_signature=lambda: self.base_signature,
            read_geo=lambda: self.geo,
            read_geo_manifest=lambda: encoded(self.metadata),
        )

    def file(self, path, bound):
        values = {
            module.PATH: self.raw,
            module.SIGNATURE_PATH: self.signature,
            module.GEO_PATH: self.geo,
            module.GEO_MANIFEST_PATH: encoded(self.metadata),
            Path("/run/time-tl-release/release-manifest.json"): self.base_raw,
            Path("/run/time-tl-release/release-manifest.ed25519"): self.base_signature,
        }
        raw = values[path]
        if not 0 < len(raw) <= bound:
            raise RuntimeContractError("unit file bound rejected")
        return raw


@pytest.fixture
def packet(monkeypatch):
    packet = Packet()
    monkeypatch.setattr(module.production_release, "_public_key", lambda: packet.key.public_key())
    monkeypatch.setattr(module, "_installed", lambda name: packet.installed[name])
    monkeypatch.setattr(module, "_read", packet.file)
    return packet


@pytest.mark.parametrize("role", sorted(module.ROLE_CAPABILITIES))
def test_real_ed25519_normal_and_weather_signatures_bind_each_role(packet, role):
    value, digest, base = packet.load()
    assert (
        base == BASE
        and base.migration_head == contracts.PRODUCTION_RELEASE_MANIFEST["migration_head"]
    )
    authority = module.verify_weather_scope(
        role, settings(role), production_manifest=BASE, loader=packet.load
    )
    assert digest == sha(packet.raw) and authority.manifest_sha256 == digest
    assert {
        name for name in module.CAPABILITIES if authority.enabled(name)
    } == module.ROLE_CAPABILITIES[role]
    assert not authority.enabled("arbitrary_capability")
    assert value["source"] == "MET Norway"


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_commit", "f" * 40),
        ("archive_sha256", "f" * 64),
        ("image_digest", "sha256:" + "f" * 64),
        ("base_release_sha256", "f" * 64),
        ("migration_head", "0010_personal_calendar_overrides"),
        ("owner_revision", True),
        ("owner_revision", 16),
        ("schema_version", True),
        ("mode", "canary"),
        ("source", "Open-Meteo"),
        ("kind", "other-feature"),
        ("extra", True),
    ],
)
def test_signed_tampered_artifact_or_scope_rejected(packet, field, value):
    packet.value[field] = value
    packet.resign()
    with pytest.raises(RuntimeContractError):
        packet.load()


@pytest.mark.parametrize(
    "case", ["weather_payload", "normal_payload", "wrong_key", "bad_base64", "short_signature"]
)
def test_actual_crypto_rejects_tampered_or_wrong_key_signatures(packet, case):
    if case == "weather_payload":
        packet.raw += b" "
    if case == "normal_payload":
        packet.base_raw += b" "
    if case == "wrong_key":
        packet.signature = base64.b64encode(Ed25519PrivateKey.generate().sign(packet.raw))
    if case == "bad_base64":
        packet.signature = b"not!base64"
    if case == "short_signature":
        packet.signature = base64.b64encode(b"x" * 63)
    with pytest.raises(RuntimeContractError):
        packet.load()


@pytest.mark.parametrize(
    "case", ["duplicate", "nested_duplicate", "deep", "nonfinite", "oversize", "invalid_utf8"]
)
def test_malformed_signed_json_fails_closed(packet, case):
    raw = packet.raw
    if case == "duplicate":
        raw = raw[:-1] + b',"schema_version":1}'
    if case == "nested_duplicate":
        raw = raw.replace(b'"weather_ui":true', b'"weather_ui":true,"weather_ui":true')
    if case == "deep":
        raw = b'{"x":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}"
    if case == "nonfinite":
        raw = raw.replace(b'"owner_revision":15', b'"owner_revision":NaN')
    if case == "oversize":
        raw = b" " * 65537
    if case == "invalid_utf8":
        raw = b"\xff"
    packet.raw = raw
    packet.signature = base64.b64encode(packet.key.sign(raw))
    with pytest.raises(RuntimeContractError):
        packet.load()


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "traversal",
        "unrecognized_cap",
        "non_bool",
        "duplicate_role",
        "wrong_role",
        "missing_dependency",
        "installed_tamper",
    ],
)
def test_signed_source_closure_and_capability_schema_rejected(packet, case):
    if case == "missing":
        packet.value["installed_files"].pop("time_tl/infrastructure/db/weather.py")
    if case == "traversal":
        packet.value["installed_files"]["time_tl/../private.py"] = "b" * 64
    if case == "unrecognized_cap":
        packet.value["capabilities"]["broadcast"] = True
    if case == "non_bool":
        packet.value["capabilities"]["weather_collect"] = 1
    if case == "duplicate_role":
        packet.value["roles"][0] = packet.value["roles"][1]
    if case == "wrong_role":
        packet.value["roles"][0] = "alert-worker"
    if case == "missing_dependency":
        packet.value["capabilities"]["weather_collect"] = False
    packet.resign()
    if case == "installed_tamper":
        packet.installed["time_tl/infrastructure/db/weather.py"] += b"changed"
    with pytest.raises(RuntimeContractError):
        packet.load()


def test_weather_decision_modules_are_part_of_mandatory_installed_closure():
    assert {"time_tl/domain/weather.py", "time_tl/domain/coverage.py"} <= module.REQUIRED_FILES


def test_production_default_reader_requires_every_installed_python_module(packet):
    module.load_verified_weather_manifest()
    optional = set(packet.installed) - module.REQUIRED_FILES
    assert optional
    packet.value["installed_files"].pop(sorted(optional)[0])
    packet.resign()
    # Static required-file validation alone would pass; the installed inventory
    # must reject omission of another actual dependency.
    with pytest.raises(RuntimeContractError):
        module.load_verified_weather_manifest()


@pytest.mark.parametrize(
    "case",
    [
        "csv_tamper",
        "manifest_tamper",
        "wrong_license",
        "wrong_precision",
        "wrong_csv_hash",
        "missing_catalog_source",
    ],
)
def test_geo_bytes_and_provenance_are_bound(packet, case):
    if case == "csv_tamper":
        packet.geo += b"changed"
    elif case == "manifest_tamper":
        packet.metadata["catalog_version"] = "changed-after-signing"
    else:
        if case == "wrong_license":
            packet.metadata["license"] = "unreviewed"
        if case == "wrong_precision":
            packet.metadata["coordinate_kind"] = "exact-address"
        if case == "wrong_csv_hash":
            packet.metadata["normalized_sha256"] = "f" * 64
        if case == "missing_catalog_source":
            packet.metadata.pop("catalog_source_sha256")
        packet.value["geography_manifest_sha256"] = sha(encoded(packet.metadata))
        packet.resign()
    with pytest.raises(RuntimeContractError):
        packet.load()


def test_environment_can_narrow_but_not_escalate_unsigned_weather_capability(packet):
    authority = module.verify_weather_scope(
        "bot", settings(weather_notifications_enabled=False), loader=packet.load
    )
    assert authority.enabled("weather_ui") and not authority.enabled("weather_preferences")
    packet.value["capabilities"]["weather_preferences"] = False
    packet.resign()
    with pytest.raises(RuntimeContractError):
        module.verify_weather_scope("bot", settings(), loader=packet.load)


@pytest.mark.parametrize(
    "changes",
    [
        {"environment": "staging"},
        {"service_role": "sender-worker"},
        {"weather_enabled": "true"},
        {"owner_config_source": "environment"},
        {"owner_config_revision": 16},
        {"owner_config_revision": True},
        {"test_chat_allowlist": (123,)},
    ],
)
def test_runtime_admission_cannot_change_public_owner_scope(packet, changes):
    with pytest.raises(RuntimeContractError):
        module.verify_weather_scope("bot", settings(**changes), loader=packet.load)


def test_normal_gate_must_be_same_signed_head12_release(packet):
    with pytest.raises(RuntimeContractError):
        module.verify_weather_scope(
            "bot",
            settings(),
            production_manifest=replace(BASE, config_revision=16),
            loader=packet.load,
        )
    packet.base = replace(BASE, migration_head="0010_personal_calendar_overrides")
    packet.value["base_release_sha256"] = sha(encoded(asdict(packet.base)))
    packet.resign()
    with pytest.raises(RuntimeContractError):
        packet.load()


def fallback(packet):
    packet.value.update(
        profile="weather-off-fallback", geography_sha256=None, geography_manifest_sha256=None
    )
    packet.value["capabilities"] = dict.fromkeys(module.CAPABILITIES, False)
    packet.resign()


@pytest.mark.parametrize("role", sorted(module.ROLE_CAPABILITIES))
def test_signed_fallback_is_head12_and_all_seven_capabilities_off(packet, role):
    fallback(packet)
    cfg = settings(
        role,
        weather_enabled=False,
        weather_notifications_enabled=False,
        weather_admin_enabled=False,
        enabled=False,
        dynamic_coverage=False,
    )
    authority = module.verify_weather_scope(role, cfg, production_manifest=BASE, loader=packet.load)
    assert authority.migration_head == "0015_alert_history"
    assert all(not authority.enabled(name) for name in module.CAPABILITIES)
    with pytest.raises(RuntimeContractError):
        module.verify_weather_scope(role, settings(role), loader=packet.load)


def test_fallback_cannot_claim_one_enabled_capability(packet):
    fallback(packet)
    packet.value["capabilities"]["weather_admin"] = True
    packet.resign()
    with pytest.raises(RuntimeContractError):
        packet.load()


class Result:
    def __init__(self, value):
        self.value = value

    def mappings(self):
        return self

    def one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.value


class Session:
    def __init__(self):
        self.state = {"revision": 15, "pending": None, "has_pending": True}
        self.active = {
            "revision": 15,
            "environment": "production",
            "provider": "alerts_in_ua",
            "transport": "live",
            "allowlist": [],
        }
        self.heads = ["0015_alert_history"]
        self.catalog = ("unit-exact-catalog", "a" * 64)
        self.points = [
            (
                "UA00000000000000003",
                "30",
                48.5,
                35.0,
                "BASE",
                sha(GEO_BYTES),
                PUBLISHER_COMMIT,
                "https://time.yoleotard.com/weather/geography/" + sha(GEO_BYTES) + "/geography.csv",
                NOW,
            )
        ]
        self.queries = []

    async def execute(self, statement):
        sql = str(statement)
        self.queries.append(sql)
        return Result(
            self.state
            if "owner_configuration_state" in sql
            else self.active
            if "owner_configuration_active" in sql
            else self.heads
            if "alembic_version" in sql
            else self.points
            if "FROM weather_locations" in sql
            else self.catalog
        )


async def test_database_guard_projects_public_owner_then_head_and_catalog_source_hash(packet):
    authority = module.verify_weather_scope(
        "sender-worker", settings("sender-worker"), loader=packet.load
    )
    session = Session()
    await module.weather_authority_guard(authority, settings("sender-worker"))(session, NOW)
    assert len(session.queries) == 5
    assert "owner_configuration_state" in session.queries[0] and "FOR UPDATE" in session.queries[0]
    assert "owner_configuration_active" in session.queries[1] and "FOR UPDATE" in session.queries[1]
    assert "FOR SHARE" in session.queries[2]
    assert "v.source_sha256" in session.queries[3]
    assert packet.metadata["catalog_sha256"] != session.catalog[1]  # capture != DB source identity.
    assert all(
        "SELECT value FROM" not in sql and "encrypted" not in sql and "token" not in sql
        for sql in session.queries
    )


@pytest.mark.parametrize(
    "case",
    [
        "owner",
        "pending",
        "provider",
        "transport",
        "allowlist",
        "head9",
        "head10",
        "head11",
        "catalog_version",
        "catalog_source",
        "capture_hash_as_source",
    ],
)
async def test_database_authority_drift_fails_closed(packet, case):
    authority = module.verify_weather_scope(
        "weather-worker", settings("weather-worker"), loader=packet.load
    )
    session = Session()
    if case == "owner":
        session.state["revision"] = 16
    if case == "pending":
        session.state["pending"] = 16
    if case == "provider":
        session.active["provider"] = "fake"
    if case == "transport":
        session.active["transport"] = "disabled"
    if case == "allowlist":
        session.active["allowlist"] = [123]
    if case.startswith("head"):
        session.heads = [
            {
                "head9": "0009_calendar_foundation",
                "head10": "0010_personal_calendar_overrides",
                "head11": "0011_power_outages",
            }[case]
        ]
    if case == "catalog_version":
        session.catalog = ("other", "a" * 64)
    if case == "catalog_source":
        session.catalog = ("unit-exact-catalog", "f" * 64)
    if case == "capture_hash_as_source":
        session.catalog = ("unit-exact-catalog", "9" * 64)
    with pytest.raises(RuntimeContractError):
        await module.weather_authority_guard(authority, settings("weather-worker"))(session, NOW)


async def test_post_wait_signed_revocation_rechecked_by_same_transaction_guard(packet):
    authority = module.verify_weather_scope(
        "sender-worker", settings("sender-worker"), loader=packet.load
    )
    guard = module.weather_authority_guard(authority, settings("sender-worker"))
    await guard(Session(), NOW)
    packet.value["capabilities"]["weather_dispatch"] = False
    packet.resign()
    calls = 0
    with pytest.raises(RuntimeContractError):
        await guard(Session(), NOW)
        calls += 1
    assert calls == 0


@pytest.mark.parametrize(
    "index,value",
    [
        (0, "UA00000000000000004"),
        (1, "40"),
        (2, 49.0),
        (3, 36.0),
        (4, "EXTENDED"),
        (5, "f" * 64),
        (6, "e" * 40),
        (7, "https://example.invalid/unbound.csv"),
        (8, None),
    ],
)
async def test_enabled_db_point_must_match_signed_geometry_and_provenance(packet, index, value):
    authority = module.verify_weather_scope(
        "sender-worker", settings("sender-worker"), loader=packet.load
    )
    session = Session()
    row = list(session.points[0])
    row[index] = value
    session.points = [tuple(row)]
    with pytest.raises(RuntimeContractError):
        await module.weather_authority_guard(authority, settings("sender-worker"))(session, NOW)
