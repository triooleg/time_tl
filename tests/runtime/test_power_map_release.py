"""Strict public feature envelopes; synthetic verifier/SQL ports, no private keys."""

import base64
import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from cryptography.exceptions import InvalidSignature

from time_tl.runtime import contracts
from time_tl.runtime import power_map_release as module
from time_tl.runtime.contracts import ProductionReleaseManifest, RuntimeContractError

BASE = ProductionReleaseManifest(
    tag="time-tl-public-fixture",
    commit_sha="1" * 40,
    tree_sha="2" * 40,
    archive_sha256="3" * 64,
    image_digest="sha256:" + "4" * 64,
    config_revision=15,
    provider="alerts.in.ua/full-territory",
    catalog_sha256=contracts.PRODUCTION_RELEASE_MANIFEST["catalog_sha256"],
    migration_head="0014_calendar_reminders",
    homework_enabled=True,
    replicas=1,
    territory_selection="manual-oblast-raion-hromada",
)
BASE_RAW = json.dumps(asdict(BASE), sort_keys=True).encode()
SIGNATURE = base64.b64encode(b"S" * 64)


def settings(role="bot", **overrides):
    return SimpleNamespace(
        **{
            "service_role": role,
            "environment": "production",
            "owner_config_source": "database",
            "owner_config_revision": 15,
            "test_chat_allowlist": (),
            "power_outages_enabled": True,
            "power_alerts_energy_api_enabled": True,
            "power_map_reminders_enabled": True,
            "power_address_lookup_enabled": False,
            **overrides,
        }
    )


def manifest():
    files = set(module._REQUIRED_FILES) | {"time_tl/runtime/weather_release.py"}
    files.update(f"time_tl/fixture_module_{number}.py" for number in range(27 - len(files)))
    return {
        "schema_version": 2,
        "kind": "time-tl-power-map-public-release",
        "mode": "public",
        "source_commit": BASE.commit_sha,
        "archive_sha256": BASE.archive_sha256,
        "image_digest": BASE.image_digest,
        "native_check_sha256": "6" * 64,
        "base_release_sha256": hashlib.sha256(BASE_RAW).hexdigest(),
        "weather_release_sha256": "7" * 64,
        "migration_head": BASE.migration_head,
        "owner_revision": 15,
        "roles": ["bot", "map-planner", "sender-worker"],
        "capabilities": {
            "bot_power_ui": True,
            "map_preferences": True,
            "map_planner": True,
            "map_dispatch": True,
            "address_lookup": False,
        },
        "installed_files": {
            name: hashlib.sha256(name.encode()).hexdigest() for name in sorted(files)
        },
    }


class Verifier:
    """Mock only signature admission, not an Ed25519/production signature proof."""

    def __init__(self, expected):
        self.expected, self.calls = expected, 0

    def verify(self, signature, payload):
        self.calls += 1
        if signature != b"S" * 64 or payload != self.expected:
            raise InvalidSignature


def prepare(monkeypatch, value=None, *, raw=None, signature=SIGNATURE, installed=None):
    raw = raw if raw is not None else json.dumps(value or manifest(), sort_keys=True).encode()
    verifier = Verifier(raw)
    monkeypatch.setattr(module.production_release, "_public_key", lambda: verifier)
    monkeypatch.setattr(
        module,
        "_read_bounded",
        lambda path, limit: BASE_RAW if path == module.BASE_PATH else SIGNATURE,
    )
    calls = []
    from time_tl.runtime import weather_release

    monkeypatch.setattr(
        weather_release,
        "load_verified_weather_manifest",
        lambda: (
            {"image_digest": BASE.image_digest, "source_commit": BASE.commit_sha},
            "7" * 64,
            BASE,
        ),
    )

    def base_loader(**kwargs):
        calls.append(kwargs["expected_config_revision"])
        assert kwargs["read_manifest_bytes"]() == BASE_RAW
        assert kwargs["read_signature_bytes"]() == SIGNATURE
        return BASE, BASE.fingerprint()

    monkeypatch.setattr(module.production_release, "load_verified_release_attestation", base_loader)
    kwargs = {
        "read_manifest_bytes": lambda: raw,
        "read_signature_bytes": lambda: signature,
        "read_installed_file_bytes": installed or (lambda name: name.encode()),
    }
    return kwargs, verifier, calls


@pytest.mark.parametrize(
    "role,enabled",
    [
        ("bot", {"bot_power_ui", "map_preferences"}),
        ("map-planner", {"map_planner"}),
        ("sender-worker", {"map_dispatch"}),
    ],
)
def test_role_bound_authority_verifies_both_documents_and_every_installed_byte(
    monkeypatch, role, enabled
):
    kwargs, verifier, calls = prepare(monkeypatch)
    authority = module.verify_feature_scope(
        role, settings(role), production_manifest=BASE, **kwargs
    )
    assert verifier.calls == 1 and calls == [15]
    assert authority.source_commit == BASE.commit_sha and len(authority.installed_files) == 27
    assert {cap for cap in module.CAPABILITIES if authority.enabled(cap)} == enabled
    with pytest.raises(RuntimeContractError):
        authority.enabled("unknown")


