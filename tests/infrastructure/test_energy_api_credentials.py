"""Synthetic credential tests; opt-in native PG target is strictly isolated."""

import asyncio
import base64
import copy
import json
import os
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import pytest
from pydantic import SecretStr
from sqlalchemy import delete, select, text

from time_tl.infrastructure.db.energy_api_credentials import (
    SETTING_KEY,
    EnergyApiCheckResult,
    EnergyApiCheckStatus,
    EnergyApiConflict,
    EnergyApiCooldown,
    EnergyApiCredentialCodec,
    EnergyApiUnavailable,
    PostgresEnergyApiCredentials,
)
from time_tl.infrastructure.db.engine import create_engine, create_session_factory
from time_tl.infrastructure.db.models import AuditLog, RuntimeSetting

SEED = SecretStr(base64.urlsafe_b64encode(b"A" * 32).decode())
KEY = "synthetic-api-key-only-for-offline-tests"
NOW = datetime(2026, 11, 4, 16, tzinfo=UTC)


class MemoryFactory:
    def __init__(self):
        self.row = None
        self.audits = []
        self.lock = asyncio.Lock()

    def __call__(self):
        return MemorySession(self)


class MemorySession:
    def __init__(self, factory):
        self.factory = factory

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def begin(self):
        return MemoryTransaction(self.factory)

    async def get(self, model, key):
        assert model is RuntimeSetting and key == SETTING_KEY
        return self.factory.row

    def add(self, audit):
        self.factory.audits.append(audit)


class MemoryTransaction:
    def __init__(self, factory):
        self.factory = factory

    async def __aenter__(self):
        await self.factory.lock.acquire()
        self.before = copy.deepcopy(self.factory.row.value) if self.factory.row else None

    async def __aexit__(self, kind, *args):
        if kind is not None and self.factory.row is not None and self.before is not None:
            self.factory.row.value = self.before
        self.factory.lock.release()


class MemoryCredentials(PostgresEnergyApiCredentials):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("clock", lambda: NOW)
        super().__init__(*args, **kwargs)

    @staticmethod
    async def _locked(session):
        from time_tl.infrastructure.db.energy_api_credentials import _empty

        if session.factory.row is None:
            session.factory.row = RuntimeSetting(
                key=SETTING_KEY, value=_empty(), updated_by="system"
            )
        return session.factory.row


class Checker:
    def __init__(self, result=None):
        self.calls = 0
        self.result = result or EnergyApiCheckResult(EnergyApiCheckStatus.VALID)

    async def check(self, key):
        assert isinstance(key, SecretStr)
        self.calls += 1
        return self.result


def test_codec_restart_wrong_seed_and_purpose_binding():
    codec = EnergyApiCredentialCodec(SEED)
    ciphertext = codec.encrypt(SecretStr(KEY))
    assert KEY not in ciphertext and KEY not in repr(codec)
    assert EnergyApiCredentialCodec(SEED).decrypt(ciphertext).get_secret_value() == KEY
    wrong = EnergyApiCredentialCodec(base64.urlsafe_b64encode(b"B" * 32).decode())
    with pytest.raises(EnergyApiUnavailable, match="energy_api_unavailable"):
        wrong.decrypt(ciphertext)
    forged = codec._cipher.encrypt(
        json.dumps({"purpose": "another-service", "version": 1, "api_key": KEY}).encode()
    ).decode()
    with pytest.raises(EnergyApiUnavailable):
        codec.decrypt(forged)
    with pytest.raises(EnergyApiUnavailable):
        EnergyApiCredentialCodec("")


@pytest.mark.parametrize("key", ["", "with space", "a\n", "ї", "a" * 4097])
def test_invalid_key_fails_without_echo(key):
    with pytest.raises(EnergyApiUnavailable) as error:
        EnergyApiCredentialCodec(SEED).encrypt(SecretStr(key))
    assert str(error.value) == "energy_api_unavailable"


