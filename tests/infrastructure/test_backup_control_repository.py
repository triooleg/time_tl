from __future__ import annotations

import asyncio
import copy
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from time_tl.infrastructure.db.admin import (
    AsyncPostgresAdminRepository,
    BackupControlConflict,
)
from time_tl.infrastructure.db.models import AuditLog, RuntimeSetting

NOW = datetime(2026, 10, 2, 20, 0, tzinfo=UTC)
IMAGE = "localhost:5000/time-tl-bot@sha256:" + "a" * 64


class MemoryDatabase:
    """Transactional test double, including absent-row lock and rollback."""

    def __init__(self) -> None:
        self.values = {
            "owner_configuration_state": {"active_revision": 14},
            "owner_configuration_active": {"revision": 14},
        }
        self.audits: list[AuditLog] = []
        self.lock = asyncio.Lock()
        self.statements: list[str] = []
        self.fail_audit = False
        self.sessions = 0

    def __call__(self):
        self.sessions += 1
        return MemorySession(self)

    def repository(self):
        return AsyncPostgresAdminRepository(self, ip_hash_key=b"x" * 32)


class Transaction:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self

    async def __aexit__(self, kind, *_):
        session = self.session
        if session.locked:
            if kind is None:
                for key, row in session.rows.items():
                    session.database.values[key] = copy.deepcopy(row.value)
                session.database.audits.extend(session.audits)
            session.database.lock.release()
            session.locked = False


class MemorySession:
    def __init__(self, database):
        self.database = database
        self.rows = {}
        self.audits = []
        self.locked = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    def begin(self):
        return Transaction(self)

    async def execute(self, statement):
        sql = str(statement)
        self.database.statements.append(sql)
        assert "pg_advisory_xact_lock" in sql
        if not self.locked:
            await self.database.lock.acquire()
            self.locked = True
        await asyncio.sleep(0)

    async def get(self, model, key):
        assert model is RuntimeSetting
        if key in self.rows:
            return self.rows[key]
        if key not in self.database.values:
            return None
        row = RuntimeSetting(
            key=key, value=copy.deepcopy(self.database.values[key]), updated_by="admin"
        )
        self.rows[key] = row
        return row

    def add(self, row):
        if isinstance(row, AuditLog):
            if self.database.fail_audit:
                raise RuntimeError("injected database audit failure")
            self.audits.append(row)
        else:
            assert isinstance(row, RuntimeSetting)
            self.rows[row.key] = row


async def request(repository, **overrides):
    args = {
        "kind": "verify",
        "artifact_id": "b" * 32,
        "catalogue_version": "c" * 64,
        "expected_active_revision": 14,
        "expected_image": IMAGE,
        "expected_version": 0,
        "actor": "admin",
        "now": NOW,
    }
    args.update(overrides)
    return await repository.request_backup_operation(**args)


async def test_defaults_are_readonly_and_policy_cas_preserves_request():
    db = MemoryDatabase()
    repo = db.repository()
    assert await repo.get_backup_control() == {"version": 0, "retention_days": 7, "request": None}
    assert "backup_control" not in db.values
    control = await request(repo)
    saved = await repo.set_backup_policy(10, 1, "admin")
    assert saved["version"] == 2 and saved["retention_days"] == 10
    assert saved["request"] == control["request"]
    with pytest.raises(BackupControlConflict):
        await repo.set_backup_policy(5, 1, "admin")
    assert (await repo.get_backup_control())["version"] == 2
    assert any("hashtext('time_tl:backup_control')::bigint" in sql for sql in db.statements)


async def test_concurrent_first_write_serializes_absent_row():
    db = MemoryDatabase()
    results = await asyncio.gather(
        db.repository().set_backup_policy(7, 0, "admin"),
        db.repository().set_backup_policy(8, 0, "admin"),
        return_exceptions=True,
    )
    assert sum(isinstance(value, BackupControlConflict) for value in results) == 1
    assert db.values["backup_control"]["version"] == 1
    assert len(db.audits) == 1