@pytest.mark.parametrize(
    "changes",
    [
        {"mode": "canary"},
        {"kind": "time-tl-telegram-power-canary-overlay"},
        {"schema_version": True},
        {"owner_revision": True},
        {"owner_revision": 16},
        {"migration_head": "0011_power"},
        {"source_commit": "f" * 40},
        {"archive_sha256": "f" * 64},
        {"image_digest": "sha256:" + "f" * 64},
        {"base_release_sha256": "f" * 64},
        {"native_check_sha256": "invalid"},
        {"roles": ["bot", "bot", "sender-worker"]},
        {"roles": ["bot", "map-planner"]},
        {"extra": True},
    ],
)
def test_strict_manifest_and_base_binding_rejects_changes(monkeypatch, changes):
    kwargs, _, _ = prepare(monkeypatch, {**manifest(), **changes})
    with pytest.raises(RuntimeContractError):
        module.verify_feature_scope("bot", settings(), **kwargs)


@pytest.mark.parametrize("case", ["duplicate", "nested_duplicate", "deep", "nan", "large", "utf8"])
def test_duplicate_deep_nonfinite_large_and_invalid_encoding_fail_closed(monkeypatch, case):
    raw = json.dumps(manifest()).encode()
    if case == "duplicate":
        raw = raw[:-1] + b',"schema_version":1}'
    elif case == "nested_duplicate":
        raw = raw.replace(b'"bot_power_ui": true', b'"bot_power_ui":true,"bot_power_ui":true')
    elif case == "deep":
        raw = b'{"deep":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}"
    elif case == "nan":
        raw = raw.replace(b'"owner_revision": 15', b'"owner_revision":NaN')
    elif case == "large":
        raw = b" " * 32769
    elif case == "utf8":
        raw = b"\xff"
    kwargs, _, _ = prepare(monkeypatch, raw=raw)
    with pytest.raises(RuntimeContractError):
        module.verify_feature_scope("bot", settings(), **kwargs)


@pytest.mark.parametrize(
    "signature",
    [b"", b"!invalid", base64.b64encode(b"X" * 63), base64.b64encode(b"X" * 64), b"X" * 257],
)
def test_bad_signatures_fail_closed(monkeypatch, signature):
    kwargs, _, _ = prepare(monkeypatch, signature=signature)
    with pytest.raises(RuntimeContractError):
        module.verify_feature_scope("bot", settings(), **kwargs)


@pytest.mark.parametrize(
    "changes",
    [
        {"service_role": "sender-worker"},
        {"environment": "staging"},
        {"owner_config_source": "environment"},
        {"owner_config_revision": 16},
        {"owner_config_revision": True},
        {"test_chat_allowlist": (123,)},
        {"test_chat_allowlist": []},
        {"power_map_reminders_enabled": "true"},
        {"power_address_lookup_enabled": True},
    ],
)
def test_runtime_flags_cannot_escalate_scope(monkeypatch, changes):
    kwargs, _, _ = prepare(monkeypatch)
    with pytest.raises(RuntimeContractError):
        module.verify_feature_scope("bot", settings(**changes), **kwargs)


def test_environment_can_disable_but_cannot_enable_unsigned_capability(monkeypatch):
    kwargs, _, _ = prepare(monkeypatch)
    authority = module.verify_feature_scope(
        "bot", settings(power_map_reminders_enabled=False), **kwargs
    )
    assert authority.enabled("bot_power_ui") and not authority.enabled("map_preferences")
    assert not authority.enabled("map_preferences", settings())
    value = manifest()
    value["capabilities"]["map_preferences"] = False
    kwargs, _, _ = prepare(monkeypatch, value)
    with pytest.raises(RuntimeContractError):
        module.verify_feature_scope("bot", settings(), **kwargs)


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "traversal",
        "extra_capability",
        "invalid_bool",
        "address",
        "drift",
        "missing_dependency",
    ],
)
def test_installed_scope_and_capability_structure_are_strict(monkeypatch, case):
    value = deepcopy(manifest())
    installed = None
    if case == "missing":
        value["installed_files"].pop("time_tl/runtime/services.py")
    elif case == "traversal":
        value["installed_files"]["time_tl/../../private.py"] = "a" * 64
    elif case == "extra_capability":
        value["capabilities"]["mass_send"] = True
    elif case == "invalid_bool":
        value["capabilities"]["bot_power_ui"] = 1
    elif case == "address":
        value["capabilities"]["address_lookup"] = True
    elif case == "missing_dependency":
        value["capabilities"]["map_planner"] = False
    elif case == "drift":

        def installed(name):
            return b"changed-code"

    kwargs, _, _ = prepare(monkeypatch, value, installed=installed)
    with pytest.raises(RuntimeContractError):
        module.verify_feature_scope("bot", settings(), **kwargs)


def test_normal_production_gate_cannot_be_replaced_by_feature_scope(monkeypatch):
    kwargs, _, _ = prepare(monkeypatch)
    with pytest.raises(RuntimeContractError):
        module.verify_feature_scope(
            "bot", settings(), production_manifest=replace(BASE, config_revision=16), **kwargs
        )


