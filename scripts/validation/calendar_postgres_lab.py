"""Destructive tests ONLY in an empty, isolated, named disposable PostgreSQL lab.

Run from the immutable application candidate; no production environment or mount
is needed. URL is explicit, guarded, trust-only, never read from environment.
stdout is one bounded JSON receipt. Failure details/SQL/tokens are never printed.
The operator destroys the lab containers/network after collecting evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import delete, func, inspect, select, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from time_tl.domain.calendar_memberships import (
    CalendarConflict,
    CalendarLimitExceeded,
    CalendarRole,
    CalendarSummary,
)
from time_tl.domain.calendars import CalendarKind
from time_tl.domain.schedule import Lesson, ScheduleProfile, ScheduleRule
from time_tl.infrastructure.db.calendars import (
    AsyncPostgresCalendarRepository,
    digest_invitation_token,
)
from time_tl.infrastructure.db.models import Calendar, CalendarInvitation, CalendarMembership, User

LAB_HOST = "time-tl-calendar-lab-pg"
LAB_DATABASE = "calendar_lab"
LAB_USER = "calendar_lab"
HEAD = "0009_calendar_foundation"
PREVIOUS = "0008_homework_foundation"
EXPECTED_TABLES = 31  # 30 ORM tables plus alembic_version.
NOW = datetime(2026, 10, 5, 6, 35, tzinfo=UTC)


class LabFailure(RuntimeError):
    """Only a fixed reason code leaves the harness."""


class RollbackProbe(Exception):
    pass


def guard_database_url(value: str) -> str:
    try:
        url = make_url(value)
        accepted = (
            url.drivername == "postgresql+asyncpg"
            and url.host == LAB_HOST
            and url.database == LAB_DATABASE
            and url.username == LAB_USER
            and url.password is None
            and url.port in (None, 5432)
            and not url.query
        )
    except Exception:
        raise LabFailure("TARGET_GUARD") from None
    if not accepted or "#" in value:
        raise LabFailure("TARGET_GUARD")
    return url.render_as_string(hide_password=True)


def migration_config(workspace: Path, url: str) -> Config:
    ini, migrations = workspace / "alembic.ini", workspace / "migrations"
    if not ini.is_file() or not (migrations / "env.py").is_file():
        raise LabFailure("MIGRATIONS_UNAVAILABLE")
    config = Config(str(ini))
    config.set_main_option("script_location", str(migrations))
    config.set_main_option("sqlalchemy.url", guard_database_url(url))
    config.set_section_option("logger_alembic", "level", "ERROR")
    return config


def profile(name: str = "Lab lesson") -> ScheduleProfile:
    return ScheduleProfile(
        profile_id="lab-only",
        name=name,
        rules=(
            ScheduleRule(
                weekday=0,
                lessons=(Lesson(time(9), time(9, 45), name),),
            ),
        ),
    )


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise LabFailure(reason)


async def version_and_tables(engine: AsyncEngine) -> tuple[str, list[str]]:
    async with engine.connect() as connection:
        version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        tables = await connection.run_sync(lambda bind: inspect(bind).get_table_names())
        return str(version), sorted(tables)


def validate_revision(connection: Connection) -> None:
    module = importlib.import_module("migrations.versions.0009_calendar_foundation")
    operations = Operations(MigrationContext.configure(connection))
    with (
        patch.object(module, "op", operations),
        patch.object(
            module.context,
            "is_offline_mode",
            return_value=False,
        ),
    ):
        module.upgrade()


async def mismatch_probe(engine: AsyncEngine, statements: tuple[str, ...]) -> None:
    """Every intentionally broken schema is rolled back, including probe DDL."""
    rejected = False
    try:
        async with engine.begin() as connection:
            for statement in statements:
                await connection.execute(text(statement))
            try:
                await connection.run_sync(validate_revision)
            except RuntimeError:
                rejected = True
            raise RollbackProbe()
    except RollbackProbe:
        pass
    require(rejected, "REFLECTED_SCHEMA_FALSE_PASS")
    async with engine.connect() as connection:
        await connection.run_sync(validate_revision)


async def run_lab(url: str, workspace: Path) -> dict[str, Any]:
    safe_url = guard_database_url(url)
    config = migration_config(workspace, safe_url)
    engine = create_async_engine(safe_url, pool_size=20, max_overflow=0, echo=False)
    checks: list[str] = []
    try:
        async with engine.connect() as connection:
            identity = (
                await connection.execute(text("SELECT current_database(), current_user"))
            ).one()
            require(tuple(identity) == (LAB_DATABASE, LAB_USER), "LAB_IDENTITY_MISMATCH")
            tables = await connection.run_sync(lambda bind: inspect(bind).get_table_names())
            schemas = (
                await connection.execute(
                    text(
                        "SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' "
                        "AND nspname NOT IN ('public','information_schema')"
                    )
                )
            ).all()
            require(not tables and not schemas, "LAB_NOT_EMPTY")
        checks.append("empty_named_disposable_target")

        # env.py uses asyncio.run: a separate thread runs its real Alembic path.
        await asyncio.to_thread(command.upgrade, config, HEAD)
        head, tables = await version_and_tables(engine)
        require(head == HEAD and len(tables) == EXPECTED_TABLES, "FRESH_BOOTSTRAP_FAILED")
        checks.append("fresh_alembic_bootstrap_current_metadata")
        await asyncio.to_thread(command.downgrade, config, PREVIOUS)
        previous, tables = await version_and_tables(engine)
        require(previous == PREVIOUS and len(tables) == EXPECTED_TABLES - 3, "LAB_DOWNGRADE_FAILED")
        require(
            not {"calendars", "calendar_memberships", "calendar_invitations"}.intersection(tables),
            "LAB_0008_NOT_ISOLATED",
        )
        await asyncio.to_thread(command.upgrade, config, HEAD)
        head, tables = await version_and_tables(engine)
        require(head == HEAD and len(tables) == EXPECTED_TABLES, "FORWARD_MIGRATION_FAILED")
        checks.append("real_0008_to_0009_forward_migration")

        async with engine.connect() as connection:
            await connection.run_sync(validate_revision)
        checks.append("real_postgres_schema_reflection")
        await mismatch_probe(engine, ("DROP TABLE calendar_invitations",))
        await mismatch_probe(
            engine,
            (
                "ALTER TABLE calendars DROP CONSTRAINT ck_calendars_kind_allowed",
                "ALTER TABLE calendars ADD CONSTRAINT ck_calendars_kind_allowed "
                "CHECK (kind IN ('school','PERSONAL','ACTIVITY'))",
            ),
        )
        await mismatch_probe(
            engine,
            (
                "DROP INDEX ix_calendars_owner_user_id",
                "CREATE INDEX ix_calendars_owner_user_id "
                "ON calendars(owner_user_id) WHERE owner_visible",
            ),
        )
        await mismatch_probe(
            engine,
            (
                "CREATE SCHEMA calendar_lab_shadow",
                "CREATE TABLE calendar_lab_shadow.users (id uuid PRIMARY KEY)",
                "ALTER TABLE calendars DROP CONSTRAINT fk_calendars_owner_user_id_users",
                "ALTER TABLE calendars ADD CONSTRAINT fk_calendars_owner_user_id_users "
                "FOREIGN KEY(owner_user_id) REFERENCES calendar_lab_shadow.users(id) "
                "ON DELETE CASCADE",
            ),
        )
        checks.append("partial_check_index_foreign_schema_mismatch_rejected")

        factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with factory() as session, session.begin():
            session.add_all(
                [User(id=uuid4(), telegram_user_id=identity) for identity in range(1, 40)]
            )
        repo = AsyncPostgresCalendarRepository(factory)
        results = await asyncio.gather(
            *(repo.create_calendar(1, profile(), CalendarKind.PERSONAL) for _ in range(20)),
            return_exceptions=True,
        )
        require(
            sum(isinstance(result, CalendarSummary) for result in results) == 11
            and sum(isinstance(result, CalendarLimitExceeded) for result in results) == 9,
            "CONCURRENT_QUOTA_FAILED",
        )
        for summary in await repo.list_calendars(1):
            await repo.set_visible(1, summary.calendar_id, summary.version, False)
        require(await repo.list_sources(1) == (), "HIDDEN_VIEW_FAILED")
        try:
            await repo.create_calendar(1, profile(), CalendarKind.SCHOOL)
        except CalendarLimitExceeded:
            pass
        else:
            raise LabFailure("HIDDEN_QUOTA_FAILED")
        checks.append("actual_concurrent_owned_hidden_quota")

        # Different calendars compete for the same recipient's 11 quota slots.
        joined_tokens = []
        for index in range(12):
            owner = 8 if index < 6 else 9
            item = await repo.create_calendar(owner, profile(), CalendarKind.ACTIVITY)
            token = await repo.create_invitation(owner, item.calendar_id, 10, item.version, NOW)
            joined_tokens.append(token)
        joined_results = await asyncio.gather(
            *(repo.accept_invitation(10, token, 2, NOW) for token in joined_tokens),
            return_exceptions=True,
        )
        require(
            sum(isinstance(result, CalendarSummary) for result in joined_results) == 11
            and sum(isinstance(result, CalendarLimitExceeded) for result in joined_results) == 1,
            "CONCURRENT_JOIN_QUOTA_FAILED",
        )
        for summary in await repo.list_calendars(10):
            await repo.set_visible(10, summary.calendar_id, summary.version, False)
        require(
            await repo.list_sources(10) == () and len(await repo.list_calendars(10)) == 11,
            "HIDDEN_MEMBERSHIP_QUOTA_FAILED",
        )
        checks.append("actual_concurrent_joined_hidden_quota")

        shared = await repo.create_calendar(2, profile(), CalendarKind.SCHOOL)
        results = await asyncio.gather(
            *(repo.set_visible(2, shared.calendar_id, 1, True) for _ in range(10)),
            return_exceptions=True,
        )
        require(
            sum(isinstance(result, CalendarSummary) for result in results) == 1
            and sum(isinstance(result, CalendarConflict) for result in results) == 9,
            "CONCURRENT_CAS_FAILED",
        )
        checks.append("actual_concurrent_calendar_cas")
        token3 = await repo.create_invitation(2, shared.calendar_id, 3, 2, NOW)
        token4 = await repo.create_invitation(2, shared.calendar_id, 4, 3, NOW)
        preview3 = await repo.preview_invitation(3, token3, NOW)
        for target, instant in ((5, NOW), (3, NOW + timedelta(hours=24))):
            try:
                await repo.preview_invitation(target, token3, instant)
            except CalendarConflict:
                pass
            else:
                raise LabFailure("TARGET_EXPIRY_FAILED")
        require(not await repo.list_calendars(3), "PREVIEW_MUTATED_MEMBERSHIP")
        results = await asyncio.gather(
            *(repo.accept_invitation(3, token3, preview3.summary.version, NOW) for _ in range(10)),
            return_exceptions=True,
        )
        require(
            sum(isinstance(result, CalendarSummary) for result in results) == 1
            and sum(isinstance(result, CalendarConflict) for result in results) == 9,
            "CONCURRENT_SINGLE_USE_FAILED",
        )
        hidden = await repo.set_visible(3, shared.calendar_id, 5, False)
        granted = await repo.set_role(2, shared.calendar_id, 3, hidden.version, CalendarRole.ADMIN)
        preview4 = await repo.preview_invitation(4, token4, NOW)
        require(preview4.summary.version == granted.version, "UNRELATED_INVITATION_INVALIDATED")
        accepted4 = await repo.accept_invitation(4, token4, preview4.summary.version, NOW)
        token5 = await repo.create_invitation(2, shared.calendar_id, 5, accepted4.version, NOW)
        updated = await repo.update_calendar(
            3, shared.calendar_id, accepted4.version + 1, profile("Shared update")
        )
        try:
            await repo.preview_invitation(5, token5, NOW)
        except CalendarConflict:
            pass
        else:
            raise LabFailure("OLD_CONTENT_INVITATION_ACCEPTED")
        require(
            (await repo.get_snapshot(2, shared.calendar_id))[1].name == "Shared update"
            and (await repo.get_snapshot(4, shared.calendar_id))[1].name == "Shared update",
            "SHARED_UPDATE_NOT_VISIBLE",
        )
        checks.append("actual_targeted_single_use_shared_update_invitations")

        class AbortRepository(AsyncPostgresCalendarRepository):
            @staticmethod
            def _summary(row, member, user_id):
                if row.name == "Rollback probe":
                    raise RuntimeError("injected lab rollback")
                return AsyncPostgresCalendarRepository._summary(row, member, user_id)

        try:
            await AbortRepository(factory).update_calendar(
                3, shared.calendar_id, updated.version, profile("Rollback probe")
            )
        except RuntimeError:
            pass
        else:
            raise LabFailure("ROLLBACK_PROBE_NOT_INJECTED")
        summary, snapshot = await repo.get_snapshot(2, shared.calendar_id)
        require(
            summary.version == updated.version and snapshot.name == "Shared update",
            "ACTUAL_TRANSACTION_ROLLBACK_FAILED",
        )
        checks.append("actual_flushed_content_write_rollback")

        token6 = await repo.create_invitation(2, shared.calendar_id, 6, summary.version, NOW)
        preview6 = await repo.preview_invitation(6, token6, NOW)

        class AbortAcceptance(AsyncPostgresCalendarRepository):
            @staticmethod
            def _summary(row, member, user_id):
                raise RuntimeError("injected lab acceptance rollback")

        try:
            await AbortAcceptance(factory).accept_invitation(
                6, token6, preview6.summary.version, NOW
            )
        except RuntimeError:
            pass
        else:
            raise LabFailure("ACCEPT_ROLLBACK_PROBE_NOT_INJECTED")
        async with factory() as session:
            invite6 = await session.get(CalendarInvitation, digest_invitation_token(token6))
            require(
                invite6 is not None and invite6.consumed_at is None,
                "ACCEPT_TOMBSTONE_ROLLBACK_FAILED",
            )
        require(not await repo.list_calendars(6), "ACCEPT_MEMBERSHIP_ROLLBACK_FAILED")
        summary6, _ = await repo.get_snapshot(2, shared.calendar_id)
        require(summary6.version == preview6.summary.version, "ACCEPT_CAS_ROLLBACK_FAILED")
        await repo.accept_invitation(6, token6, preview6.summary.version, NOW)
        checks.append("actual_flushed_invitation_membership_cas_rollback_and_retry")

        exported = await repo.export_calendars(4)
        require(len(exported) == 1 and exported[0]["role"] == "MEMBER", "EXPORT_FAILED")
        serialized = json.dumps(exported)
        require(
            not any(
                marker in serialized
                for marker in ("user_id", "owner_user_id", "target_user_id", "token", "join_url")
            ),
            "EXPORT_PRIVATE_FIELDS",
        )
        async with factory() as session, session.begin():
            await session.execute(delete(User).where(User.telegram_user_id == 4))
        require(len(await repo.list_calendars(2)) == 1, "MEMBER_DELETE_REMOVED_CALENDAR")
        async with factory() as session:
            member_count = await session.scalar(
                select(func.count())
                .select_from(CalendarMembership)
                .where(CalendarMembership.calendar_id == shared.calendar_id)
            )
        require(member_count == 2, "MEMBER_CASCADE_FAILED")
        checks.append("self_only_export_member_delete_preserves_calendar")

        require(
            await repo.cleanup_invitations(NOW + timedelta(hours=23)) == 0,
            "EARLY_INVITATION_RETENTION_FAILED",
        )
        deleted = await repo.cleanup_invitations(NOW + timedelta(hours=24), limit=1)
        require(deleted == 1, "CLEANUP_BATCH_BOUND_FAILED")
        await repo.cleanup_invitations(NOW + timedelta(hours=24))
        async with factory() as session:
            count = await session.scalar(select(func.count()).select_from(CalendarInvitation))
        require(count == 0, "EXPIRED_TOKEN_RETENTION_FAILED")
        checks.append("actual_bounded_expired_terminal_cleanup")

        # Re-create a pending invite so owner cascade covers all three tables.
        summary, _ = await repo.get_snapshot(2, shared.calendar_id)
        await repo.create_invitation(2, shared.calendar_id, 7, summary.version, NOW)
        async with factory() as session, session.begin():
            await session.execute(delete(User).where(User.telegram_user_id == 2))
        async with factory() as session:
            require(await session.get(Calendar, shared.calendar_id) is None, "OWNER_CASCADE_FAILED")
            require(
                await session.scalar(
                    select(func.count())
                    .select_from(CalendarMembership)
                    .where(CalendarMembership.calendar_id == shared.calendar_id)
                )
                == 0,
                "OWNER_MEMBERSHIP_CASCADE_FAILED",
            )
            require(
                await session.scalar(select(func.count()).select_from(CalendarInvitation)) == 0,
                "OWNER_INVITATION_CASCADE_FAILED",
            )
        checks.append("actual_owner_deletion_all_calendar_cascades")
        head, tables = await version_and_tables(engine)
        require(head == HEAD and len(tables) == EXPECTED_TABLES, "FINAL_SCHEMA_FAILED")
        return {
            "status": "PASS",
            "environment": "DISPOSABLE_POSTGRES_LAB",
            "head": head,
            "table_count": len(tables),
            "checks": checks,
        }
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        receipt = asyncio.run(run_lab(args.database_url, args.workspace.resolve()))
    except LabFailure as error:
        receipt = {
            "status": "REJECTED",
            "environment": "DISPOSABLE_POSTGRES_LAB",
            "reason": str(error),
        }
    except Exception:
        receipt = {
            "status": "REJECTED",
            "environment": "DISPOSABLE_POSTGRES_LAB",
            "reason": "UNEXPECTED_LAB_FAILURE",
        }
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