@pytest.mark.parametrize("days", [True, 0, 91, "7", 7.5, None])
async def test_policy_invalid_before_sql(days):
    db = MemoryDatabase()
    with pytest.raises(BackupControlConflict):
        await db.repository().set_backup_policy(days, 0, "admin")
    assert db.sessions == 0


@pytest.mark.parametrize(
    "override",
    [
        {"kind": "destroy"},
        {"kind": []},
        {"artifact_id": "../file"},
        {"artifact_id": "A" * 32},
        {"catalogue_version": "wrong"},
        {"expected_active_revision": True},
        {"expected_active_revision": 0},
        {"expected_version": -1},
        {"expected_image": "localhost:5000/time-tl-bot:latest"},
        {"expected_image": "x\n@sha256:" + "a" * 64},
        {"actor": "admin\nsecret"},
        {"now": NOW.replace(tzinfo=None)},
    ],
)
async def test_request_invalid_before_sql(override):
    db = MemoryDatabase()
    with pytest.raises(BackupControlConflict):
        await request(db.repository(), **override)
    assert db.sessions == 0


async def test_request_revision_binding_and_no_pending_overwrite():
    db = MemoryDatabase()
    repo = db.repository()
    with pytest.raises(BackupControlConflict):
        await request(repo, expected_active_revision=13)
    control = await request(repo)
    operation = control["request"]
    assert len(operation["request_id"]) == 32
    assert datetime.fromisoformat(operation["expires_at"]) - NOW == timedelta(minutes=15)
    for now in (NOW, NOW + timedelta(hours=1)):
        with pytest.raises(BackupControlConflict):
            await request(repo, expected_version=1, now=now)
    assert (await repo.get_backup_control())["request"] == operation


async def test_claim_expiry_boundary_doubleclaim_and_revision_change():
    db = MemoryDatabase()
    repo = db.repository()
    op = (await request(repo))["request"]
    for now in (NOW - timedelta(seconds=1), NOW + timedelta(minutes=15)):
        with pytest.raises(BackupControlConflict):
            await repo.claim_backup_operation(op["request_id"], now)
    db.values["owner_configuration_state"]["active_revision"] = 15
    with pytest.raises(BackupControlConflict):
        await repo.claim_backup_operation(op["request_id"], NOW)
    db.values["owner_configuration_state"]["active_revision"] = 14
    claimed = await repo.claim_backup_operation(op["request_id"], NOW)
    assert claimed["status"] == "CLAIMED"
    assert (await repo.get_backup_control())["version"] == 2
    with pytest.raises(BackupControlConflict):
        await repo.claim_backup_operation(op["request_id"], NOW)


async def test_finish_sanitizes_result_and_audits_no_arbitrary_values():
    db = MemoryDatabase()
    repo = db.repository()
    op = (await request(repo))["request"]
    await repo.claim_backup_operation(op["request_id"], NOW)
    control = await repo.finish_backup_operation(
        op["request_id"],
        "PASS",
        {
            "artifact_sha256": "d" * 64,
            "verified_at": NOW.isoformat(),
            "schema": "0008_homework_foundation",
            "size_bytes": 12,
            "table_count": 28,
            "reason_code": "VERIFY_PASSED",
            "password": "sensitive-do-not-persist",
            "raw_error": "postgresql://sensitive-do-not-persist",
        },
        NOW,
    )
    assert control["version"] == 3 and control["request"]["status"] == "PASS"
    assert "sensitive-do-not-persist" not in str(db.values)
    assert "sensitive-do-not-persist" not in str([audit.redacted_details for audit in db.audits])
    assert all("artifact_id" not in audit.redacted_details for audit in db.audits)
    with pytest.raises(BackupControlConflict):
        await repo.finish_backup_operation(op["request_id"], "PASS", {}, NOW)
    await request(repo, expected_version=3)