async def test_save_restart_cas_clear_and_redacted_metadata():
    factory = MemoryFactory()
    checker = Checker()
    repo = MemoryCredentials(factory, SEED, checker)
    assert not (await repo.status(NOW)).configured
    saved = await repo.save(SecretStr(KEY), expected_revision=0, actor_id="admin", now=NOW)
    assert saved.configured and saved.revision == 1 and not checker.calls
    assert KEY not in json.dumps(factory.row.value)
    assert KEY not in repr(saved)
    assert all(set(a.redacted_details) == {"revision"} for a in factory.audits)
    restarted = MemoryCredentials(factory, SEED, checker)
    assert (await restarted.load()).get_secret_value() == KEY
    results = await asyncio.gather(
        *[
            restarted.save(SecretStr(KEY + str(i)), expected_revision=1, actor_id="admin", now=NOW)
            for i in range(2)
        ],
        return_exceptions=True,
    )
    assert sum(isinstance(x, EnergyApiConflict) for x in results) == 1
    cleared = await restarted.clear(expected_revision=2, actor_id="admin", now=NOW)
    assert not cleared.configured and await restarted.load() is None


async def test_checks_cooldown_retry_after_persisted_and_no_raw_failure():
    factory = MemoryFactory()
    checker = Checker(EnergyApiCheckResult(EnergyApiCheckStatus.RATE_LIMITED, 3600))
    repo = MemoryCredentials(factory, SEED, checker)
    await repo.save(SecretStr(KEY), expected_revision=0, actor_id="admin", now=NOW)
    status = await repo.check(expected_revision=1, actor_id="admin", now=NOW)
    assert status.check_status is EnergyApiCheckStatus.RATE_LIMITED
    with pytest.raises(EnergyApiCooldown):
        await MemoryCredentials(factory, SEED, checker).check(
            expected_revision=1, actor_id="admin", now=NOW + timedelta(seconds=61)
        )
    assert checker.calls == 1
    await repo.save(SecretStr(KEY), expected_revision=1, actor_id="admin", now=NOW)
    await repo.clear(expected_revision=2, actor_id="admin", now=NOW)
    await repo.save(SecretStr(KEY), expected_revision=3, actor_id="admin", now=NOW)
    with pytest.raises(EnergyApiCooldown):
        await repo.check(expected_revision=4, actor_id="admin", now=NOW + timedelta(seconds=61))

    class BadChecker:
        async def check(self, key):
            raise RuntimeError(KEY)

    status = await MemoryCredentials(factory, SEED, BadChecker()).check(
        expected_revision=4, actor_id="admin", now=NOW + timedelta(hours=2)
    )
    assert status.check_status is EnergyApiCheckStatus.UNAVAILABLE
    assert KEY not in repr(status) + json.dumps(factory.row.value)


async def test_check_releases_locks_and_does_not_apply_result_after_rotation():
    started, finish = asyncio.Event(), asyncio.Event()

    class BlockingChecker:
        async def check(self, key):
            started.set()
            await finish.wait()
            return EnergyApiCheckResult(EnergyApiCheckStatus.RATE_LIMITED, 3600)

    factory = MemoryFactory()
    repo = MemoryCredentials(
        factory, SEED, BlockingChecker(), clock=lambda: NOW + timedelta(seconds=7)
    )
    await repo.save(SecretStr(KEY), expected_revision=0, actor_id="admin", now=NOW)
    running = asyncio.create_task(repo.check(expected_revision=1, actor_id="admin", now=NOW))
    await started.wait()
    await asyncio.wait_for(
        repo.save(SecretStr(KEY + "-new"), expected_revision=1, actor_id="admin", now=NOW),
        timeout=1,
    )
    finish.set()
    with pytest.raises(EnergyApiConflict):
        await running
    current = await repo.status(NOW)
    assert current.revision == 2 and current.check_status is EnergyApiCheckStatus.NOT_CHECKED
    assert factory.row.value["next_check_at"] == (NOW + timedelta(seconds=3607)).isoformat()
    with pytest.raises(EnergyApiCooldown):
        await repo.check(expected_revision=2, actor_id="admin", now=NOW + timedelta(seconds=61))


@pytest.mark.parametrize("retry", [-1, 86401, True, float("nan"), float("inf")])
def test_retry_after_bounds(retry):
    with pytest.raises(EnergyApiUnavailable):
        EnergyApiCheckResult(EnergyApiCheckStatus.RATE_LIMITED, retry)


