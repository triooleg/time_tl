"""Public map capability composition without database/network/Telegram delivery."""

from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tests.runtime.test_power_map_release import BASE
from tests.runtime.test_power_map_release import authority as fixture_authority
from tests.runtime.test_startup_contracts import settings_for
from time_tl.config import Environment, OwnerConfigSource
from time_tl.runtime import services
from time_tl.runtime.contracts import DEFAULT_CAPABILITIES, RuntimeContractError


def settings(role="bot", *, enabled=True):
    return settings_for(role, environment=Environment.PRODUCTION).model_copy(
        update={
            "owner_config_source": OwnerConfigSource.DATABASE,
            "owner_config_revision": None,
            "power_outages_enabled": enabled,
            "power_alerts_energy_api_enabled": enabled,
            "power_map_reminders_enabled": enabled,
            "power_address_lookup_enabled": False,
        }
    )


def signed_authority(monkeypatch, role="bot", *, preferences=True):
    base = fixture_authority(monkeypatch)
    allowed = {"bot_power_ui", "map_preferences"} if role == "bot" else {"map_dispatch"}
    if not preferences:
        allowed.discard("map_preferences")
    result = replace(
        base,
        role=role,
        effective_capabilities=tuple(
            (name, name in allowed) for name, flag in base.effective_capabilities
        ),
    )
    verifier = Mock(return_value=result)
    monkeypatch.setattr(services, "verify_feature_scope", verifier)
    return result, verifier