@pytest.mark.parametrize("kind,status", [("restore", "PASS"), ("verify", "REQUIRES_OPERATOR")])
async def test_kind_cannot_finish_with_incompatible_status(kind, status):
    db = MemoryDatabase()
    repo = db.repository()
    op = (await request(repo, kind=kind))["request"]
    await repo.claim_backup_operation(op["request_id"], NOW)
    with pytest.raises(BackupControlConflict):
        await repo.finish_backup_operation(op["request_id"], status, {}, NOW)
    assert (await repo.get_backup_control())["request"]["status"] == "CLAIMED"


async def test_restore_selection_preserved_until_explicit_owner_cancel():
    db = MemoryDatabase()
    repo = db.repository()
    op = (await request(repo, kind="restore"))["request"]
    await repo.claim_backup_operation(op["request_id"], NOW)
    with pytest.raises(BackupControlConflict):
        await repo.cancel_backup_operation(op["request_id"], 2, "admin", NOW)
    await repo.finish_backup_operation(op["request_id"], "REQUIRES_OPERATOR", {}, NOW)
    with pytest.raises(BackupControlConflict):
        await request(repo, expected_version=3, now=NOW + timedelta(days=1))
    with pytest.raises(BackupControlConflict):
        await repo.finish_backup_operation(op["request_id"], "REJECTED", {}, NOW)
    with pytest.raises(BackupControlConflict):
        await repo.cancel_backup_operation(op["request_id"], 2, "admin", NOW)
    control = await repo.cancel_backup_operation(op["request_id"], 3, "admin", NOW)
    assert control["request"]["result"] == {"reason_code": "CANCELLED"}
    await request(repo, expected_version=4)


async def test_pending_reject_and_cancel_free_operation_without_claim():
    db = MemoryDatabase()
    repo = db.repository()
    op = (await request(repo))["request"]
    with pytest.raises(BackupControlConflict):
        await repo.finish_backup_operation(op["request_id"], "PASS", {}, NOW)
    await repo.finish_backup_operation(
        op["request_id"], "REJECTED", {"reason_code": "EXPIRED"}, NOW
    )
    op = (await request(repo, expected_version=2))["request"]
    await repo.cancel_backup_operation(op["request_id"], 3, "admin", NOW)


async def test_audit_failure_rolls_back_state_and_retry_works():
    db = MemoryDatabase()
    repo = db.repository()
    db.fail_audit = True
    with pytest.raises(RuntimeError):
        await repo.set_backup_policy(20, 0, "admin")
    assert "backup_control" not in db.values and not db.audits
    db.fail_audit = False
    assert (await repo.set_backup_policy(20, 0, "admin"))["version"] == 1


@pytest.mark.parametrize(
    "result",
    [
        {"artifact_sha256": "token"},
        {"verified_at": "not-a-time"},
        {"size_bytes": True},
        {"schema": "password-secret"},
        {"reason_code": "arbitrary-secret"},
        {"reason_code": []},
        {"table_count": 10001},
    ],
)
async def test_invalid_result_rejected_before_sql(result):
    db = MemoryDatabase()
    with pytest.raises(BackupControlConflict):
        await db.repository().finish_backup_operation("a" * 32, "PASS", result, NOW)
    assert db.sessions == 0


@pytest.mark.parametrize(
    "value", [[], {}, {"version": True, "retention_days": 7}, {"version": 0, "retention_days": 0}]
)
async def test_corrupt_control_fails_closed(value):
    db = MemoryDatabase()
    db.values["backup_control"] = value
    with pytest.raises(BackupControlConflict):
        await db.repository().get_backup_control()
    with pytest.raises(BackupControlConflict):
        await db.repository().set_backup_policy(7, 0, "admin")
    assert db.values["backup_control"] == value


async def test_generic_settings_cannot_bypass_backup_cas():
    db = MemoryDatabase()
    with pytest.raises(BackupControlConflict):
        await db.repository().set_settings({"backup_control": {}}, "admin", "anything")
    assert db.sessions == 0


