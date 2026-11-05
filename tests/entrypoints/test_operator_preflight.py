from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from time_tl.admin.settings import ActivationCandidate
from time_tl.config import AdminBackend, Environment, OwnerConfigSource, Settings
from time_tl.entrypoints.operator_preflight import (
    OperatorPreflightError,
    preflight_production_owner_settings,
    probe_candidate,
    probe_production_candidate,
)
from time_tl.infrastructure.alerts import HttpResponse
from time_tl.runtime.contracts import (
    ProductionReleaseManifest,
    RuntimeContractError,
)
from time_tl.runtime.region_registry_full import EXPECTED_RECORD_COUNT, EXPECTED_SHA256

MARKER = "Thu, 24 Sep 2026 10:00:00 GMT"
SYNTHETIC_UNKNOWN_UID = "999999"


def full_body() -> bytes:
    return json.dumps(
        {
            "alerts": [
                {
                    "id": 10,
                    "location_title": "м. Київ",
                    "location_type": "city",
                    "started_at": "2026-09-24T09:00:00Z",
                    "finished_at": None,
                    "updated_at": "2026-09-24T09:01:00Z",
                    "alert_type": "air_raid",
                    "location_uid": "31",
                    "alert_level": "red",
                }
            ],
            "meta": {"last_updated_at": "2026/09/24 13:00:00 +0300"},
            "disclaimer": "Official alerts.in.ua data",
        }
    ).encode()


class FakeSession:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeBot:
    def __init__(self, _token: str) -> None:
        self.session = FakeSession()
        self.calls: list[str] = []

    async def get_me(self) -> SimpleNamespace:
        self.calls.append("get_me")
        return SimpleNamespace(is_bot=True, id=42, username="time_tl_staging_bot")


class FullTransport:
    async def send(self, _request: object) -> HttpResponse:
        return HttpResponse(200, {"Last-Modified": MARKER}, full_body())


def candidate(
    *,
    allowlist: tuple[int, ...] = (123,),
    environment: str = "staging",
    alert_provider: str = "alerts_in_ua",
    telegram_transport: str = "live",
) -> ActivationCandidate:
    return ActivationCandidate(
        revision=5,
        environment=environment,
        alert_provider=alert_provider,
        telegram_transport=telegram_transport,
        poll_interval_seconds=30,
        stale_after_seconds=120,
        staging_allowlist=allowlist,
        telegram_token=SecretStr("telegram-secret"),
        alerts_in_ua_token=SecretStr("alerts-secret"),
    )


def production_manifest() -> ProductionReleaseManifest:
    return ProductionReleaseManifest(
        tag="time-tl-production-20260928",
        commit_sha="a" * 40,
        tree_sha="b" * 40,
        archive_sha256="c" * 64,
        image_digest="sha256:" + "d" * 64,
        config_revision=5,
        provider="alerts.in.ua/full-territory",
        catalog_sha256=EXPECTED_SHA256,
        migration_head="0014_calendar_reminders",
        homework_enabled=True,
        replicas=1,
        territory_selection="manual-oblast-raion-hromada",
    )


