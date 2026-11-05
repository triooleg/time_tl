"""DB-only planner startup, authority/lease shutdown and commit fences without I/O."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tests.runtime.test_power_map_release import authority as fixture_authority
from time_tl.runtime import power_map_notifications as planner
from time_tl.runtime import power_map_service as module
from time_tl.runtime.contracts import RuntimeContractError

URL = "postgresql+asyncpg://fixture:fixture@127.0.0.1/map_fixture"


def settings(**extra):
    return module.MapPlannerSettings(
        _env_file=None,
        DATABASE_URL=URL,
        POWER_OUTAGES_ENABLED=True,
        POWER_ALERTS_ENERGY_API_ENABLED=True,
        POWER_MAP_REMINDERS_ENABLED=True,
        **extra,
    )


@pytest.fixture(autouse=True)
def no_http(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("planner must not construct an HTTP transport")

    monkeypatch.setattr("httpx.AsyncClient", forbidden)


def test_minimal_settings_have_no_transport_or_encryption_fields(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "DATABASE_URL=sqlite:///unexpected\nPOWER_OUTAGES_ENABLED=true\n"
    )
    value = module.MapPlannerSettings(_env_file=None, DATABASE_URL=URL)
    assert not value.power_outages_enabled and value.test_chat_allowlist == ()
    assert (
        not {
            "telegram_bot_token",
            "alerts_in_ua_token",
            "owner_alerts_fernet_key",
            "owner_telegram_fernet_key",
            "lesson_link_fernet_key",
        }
        & type(value).model_fields.keys()
    )
    assert not hasattr(value, "telegram_transport")


def test_environment_parses_revision_and_false_flags_without_a_project_dotenv(monkeypatch):
    monkeypatch.setenv("OWNER_CONFIG_REVISION", "15")
    monkeypatch.setenv("POWER_ADDRESS_LOOKUP_ENABLED", "false")
    value = settings()
    assert value.owner_config_revision == 15 and value.power_address_lookup_enabled is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("TIME_TL_ENV", "staging"),
        ("SERVICE_ROLE", "bot"),
        ("OWNER_CONFIG_SOURCE", "environment"),
        ("OWNER_CONFIG_REVISION", 16),
        ("POWER_ADDRESS_LOOKUP_ENABLED", True),
        ("DATABASE_URL", "sqlite:///bad"),
        ("DATABASE_URL", "postgresql+asyncpg://x:y@8.8.8.8/db"),
    ],
)
def test_settings_reject_nonproduction_scope_or_public_database(field, value):
    with pytest.raises(ValidationError):
        module.MapPlannerSettings(_env_file=None, **{"DATABASE_URL": URL, field: value})


@pytest.mark.parametrize(
    "name",
    [
        "TELEGRAM_BOT_TOKEN",
        "OWNER_ALERTS_FERNET_KEY",
        "OWNER_TELEGRAM_FERNET_KEY",
        "ALERTS_IN_UA_TOKEN",
        "ALERTS_ENERGY_API_KEY",
        "SIGNING_PRIVATE_KEY",
    ],
)
def test_planner_rejects_accidental_secret_inheritance(name):
    with pytest.raises(RuntimeContractError):
        module.validate_planner_environment({name: "synthetic-not-a-secret", "DATABASE_URL": URL})
    module.validate_planner_environment({"DATABASE_URL": URL, "PATH": "synthetic"})


def test_public_planner_rejects_staging_allowlist_environment():
    with pytest.raises(RuntimeContractError):
        module.validate_planner_environment({"TEST_CHAT_ALLOWLIST": "12345"})


class Engine:
    disposed = False

    async def dispose(self):
        self.disposed = True


class Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def begin(self):
        return self


async def test_startup_requires_signed_feature_and_public_database_scope(monkeypatch):
    authority = fixture_authority(monkeypatch)
    engine, calls = Engine(), []
    monkeypatch.setattr(module, "verify_feature_scope", lambda *args, **kwargs: authority)
    monkeypatch.setattr(module, "create_engine", lambda url: engine)
    monkeypatch.setattr(module, "create_session_factory", lambda engine: Session)

    async def validate(session, admitted, **kwargs):
        calls.append(admitted)

    monkeypatch.setattr(module, "validate_feature_database", validate)
    assert await module.validate_planner_startup(settings()) == authority
    assert calls == [authority] and engine.disposed

    async def changed(*args, **kwargs):
        raise RuntimeContractError("public metadata changed")

    engine.disposed = False
    monkeypatch.setattr(module, "validate_feature_database", changed)
    with pytest.raises(RuntimeContractError):
        await module.validate_planner_startup(settings())
    assert engine.disposed


async def test_disabled_capability_refuses_before_opening_database(monkeypatch):
    authority = fixture_authority(monkeypatch)
    disabled = replace(
        authority,
        effective_capabilities=tuple(
            (name, False) for name, flag in authority.effective_capabilities
        ),
    )
    monkeypatch.setattr(module, "verify_feature_scope", lambda *args, **kwargs: disabled)

    def forbidden(*args):
        raise AssertionError("disabled planner may not connect")

    monkeypatch.setattr(module, "create_engine", forbidden)
    with pytest.raises(RuntimeContractError):
        await module.validate_planner_startup(settings())


async def test_check_only_entrypoint_never_starts_background_roles(monkeypatch):
    authority = fixture_authority(monkeypatch)
    admitted, started = [], []
    configured = settings()
    monkeypatch.setattr(module.os, "environ", {})
    monkeypatch.setattr(module, "MapPlannerSettings", lambda **kwargs: configured)

    async def validate(value):
        admitted.append(value)
        return authority

    async def run(value, *, authority):
        started.append(authority)

    monkeypatch.setattr(module, "validate_planner_startup", validate)
    monkeypatch.setattr(planner, "run_map_planner", run)
    await module.run_public_map_service(check_only=True)
    assert len(admitted) == 1 and started == []
    await module.run_public_map_service()
    assert started == [authority]


class StopAfterOne(RuntimeError):
    pass


class Lease:
    def __init__(self, *, acquire=True, fail_after=None):
        self.admitted, self.fail_after = acquire, fail_after
        self.renewals, self.released = 0, False

    async def acquire(self):
        return self.admitted

    async def renew(self):
        self.renewals += 1
        return self.fail_after is None or self.renewals <= self.fail_after


async def runtime_fixture(monkeypatch, *, failure=None):
    authority = fixture_authority(monkeypatch)
    engine = Engine()
    state = SimpleNamespace(guard_calls=0, checks=0, effects=[], repository=None)
    lease = Lease(acquire=failure != "acquire", fail_after=4 if failure == "lease" else None)

    @asynccontextmanager
    async def lease_scope(*args):
        try:
            yield lease
        finally:
            lease.released = True

    monkeypatch.setattr(planner, "dedicated_advisory_lease", lease_scope)
    monkeypatch.setattr(planner, "create_engine", lambda url: engine)
    monkeypatch.setattr(planner, "create_session_factory", lambda engine: Session)
    verify_calls = []

    def verify(*args, **kwargs):
        verify_calls.append(True)
        if failure == "authority" and len(verify_calls) >= 5:
            return replace(authority, manifest_sha256="d" * 64)
        return authority

    monkeypatch.setattr(planner, "verify_feature_scope", verify)

    async def validate(session, admitted, *, lock=False):
        state.checks += 1
        if lock:
            state.guard_calls += 1
            if failure == "owner" and state.guard_calls >= 2:
                raise RuntimeContractError("owner changed before commit")

    monkeypatch.setattr(planner, "validate_feature_database", validate)

    class Repository:
        def __init__(self, factory, **kwargs):
            state.repository = self
            self.guard = kwargs["authority_guard"]
            assert kwargs["allowed_chat_ids"] is None

    class Worker:
        def __init__(self, repository):
            self.repository = repository

        async def run_once(self, now):
            before = list(state.effects)
            try:
                await self.repository.guard(Session(), now)
                state.effects.append("synthetic planning SQL")
                await self.repository.guard(Session(), now)
            except BaseException:
                state.effects[:] = before
                raise
            raise StopAfterOne("finished one guarded synthetic transaction")

    async def health(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(planner, "PostgresPowerMapNotificationRepository", Repository)
    monkeypatch.setattr(planner, "PowerNotificationWorker", Worker)
    monkeypatch.setattr(planner, "serve_health", health)
    return authority, engine, lease, state


async def test_planner_has_mandatory_authority_and_guards_each_commit(monkeypatch):
    authority, engine, lease, state = await runtime_fixture(monkeypatch)
    with pytest.raises(ExceptionGroup):
        await planner.run_map_planner(settings(), authority=authority)
    assert state.guard_calls == 2 and state.effects == ["synthetic planning SQL"]
    assert engine.disposed and lease.released


@pytest.mark.parametrize("failure", ["owner", "lease", "authority"])
async def test_owner_authority_or_lease_loss_rolls_back_and_exits(monkeypatch, failure):
    authority, engine, lease, state = await runtime_fixture(monkeypatch, failure=failure)
    with pytest.raises(ExceptionGroup):
        await planner.run_map_planner(settings(), authority=authority)
    assert state.effects == [] and engine.disposed and lease.released


async def test_unavailable_lease_starts_no_planning(monkeypatch):
    authority, engine, lease, state = await runtime_fixture(monkeypatch, failure="acquire")
    with pytest.raises(RuntimeError):
        await planner.run_map_planner(settings(), authority=authority)
    assert state.effects == [] and state.repository is None and engine.disposed and lease.released


async def test_direct_runtime_without_authority_is_not_a_bypass(monkeypatch):
    with pytest.raises(TypeError):
        await planner.run_map_planner(settings())
    with pytest.raises(RuntimeContractError):
        await planner.run_map_planner(settings(), authority=None)


async def test_final_callback_uses_db_metadata_lock_and_rejects_changed_authority(monkeypatch):
    from tests.runtime.test_power_map_release import (
        Session as PublicSession,
    )
    from tests.runtime.test_power_map_release import (
        settings as raw_settings,
    )
    from time_tl.runtime import power_map_release as release

    authority = fixture_authority(monkeypatch)
    monkeypatch.setattr(release, "verify_feature_scope", lambda *args, **kwargs: authority)
    guard = release.feature_authority_guard(authority, raw_settings("map-planner"))
    session = PublicSession()
    await guard(session, datetime.now(UTC))
    assert session.queries[0].endswith("FOR UPDATE")
    monkeypatch.setattr(
        release,
        "verify_feature_scope",
        lambda *args, **kwargs: replace(authority, manifest_sha256="d" * 64),
    )
    with pytest.raises(RuntimeContractError):
        await guard(PublicSession(), datetime.now(UTC))