@pytest.mark.parametrize(
    "change",
    [
        {"kind": []},
        {"status": []},
        {"status": "CLAIMED"},
        {"status": "PASS"},
        {"status": "REJECTED"},
        {"kind": "verify", "status": "REQUIRES_OPERATOR"},
        {"expires_at": (NOW + timedelta(minutes=16)).isoformat()},
        {"claimed_at": NOW.isoformat()},
        {"finished_at": NOW.isoformat()},
    ],
)
async def test_corrupt_lifecycle_cannot_be_read_or_mutated(change):
    db = MemoryDatabase()
    repo = db.repository()
    await request(repo)
    db.values["backup_control"]["request"].update(change)
    with pytest.raises(BackupControlConflict):
        await repo.get_backup_control()
    with pytest.raises(BackupControlConflict):
        await repo.set_backup_policy(7, 1, "admin")


async def test_read_only_projection_drops_unknown_fields_without_mutating_storage():
    db = MemoryDatabase()
    repo = db.repository()
    await request(repo)
    db.values["backup_control"]["secret"] = "not-in-public-result"
    db.values["backup_control"]["request"]["secret"] = "not-in-public-result"
    projection = await repo.get_backup_control()
    assert "not-in-public-result" not in str(projection)
    assert "secret" in db.values["backup_control"]


async def test_actual_postgres_cas_claim_and_transaction_rollback():
    """Opt-in ONLY for the disposable network-none PostgreSQL integration fixture."""
    database_url = os.environ.get("BACKUP_CONTROL_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("requires an explicitly isolated disposable PostgreSQL fixture")
    url = make_url(database_url)
    if (
        url.drivername != "postgresql+asyncpg"
        or url.host != "127.0.0.1"
        or url.database != "backupcheck"
        or url.username != "backupcheck"
        or url.password is not None
    ):
        pytest.fail("integration fixture must be loopback trust-only backupcheck database")
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    repo = AsyncPostgresAdminRepository(factory, ip_hash_key=b"x" * 32)
    try:
        async with engine.begin() as connection:
            # This dedicated disposable DB contains only the two test repository tables.
            await connection.run_sync(RuntimeSetting.__table__.create)
            await connection.run_sync(AuditLog.__table__.create)
        async with factory() as session, session.begin():
            session.add_all(
                [
                    RuntimeSetting(
                        key="owner_configuration_state",
                        value={"active_revision": 14},
                        updated_by="admin",
                    ),
                    RuntimeSetting(
                        key="owner_configuration_active", value={"revision": 14}, updated_by="admin"
                    ),
                ]
            )
        results = await asyncio.gather(
            *(repo.set_backup_policy(7, 0, "admin") for _ in range(10)),
            return_exceptions=True,
        )
        assert sum(isinstance(value, BackupControlConflict) for value in results) == 9
        assert (await repo.get_backup_control())["version"] == 1
        operation = (await request(repo, expected_version=1))["request"]
        claims = await asyncio.gather(
            *(repo.claim_backup_operation(operation["request_id"], NOW) for _ in range(10)),
            return_exceptions=True,
        )
        assert sum(isinstance(value, BackupControlConflict) for value in claims) == 9
        assert (await repo.get_backup_control())["version"] == 3
        await repo.finish_backup_operation(operation["request_id"], "PASS", {}, NOW)

        class AbortRepository(AsyncPostgresAdminRepository):
            async def _save_backup_control(self, session, control, actor, action):
                await super()._save_backup_control(session, control, actor, action)
                await session.flush()  # Real writes followed by rollback, not a fake adapter.
                raise RuntimeError("injected rollback")

        abort = AbortRepository(factory, ip_hash_key=b"x" * 32)
        with pytest.raises(RuntimeError):
            await abort.set_backup_policy(20, 4, "admin")
        control = await repo.get_backup_control()
        assert control["version"] == 4 and control["retention_days"] == 7
        async with factory() as session:
            assert await session.scalar(text("SELECT count(*) FROM audit_log")) == 4
        assert (await repo.set_backup_policy(8, 4, "admin"))["version"] == 5
    finally:
        await engine.dispose()