class StopComposition(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def no_http(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("composition test must not open HTTP")

    monkeypatch.setattr("httpx.AsyncClient", forbidden)


@pytest.mark.parametrize("role", ["bot", "sender-worker"])
async def test_normal_gate_failure_precedes_feature_and_owner_resolution(monkeypatch, role):
    from time_tl.runtime import production_release

    normal = AsyncMock(side_effect=RuntimeContractError("normal gate closed"))
    feature = Mock(side_effect=AssertionError("feature cannot replace normal gate"))
    resolve = AsyncMock()
    monkeypatch.setattr(production_release, "validate_production_runtime_release", normal)
    monkeypatch.setattr(services, "verify_feature_scope", feature)
    monkeypatch.setattr(services, "resolve_owner_runtime_settings", resolve)
    runner = services.run_bot if role == "bot" else services.run_sender_worker
    with pytest.raises(RuntimeContractError, match="normal gate closed"):
        await runner(settings(role))
    feature.assert_not_called()
    resolve.assert_not_awaited()


@pytest.mark.parametrize("role", ["bot", "sender-worker"])
async def test_feature_failure_precedes_owner_decryption_and_clients(monkeypatch, role):
    from time_tl.runtime import production_release

    monkeypatch.setattr(
        production_release,
        "validate_production_runtime_release",
        AsyncMock(return_value=SimpleNamespace(manifest=BASE)),
    )
    monkeypatch.setattr(
        services,
        "verify_feature_scope",
        Mock(side_effect=RuntimeContractError("feature scope closed")),
    )
    resolve = AsyncMock()
    monkeypatch.setattr(services, "resolve_owner_runtime_settings", resolve)
    runner = services.run_bot if role == "bot" else services.run_sender_worker
    with pytest.raises(RuntimeContractError, match="feature scope closed"):
        await runner(settings(role))
    resolve.assert_not_awaited()


def test_missing_normal_gate_and_unsigned_flags_cannot_grant_review_facts():
    with pytest.raises(RuntimeContractError):
        services._admit_map_feature("bot", settings(), None)
    assert services._reviewed_map_capabilities(None) is DEFAULT_CAPABILITIES
    assert not services._map_preferences_enabled(settings(), None)
    assert not DEFAULT_CAPABILITIES.reviewed_power_outages


def test_disabled_legacy_never_opens_feature_artifacts(monkeypatch):
    verifier = Mock(side_effect=AssertionError("legacy disabled should not need feature files"))
    monkeypatch.setattr(services, "verify_feature_scope", verifier)
    assert services._admit_map_feature("bot", settings(enabled=False), None) is None
    assert services._reviewed_map_capabilities(None) is DEFAULT_CAPABILITIES
    verifier.assert_not_called()


async def test_permanent_bot_preserves_original_admission_and_passes_signed_ui(monkeypatch):
    import aiogram

    from time_tl.runtime import imports, production_release

    original = settings()
    resolved = original.model_copy(
        update={"owner_config_source": OwnerConfigSource.ENVIRONMENT, "owner_config_revision": 15}
    )
    authority, verifier = signed_authority(monkeypatch)
    gate = SimpleNamespace(manifest=BASE)
    monkeypatch.setattr(
        production_release, "validate_production_runtime_release", AsyncMock(return_value=gate)
    )
    resolved_call = AsyncMock(return_value=resolved)
    monkeypatch.setattr(services, "resolve_owner_runtime_settings", resolved_call)
    contract = Mock()
    monkeypatch.setattr(services, "validate_runtime_contract", contract)
    dependency_kwargs, app_kwargs = {}, {}
    port = SimpleNamespace(address_port=None)

    @asynccontextmanager
    async def dependencies(value, **kwargs):
        assert value is resolved
        dependency_kwargs.update(kwargs)
        yield (
            object(),
            object(),
            (),
            lambda actor: object(),
            object(),
            object(),
            object(),
            port,
            object(),
            object(),
        )

    class Application:
        def __init__(self, **kwargs):
            app_kwargs.update(kwargs)
            raise StopComposition

    monkeypatch.setattr(services, "postgres_bot_dependencies", dependencies)
    monkeypatch.setattr(imports, "DurableAsyncTelegramApplication", Application)
    monkeypatch.setattr(imports, "DurableImportCoordinator", lambda *args: object())
    monkeypatch.setattr(imports, "PostgresTransactionalProfileStore", lambda *args: object())
    bot = Mock(side_effect=AssertionError("application assertion ends before Telegram client"))
    monkeypatch.setattr(aiogram, "Bot", bot)
    with pytest.raises(StopComposition):
        await services.run_bot(original)
    verifier.assert_called_once_with("bot", original, production_manifest=BASE)
    resolved_call.assert_awaited_once_with(original, production_gate=gate)
    assert dependency_kwargs == {"power_authority": authority, "power_feature_settings": original}
    assert app_kwargs["power_port"] is port and app_kwargs["power_notifications_available"]
    assert app_kwargs["power_address_port"] is None and not app_kwargs["power_forecast_available"]
    assert app_kwargs["power_default_outage_lead"] == 10
    assert app_kwargs["power_default_restore_lead"] == 10
    assert contract.call_args.args[2].reviewed_power_outages
    assert not contract.call_args.args[2].reviewed_power_address
    assert original.owner_config_revision is None
    bot.assert_not_called()


async def test_production_dependencies_require_authority_before_creating_engine(monkeypatch):
    from time_tl.infrastructure.db import engine

    create = Mock(side_effect=AssertionError("no unsigned dependency engine"))
    monkeypatch.setattr(engine, "create_engine", create)
    with pytest.raises(RuntimeContractError, match="signed bot power authority"):
        async with services.postgres_bot_dependencies(settings()):
            raise AssertionError("unsigned composition was admitted")
    create.assert_not_called()


@pytest.mark.parametrize("preferences", [True, False])
async def test_signed_dependencies_reverify_and_never_enable_address_lookup(
    monkeypatch, preferences
):
    from time_tl.infrastructure.db import engine
    from time_tl.runtime import power_map_bot

    original = settings()
    resolved = original.model_copy(
        update={"owner_config_source": OwnerConfigSource.ENVIRONMENT, "owner_config_revision": 15}
    )
    authority, verifier = signed_authority(monkeypatch, preferences=preferences)
    resource = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(engine, "create_engine", Mock(return_value=resource))
    monkeypatch.setattr(engine, "create_session_factory", Mock(return_value=object()))
    monkeypatch.setattr(services, "_assert_territory_runtime_ready", AsyncMock())
    port = SimpleNamespace(address_port=None)
    constructor = Mock(return_value=port)
    monkeypatch.setattr(power_map_bot, "build_power_map_bot_port", constructor)
    async with services.postgres_bot_dependencies(
        resolved,
        power_authority=authority,
        power_feature_settings=original,
    ) as parts:
        assert len(parts) == 10 and parts[7] is port
    assert constructor.call_args.kwargs == {
        "address_lookup_enabled": False,
        "notifications_enabled": preferences,
    }
    verified_original = verifier.call_args.args[1]
    assert verified_original.owner_config_source is OwnerConfigSource.DATABASE
    assert verified_original.owner_config_revision == 15 and original.owner_config_revision is None
    resource.dispose.assert_awaited_once()


def test_environment_can_only_further_disable_a_signed_preference_capability(monkeypatch):
    authority, _ = signed_authority(monkeypatch)
    assert services._map_preferences_enabled(settings(), authority)
    assert not services._map_preferences_enabled(
        settings().model_copy(
            update={
                "power_map_reminders_enabled": False,
            }
        ),
        authority,
    )


@pytest.mark.parametrize("enabled", [True, False])
async def test_sender_uses_original_settings_for_final_signed_guard(monkeypatch, enabled):
    import aiogram

    from time_tl.application import sender_worker
    from time_tl.infrastructure.db import engine, lease, workers
    from time_tl.runtime import production_release

    original = settings("sender-worker", enabled=enabled)
    resolved = original.model_copy(
        update={"owner_config_source": OwnerConfigSource.ENVIRONMENT, "owner_config_revision": 15}
    )
    authority, verifier = signed_authority(monkeypatch, "sender-worker")
    monkeypatch.setattr(
        production_release,
        "validate_production_runtime_release",
        AsyncMock(return_value=SimpleNamespace(manifest=BASE)),
    )
    monkeypatch.setattr(
        services, "resolve_owner_runtime_settings", AsyncMock(return_value=resolved)
    )
    monkeypatch.setattr(services, "validate_runtime_contract", Mock())
    final_guard = AsyncMock()
    guard_factory = Mock(return_value=final_guard)
    monkeypatch.setattr(services, "feature_authority_guard", guard_factory)
    resource = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(engine, "create_engine", Mock(return_value=resource))
    monkeypatch.setattr(engine, "create_session_factory", Mock(return_value=object()))
    monkeypatch.setattr(services, "_assert_territory_runtime_ready", AsyncMock())
    client = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
    monkeypatch.setattr(aiogram, "Bot", Mock(return_value=client))
    repository_kwargs = {}

    class Repository:
        def __init__(self, factory, **kwargs):
            repository_kwargs.update(kwargs)

    monkeypatch.setattr(workers, "PostgresSenderRepository", Repository)
    monkeypatch.setattr(workers, "PostgresJoinLinkResolver", lambda *args: object())
    monkeypatch.setattr(sender_worker, "build_sender_worker", lambda *args, **kwargs: object())
    monkeypatch.setattr(services, "build_sender_transport", lambda *args: object())

    @asynccontextmanager
    async def singleton(*args):
        yield object()

    async def stop(*args):
        raise StopComposition

    monkeypatch.setattr(lease, "dedicated_advisory_lease", singleton)
    monkeypatch.setattr(services, "_serve_sender_singleton", stop)
    with pytest.raises(StopComposition):
        await services.run_sender_worker(original)
    assert repository_kwargs["expected_owner_config_revision"] == 15
    assert repository_kwargs["expected_staging_allowlist"] is None
    assert repository_kwargs["power_enabled"] is False
    assert repository_kwargs["power_map_notifications_enabled"] is enabled
    if enabled:
        guard_factory.assert_called_once_with(authority, original)
        assert repository_kwargs["power_map_authority_guard"] is final_guard
        verifier.assert_called_once_with("sender-worker", original, production_manifest=BASE)
    else:
        guard_factory.assert_not_called()
        verifier.assert_not_called()
        assert repository_kwargs["power_map_authority_guard"] is None
    client.session.close.assert_awaited_once()
    resource.dispose.assert_awaited_once()