@pytest.fixture(autouse=True)
def verified_release_attestation(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = production_manifest()
    monkeypatch.setattr(
        "time_tl.runtime.production_release.load_verified_release_attestation",
        lambda **_kwargs: (manifest, manifest.fingerprint()),
    )


def production_preflight_settings() -> Settings:
    key = SecretStr("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    return Settings(
        TIME_TL_ENV=Environment.PRODUCTION,
        SERVICE_ROLE="admin-web",
        DATABASE_URL=SecretStr("postgresql+asyncpg://user:pass@postgres/time_tl"),
        ADMIN_BACKEND=AdminBackend.POSTGRES,
        ADMIN_IP_HASH_KEY=SecretStr("x" * 32),
        OWNER_CONFIG_SOURCE=OwnerConfigSource.DATABASE,
        OWNER_TELEGRAM_FERNET_KEY=key,
        OWNER_ALERTS_FERNET_KEY=key,
        LESSON_LINK_FERNET_KEY=key,
        HOST_METRICS_URL="http://node-exporter:9100/metrics",
        ADMIN_BOOTSTRAP_ARTIFACT_PATH="/run/secrets/time-tl-admin-bootstrap",
        _env_file=None,
    )


def metrics_body() -> bytes:
    return b"""node_cpu_seconds_total{cpu="0",mode="idle"} 1000
node_cpu_seconds_total{cpu="0",mode="user"} 100
node_memory_MemTotal_bytes 1000
node_memory_MemAvailable_bytes 250
node_filesystem_size_bytes{device="/dev/sda1",fstype="ext4",mountpoint="/"} 2000
node_filesystem_avail_bytes{device="/dev/sda1",fstype="ext4",mountpoint="/"} 500
node_load1 1.5
node_load5 1.0
node_load15 0.5
node_network_receive_bytes_total{device="eth0"} 111
node_network_transmit_bytes_total{device="eth0"} 222
node_boot_time_seconds 1
"""


@pytest.mark.asyncio
async def test_probe_is_read_only_and_summary_contains_no_secrets_or_ids() -> None:
    bots: list[FakeBot] = []

    def bot_factory(token: str) -> FakeBot:
        bot = FakeBot(token)
        bots.append(bot)
        return bot

    evidence = await probe_candidate(
        candidate(),
        123,
        bot_factory=bot_factory,
        transport_factory=FullTransport,
    )

    assert bots[0].calls == ["get_me"]
    assert bots[0].session.closed
    summary = evidence.sanitized_summary()
    assert "revision=5" in summary
    assert f"covered_regions={EXPECTED_RECORD_COUNT}" in summary
    assert "alerts=active_json_full_200" in summary
    assert "persisted=no" in summary
    assert "123" not in summary
    assert "secret" not in summary


@pytest.mark.asyncio
async def test_production_probe_is_read_only_and_bound_to_exact_manifest() -> None:
    bots: list[FakeBot] = []

    def bot_factory(token: str) -> FakeBot:
        bot = FakeBot(token)
        bots.append(bot)
        return bot

    manifest = production_manifest()
    evidence = await probe_production_candidate(
        candidate(environment="production", allowlist=()),
        manifest,
        bot_factory=bot_factory,
        transport_factory=FullTransport,
    )

    assert bots[0].calls == ["get_me"]
    assert bots[0].session.closed
    assert evidence.release_manifest_sha256 == manifest.fingerprint()
    summary = evidence.sanitized_summary()
    assert "environment=production" in summary
    assert "allowlist_count=0" in summary
    assert f"release_manifest_sha256={manifest.fingerprint()}" in summary
    assert "persisted=no" in summary
    assert "secret" not in summary


@pytest.mark.asyncio
async def test_staging_and_production_preflight_contracts_cannot_be_interchanged() -> None:
    production = candidate(environment="production", allowlist=())
    staging = candidate()

    with pytest.raises(OperatorPreflightError, match="not STAGING"):
        await probe_candidate(production, 123, bot_factory=FakeBot)
    with pytest.raises(OperatorPreflightError, match="not PRODUCTION"):
        await probe_production_candidate(
            staging,
            production_manifest(),
            bot_factory=FakeBot,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("candidate_value", "message"),
    [
        (candidate(environment="production", allowlist=(123,)), "empty STAGING allowlist"),
        (
            candidate(environment="production", allowlist=(), alert_provider="fake"),
            "alerts.in.ua",
        ),
        (
            candidate(environment="production", allowlist=(), telegram_transport="disabled"),
            "live Telegram",
        ),
    ],
)
async def test_production_probe_rejects_staging_or_fake_runtime_fields_before_io(
    candidate_value: ActivationCandidate,
    message: str,
) -> None:
    calls = 0

    def bot_factory(_token: str) -> FakeBot:
        nonlocal calls
        calls += 1
        return FakeBot(_token)

    with pytest.raises(OperatorPreflightError, match=message):
        await probe_production_candidate(
            candidate_value,
            production_manifest(),
            bot_factory=bot_factory,
        )
    assert calls == 0


@pytest.mark.asyncio
async def test_production_probe_rejects_missing_credentials_without_io() -> None:
    missing = replace(
        candidate(environment="production", allowlist=()),
        telegram_token=None,
        alerts_in_ua_token=None,
    )
    with pytest.raises(OperatorPreflightError, match="credentials are incomplete"):
        await probe_production_candidate(
            missing,
            production_manifest(),
            bot_factory=FakeBot,
        )


@pytest.mark.asyncio
async def test_production_probe_rejects_wrong_manifest_before_io() -> None:
    candidate_value = candidate(environment="production", allowlist=())
    samples = (
        (replace(production_manifest(), provider="fake"), "provider"),
        (replace(production_manifest(), migration_head="0009_calendar_foundation"), "migration"),
        (
            replace(production_manifest(), config_revision=6),
            "config revision mismatch",
        ),
    )
    for manifest, message in samples:
        with pytest.raises(OperatorPreflightError, match=message):
            await probe_production_candidate(
                candidate_value,
                manifest,
                bot_factory=FakeBot,
            )


@pytest.mark.asyncio
async def test_production_probe_rejects_partial_snapshot_after_closing_bot() -> None:
    class PartialTransport:
        async def send(self, _request: object) -> HttpResponse:
            return HttpResponse(200, {}, full_body())

    bot = FakeBot("ignored")
    with pytest.raises(OperatorPreflightError, match="full-snapshot"):
        await probe_production_candidate(
            candidate(environment="production", allowlist=()),
            production_manifest(),
            bot_factory=lambda _token: bot,
            transport_factory=PartialTransport,
        )
    assert bot.session.closed


@pytest.mark.asyncio
async def test_production_owner_preflight_requires_stable_keys_metrics_and_db_config() -> None:
    base = production_preflight_settings()
    samples = (
        (base.model_copy(update={"environment": Environment.STAGING}), "TIME_TL_ENV"),
        (base.model_copy(update={"admin_backend": AdminBackend.DISABLED}), "PostgreSQL"),
        (
            base.model_copy(update={"owner_config_source": OwnerConfigSource.ENVIRONMENT}),
            "database-managed",
        ),
        (base.model_copy(update={"owner_alerts_fernet_key": None}), "stable encryption keys"),
        (base.model_copy(update={"host_metrics_url": None}), "HOST_METRICS_URL"),
        (
            base.model_copy(update={"admin_bootstrap_artifact_path": None}),
            "ADMIN_BOOTSTRAP_ARTIFACT_PATH",
        ),
    )
    for settings, message in samples:
        with pytest.raises(RuntimeContractError, match=message):
            await preflight_production_owner_settings(
                settings,
                5,
                bot_factory=FakeBot,
                transport_factory=FullTransport,
            )


@pytest.mark.asyncio
async def test_production_owner_preflight_stores_receipt_for_exact_pending_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[int] = []

    class FakeEngine:
        def __init__(self) -> None:
            self.disposed = False

        async def dispose(self) -> None:
            self.disposed = True

    class FakeOwnerSettingsService:
        receipt: tuple[object, ...] | None = None

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def production_activation_candidate(
            self, revision: int
        ) -> tuple[ActivationCandidate, str]:
            requested.append(revision)
            return candidate(environment="production", allowlist=()), "d" * 64

        async def store_production_preflight_receipt(self, *args: object) -> None:
            FakeOwnerSettingsService.receipt = args

    engine = FakeEngine()
    monkeypatch.setattr(
        "time_tl.runtime.services._postgres_admin_repository",
        lambda _settings: (engine, object()),
    )
    monkeypatch.setattr(
        "time_tl.entrypoints.operator_preflight.OwnerSettingsService",
        FakeOwnerSettingsService,
    )
    migration_gate = AsyncMock()
    monkeypatch.setattr(
        "time_tl.runtime.production_release.validate_production_database_migration_head",
        migration_gate,
    )

    evidence = await preflight_production_owner_settings(
        production_preflight_settings(),
        5,
        bot_factory=FakeBot,
        transport_factory=FullTransport,
        metrics_fetcher=lambda *_args: metrics_body(),
    )

    assert requested == [5]
    assert engine.disposed
    assert evidence.revision == 5
    assert evidence.host_metrics_ok
    assert evidence.receipt_stored
    migration_gate.assert_awaited_once()
    assert "receipt=stored" in evidence.sanitized_summary()
    assert "persisted=no" not in evidence.sanitized_summary()
    assert "node_exporter=ok" in evidence.sanitized_summary()
    assert FakeOwnerSettingsService.receipt is not None
    manifest = production_manifest()
    assert FakeOwnerSettingsService.receipt[:4] == (
        5,
        manifest.fingerprint(),
        "d" * 64,
        manifest.migration_head,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "ftp://node-exporter:9100/metrics",
        "https://example.com/metrics",
        "http://node-exporter:9100/other",
        "http://user:pass@node-exporter:9100/metrics",
    ],
)
async def test_production_preflight_rejects_unsafe_metrics_url_without_external_io(
    url: str,
) -> None:
    settings = production_preflight_settings().model_copy(update={"host_metrics_url": url})
    with pytest.raises(RuntimeContractError, match="node_exporter URL is invalid"):
        await preflight_production_owner_settings(
            settings,
            5,
            bot_factory=FakeBot,
            transport_factory=FullTransport,
            metrics_fetcher=lambda *_args: metrics_body(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metrics_fetcher",
    [
        lambda *_args: b"node_load1 1\n",
        lambda *_args: (_ for _ in ()).throw(OSError("offline")),
    ],
)
async def test_production_preflight_rejects_unreachable_or_invalid_metrics_response(
    metrics_fetcher: object,
) -> None:
    with pytest.raises(RuntimeContractError, match="node_exporter probe failed"):
        await preflight_production_owner_settings(
            production_preflight_settings(),
            5,
            bot_factory=FakeBot,
            transport_factory=FullTransport,
            metrics_fetcher=metrics_fetcher,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_probe_accepts_exact_catalogue_special_city_oblast_presentation() -> None:
    payload = json.loads(full_body())
    payload["alerts"][0]["location_type"] = "oblast"

    class SpecialCityTransport:
        async def send(self, _request: object) -> HttpResponse:
            return HttpResponse(
                200,
                {"Last-Modified": MARKER},
                json.dumps(payload).encode(),
            )

    evidence = await probe_candidate(
        candidate(),
        123,
        bot_factory=FakeBot,
        transport_factory=SpecialCityTransport,
    )

    summary = evidence.sanitized_summary()
    assert "alerts=active_json_full_200" in summary
    assert "м. Київ" not in summary
    assert "secret" not in summary


@pytest.mark.asyncio
async def test_probe_accepts_exact_catalogue_backed_city_alias_without_exposing_it() -> None:
    payload = json.loads(full_body())
    payload["alerts"][0].update(
        location_uid="5349",
        location_title="м. Марганець",
        location_type="city",
    )

    class CityAliasTransport:
        async def send(self, _request: object) -> HttpResponse:
            return HttpResponse(
                200,
                {"Last-Modified": MARKER},
                json.dumps(payload).encode(),
            )

    evidence = await probe_candidate(
        candidate(),
        123,
        bot_factory=FakeBot,
        transport_factory=CityAliasTransport,
    )

    summary = evidence.sanitized_summary()
    assert "alerts=active_json_full_200" in summary
    assert "5349" not in summary
    assert "Марганець" not in summary
    assert "secret" not in summary


@pytest.mark.asyncio
@pytest.mark.parametrize("allowlist", [(), (999,), (123, 999)])
async def test_probe_rejects_any_non_exact_allowlist(allowlist: tuple[int, ...]) -> None:
    with pytest.raises(OperatorPreflightError, match="exactly match"):
        await probe_candidate(
            candidate(allowlist=allowlist),
            123,
            bot_factory=FakeBot,
            transport_factory=FullTransport,
        )


@pytest.mark.asyncio
async def test_probe_rejects_partial_provider_response_after_closing_bot() -> None:
    class PartialTransport:
        async def send(self, _request: object) -> HttpResponse:
            return HttpResponse(200, {}, full_body())

    bot = FakeBot("ignored")
    with pytest.raises(OperatorPreflightError, match="full-snapshot") as captured:
        await probe_candidate(
            candidate(),
            123,
            bot_factory=lambda _token: bot,
            transport_factory=PartialTransport,
        )
    assert bot.session.closed
    assert "kind=PARTIAL_RESPONSE code=last_modified_missing count=1" in str(captured.value)


@pytest.mark.asyncio
async def test_probe_rejects_malformed_last_modified_marker() -> None:
    class MalformedMarkerTransport:
        async def send(self, _request: object) -> HttpResponse:
            return HttpResponse(200, {"Last-Modified": "not-an-http-date"}, full_body())

    with pytest.raises(OperatorPreflightError, match="full-snapshot") as captured:
        await probe_candidate(
            candidate(),
            123,
            bot_factory=FakeBot,
            transport_factory=MalformedMarkerTransport,
        )
    message = str(captured.value)
    assert "kind=INVALID_RESPONSE code=last_modified_invalid count=1" in message
    assert "not-an-http-date" not in message
    assert "secret" not in message


@pytest.mark.asyncio
async def test_probe_reports_unknown_uid_as_sanitized_non_pass() -> None:
    payload = json.loads(full_body())
    unknown = dict(payload["alerts"][0])
    unknown.update(
        id=11,
        location_uid=SYNTHETIC_UNKNOWN_UID,
        location_title="Синтетична невідома тестова локація",
    )
    payload["alerts"].append(unknown)

    class UnknownUidTransport:
        async def send(self, _request: object) -> HttpResponse:
            return HttpResponse(
                200,
                {"Last-Modified": MARKER},
                json.dumps(payload).encode(),
            )

    with pytest.raises(OperatorPreflightError, match="full-snapshot") as captured:
        await probe_candidate(
            candidate(),
            123,
            bot_factory=FakeBot,
            transport_factory=UnknownUidTransport,
        )

    message = str(captured.value)
    assert "kind=INVALID_RESPONSE code=location_uid_unknown count=1" in message
    assert "preflight passed" not in message
    assert SYNTHETIC_UNKNOWN_UID not in message
    assert "Синтетична" not in message
    assert "telegram-secret" not in message
    assert "alerts-secret" not in message


@pytest.mark.asyncio
async def test_probe_ignores_bounded_undocumented_field_without_exposing_it() -> None:
    payload = json.loads(full_body())
    payload["alerts"][0]["private_provider_field"] = "must-not-leak"

    class DriftTransport:
        async def send(self, _request: object) -> HttpResponse:
            return HttpResponse(
                200,
                {"Last-Modified": MARKER},
                json.dumps(payload).encode(),
            )

    evidence = await probe_candidate(
        candidate(),
        123,
        bot_factory=FakeBot,
        transport_factory=DriftTransport,
    )

    summary = evidence.sanitized_summary()
    assert "alerts=active_json_full_200" in summary
    assert "private_provider_field" not in summary
    assert "must-not-leak" not in summary
    assert "telegram-secret" not in summary
    assert "alerts-secret" not in summary


@pytest.mark.asyncio
async def test_probe_does_not_use_previously_diagnostic_only_alert_field() -> None:
    payload = json.loads(full_body())
    payload["alerts"][0]["calculated"] = "private-value-must-not-leak"

    class DiagnosticCandidateTransport:
        async def send(self, _request: object) -> HttpResponse:
            return HttpResponse(
                200,
                {"Last-Modified": MARKER},
                json.dumps(payload).encode(),
            )

    evidence = await probe_candidate(
        candidate(),
        123,
        bot_factory=FakeBot,
        transport_factory=DiagnosticCandidateTransport,
    )

    summary = evidence.sanitized_summary()
    assert "alerts=active_json_full_200" in summary
    assert "calculated" not in summary
    assert "private-value-must-not-leak" not in summary
    assert "123" not in summary
    assert "telegram-secret" not in summary
    assert "alerts-secret" not in summary


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [None, False, -31, 31, 31.0, "not-authoritative", [31, None], {"nested": True}],
)
async def test_probe_ignores_bounded_oblast_uid_without_exposing_it(value: object) -> None:
    payload = json.loads(full_body())
    payload["alerts"][0]["location_oblast_uid"] = value
    payload["alerts"][0]["private_provider_field"] = "must-not-leak"

    class IgnoredOblastUidTransport:
        async def send(self, _request: object) -> HttpResponse:
            return HttpResponse(
                200,
                {"Last-Modified": MARKER},
                json.dumps(payload).encode(),
            )

    evidence = await probe_candidate(
        candidate(),
        123,
        bot_factory=FakeBot,
        transport_factory=IgnoredOblastUidTransport,
    )

    summary = evidence.sanitized_summary()
    assert "alerts=active_json_full_200" in summary
    assert "location_oblast_uid" not in summary
    assert "private_provider_field" not in summary
    assert "must-not-leak" not in summary


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "code"),
    [
        (1 << 63, "alert_ignored_value_invalid"),
        ("private-value-must-not-leak" * 200, "alert_ignored_value_invalid"),
    ],
)
async def test_probe_rejects_unbounded_ignored_value_with_sanitized_category(
    value: object,
    code: str,
) -> None:
    payload = json.loads(full_body())
    payload["alerts"][0]["location_oblast_uid"] = value
    payload["alerts"][0]["private_provider_field"] = "must-not-leak"

    class InvalidOblastUidTransport:
        async def send(self, _request: object) -> HttpResponse:
            return HttpResponse(
                200,
                {"Last-Modified": MARKER},
                json.dumps(payload).encode(),
            )

    with pytest.raises(OperatorPreflightError, match="full-snapshot") as captured:
        await probe_candidate(
            candidate(),
            123,
            bot_factory=FakeBot,
            transport_factory=InvalidOblastUidTransport,
        )

    message = str(captured.value)
    assert f"kind=INVALID_RESPONSE code={code} count=1" in message
    assert "location_oblast_uid" not in message
    assert "private-value-must-not-leak" not in message
    assert "private_provider_field" not in message
    assert "must-not-leak" not in message
    assert "telegram-secret" not in message
    assert "alerts-secret" not in message


@pytest.mark.asyncio
async def test_probe_rejects_malformed_official_metadata_without_leaking_value() -> None:
    payload = json.loads(full_body())
    payload["meta"]["last_updated_at"] = "private-invalid-timestamp"

    class InvalidMetadataTransport:
        async def send(self, _request: object) -> HttpResponse:
            return HttpResponse(
                200,
                {"Last-Modified": MARKER},
                json.dumps(payload).encode(),
            )

    with pytest.raises(OperatorPreflightError, match="full-snapshot") as captured:
        await probe_candidate(
            candidate(),
            123,
            bot_factory=FakeBot,
            transport_factory=InvalidMetadataTransport,
        )

    message = str(captured.value)
    assert "code=meta_last_updated_at_format_invalid count=1" in message
    assert "private-invalid-timestamp" not in message
    assert "telegram-secret" not in message
    assert "alerts-secret" not in message