def test_unresolved_revision_requires_the_completed_normal_production_gate(monkeypatch):
    kwargs, _, _ = prepare(monkeypatch)
    unresolved = settings(owner_config_revision=None)
    with pytest.raises(RuntimeContractError):
        module.verify_feature_scope("bot", unresolved, **kwargs)
    result = module.verify_feature_scope("bot", unresolved, production_manifest=BASE, **kwargs)
    assert result.owner_revision == 15 and result.enabled("bot_power_ui", unresolved)


def test_real_normal_parser_and_compiled_policy_accept_correct_provider_layer(monkeypatch):
    # Exercise the current signed head12 contract; all remaining normal compiled
    # rules and the real normal parser/validator run unchanged. Only signature
    # cryptography is a synthetic verifier, not production signature evidence.
    real_loader = module.production_release.load_verified_release_attestation
    monkeypatch.setitem(
        contracts.PRODUCTION_RELEASE_MANIFEST, "migration_head", module.MIGRATION_HEAD
    )
    kwargs, _, _ = prepare(monkeypatch)
    raw = kwargs["read_manifest_bytes"]()
    calls = []

    class PublicFixtureVerifier:
        def verify(self, signature, payload):
            calls.append(payload)
            if signature != b"S" * 64 or payload not in (raw, BASE_RAW):
                raise InvalidSignature

    monkeypatch.setattr(module.production_release, "load_verified_release_attestation", real_loader)
    monkeypatch.setattr(module.production_release, "_public_key", PublicFixtureVerifier)
    result = module.verify_feature_scope("bot", settings(), production_manifest=BASE, **kwargs)
    assert result.base_fingerprint == BASE.fingerprint()
    assert calls == [raw, BASE_RAW]


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
    def __init__(self, *, state=None, active=None, heads=None):
        self.state = (
            state
            if state is not None
            else {
                "revision": 15,
                "pending": None,
                "has_pending": True,
            }
        )
        self.active = (
            active
            if active is not None
            else {
                "revision": 15,
                "environment": "production",
                "provider": "alerts_in_ua",
                "transport": "live",
                "allowlist": [],
            }
        )
        self.heads = heads if heads is not None else ["0014_calendar_reminders"]
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
        )


def authority(monkeypatch):
    kwargs, _, _ = prepare(monkeypatch)
    return module.verify_feature_scope("map-planner", settings("map-planner"), **kwargs)


async def test_database_projection_and_lock_order_never_load_encrypted_active(monkeypatch):
    session = Session()
    await module.validate_feature_database(session, authority(monkeypatch), lock=True)
    assert "owner_configuration_state" in session.queries[0]
    assert "owner_configuration_active" in session.queries[1]
    assert session.queries[0].endswith("FOR UPDATE") and session.queries[1].endswith("FOR UPDATE")
    assert session.queries[2].endswith("FOR SHARE")
    assert all("SELECT value FROM" not in sql for sql in session.queries)
    assert all(
        "token" not in sql and "encrypted" not in sql and "fernet" not in sql
        for sql in session.queries
    )


@pytest.mark.parametrize("field,value", [("revision", 16), ("revision", True), ("pending", 16)])
async def test_owner_state_changes_fail_closed(monkeypatch, field, value):
    session = Session(state={"revision": 15, "pending": None, "has_pending": True, field: value})
    with pytest.raises(RuntimeContractError):
        await module.validate_feature_database(session, authority(monkeypatch), lock=True)


async def test_missing_pending_field_is_not_a_complete_owner_state(monkeypatch):
    with pytest.raises(RuntimeContractError):
        await module.validate_feature_database(
            Session(state={"revision": 15, "pending": None, "has_pending": False}),
            authority(monkeypatch),
            lock=True,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", 16),
        ("revision", True),
        ("environment", "staging"),
        ("provider", "fake"),
        ("transport", "disabled"),
        ("allowlist", [123]),
        ("allowlist", ()),
    ],
)
async def test_public_active_drift_and_missing_schema_fail_closed(monkeypatch, field, value):
    session = Session()
    session.active[field] = value
    with pytest.raises(RuntimeContractError):
        await module.validate_feature_database(session, authority(monkeypatch))
    with pytest.raises(RuntimeContractError):
        await module.validate_feature_database(
            Session(heads=["0011_power"]), authority(monkeypatch)
        )


def test_historical_schema1_keeps_head9_meaning_but_cannot_be_relabelled_current(monkeypatch):
    old = manifest()
    old["schema_version"] = 1
    old["migration_head"] = "0009_calendar_foundation"
    old.pop("weather_release_sha256")
    assert module._parse(json.dumps(old).encode())["migration_head"] == "0009_calendar_foundation"
    kwargs, _, _ = prepare(monkeypatch, old)
    with pytest.raises(RuntimeContractError):
        module.verify_feature_scope("bot", settings(), production_manifest=BASE, **kwargs)
    old["migration_head"] = "0014_calendar_reminders"
    with pytest.raises(RuntimeContractError):
        module._parse(json.dumps(old).encode())