async def test_native_isolated_postgres_credentials_restart_cas_and_audit():
    url = os.environ.get("ENERGY_API_TEST_DATABASE_URL")
    if not url:
        pytest.skip("opt-in isolated native PostgreSQL")
    parsed = urlsplit(url)
    assert (
        parsed.scheme == "postgresql+asyncpg"
        and parsed.hostname in {"127.0.0.1", "localhost"}
        and parsed.port is not None
        and parsed.port >= 1024
        and parsed.username == "energycredentialcheck"
        and parsed.password is None
        and parsed.path == "/energycredentialcheck"
        and not parsed.query
        and not parsed.fragment
    )
    engine = create_engine(url)
    factory = create_session_factory(engine)
    try:
        async with engine.connect() as connection:
            tables = (
                (
                    await connection.execute(
                        text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
                    )
                )
                .scalars()
                .all()
            )
        if not tables:
            from alembic import command
            from alembic.config import Config

            config = Config("alembic.ini")
            config.set_main_option("sqlalchemy.url", url)
            await asyncio.to_thread(command.upgrade, config, "head")
        async with factory() as session:
            assert (
                await session.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one() == "0011_power_outages"
            assert await session.get(RuntimeSetting, SETTING_KEY) is None
        checker = Checker(EnergyApiCheckResult(EnergyApiCheckStatus.RATE_LIMITED, 1200))
        repo = PostgresEnergyApiCredentials(factory, SEED, checker, clock=lambda: NOW)
        await repo.save(SecretStr(KEY), expected_revision=0, actor_id="admin", now=NOW)
        async with factory() as session:
            stored = await session.get(RuntimeSetting, SETTING_KEY)
            assert KEY not in json.dumps(stored.value)
        restarted = PostgresEnergyApiCredentials(factory, SEED, checker, clock=lambda: NOW)
        assert (await restarted.load()).get_secret_value() == KEY
        results = await asyncio.gather(
            *[
                restarted.save(
                    SecretStr(KEY + str(i)), expected_revision=1, actor_id="admin", now=NOW
                )
                for i in range(2)
            ],
            return_exceptions=True,
        )
        assert sum(isinstance(x, EnergyApiConflict) for x in results) == 1
        checked = await restarted.check(expected_revision=2, actor_id="admin", now=NOW)
        assert checked.check_status is EnergyApiCheckStatus.RATE_LIMITED
        with pytest.raises(EnergyApiCooldown):
            await restarted.check(
                expected_revision=2, actor_id="admin", now=NOW + timedelta(seconds=61)
            )
        async with factory() as session:
            audits = (
                (
                    await session.execute(
                        select(AuditLog).where(AuditLog.target_type == "energy_api_credentials")
                    )
                )
                .scalars()
                .all()
            )
            assert audits and all(set(a.redacted_details) == {"revision"} for a in audits)
            assert KEY not in repr([a.redacted_details for a in audits])
        started, finish = asyncio.Event(), asyncio.Event()

        class RotatingChecker:
            async def check(self, key):
                started.set()
                await finish.wait()
                return EnergyApiCheckResult(EnergyApiCheckStatus.RATE_LIMITED, 3600)

        later = NOW + timedelta(hours=2)
        racing = PostgresEnergyApiCredentials(
            factory, SEED, RotatingChecker(), clock=lambda: later + timedelta(seconds=7)
        )
        running = asyncio.create_task(
            racing.check(expected_revision=2, actor_id="admin", now=later)
        )
        await started.wait()
        await asyncio.wait_for(
            racing.save(
                SecretStr(KEY + "rotated"), expected_revision=2, actor_id="admin", now=later
            ),
            timeout=2,
        )
        finish.set()
        with pytest.raises(EnergyApiConflict):
            await running
        current = await racing.status(later)
        assert current.revision == 3 and current.check_status is EnergyApiCheckStatus.NOT_CHECKED
        with pytest.raises(EnergyApiCooldown):
            await racing.check(
                expected_revision=3, actor_id="admin", now=later + timedelta(seconds=61)
            )
    finally:
        async with factory() as session, session.begin():
            await session.execute(delete(RuntimeSetting).where(RuntimeSetting.key == SETTING_KEY))
            await session.execute(
                delete(AuditLog).where(AuditLog.target_type == "energy_api_credentials")
            )
        await engine.dispose()
