from __future__ import annotations

import asyncio
import os
import unittest
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from alembic import command
from alembic.config import Config
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import DBAPIError

from time_tl.application.schedule_worker import ScheduleWorker
from time_tl.application.sender_worker import DeliveryRecipient, SenderWorker
from time_tl.bot.models import NotificationMode as BotNotificationMode
from time_tl.domain.alerts import (
    AlertEpisode,
    AlertState,
    OfficialAlertSnapshot,
    ProviderSnapshotKind,
    SourceFailure,
    SourceFailureKind,
)
from time_tl.domain.schedule import DateOverride, Lesson, ScheduleProfile
from time_tl.infrastructure.alerts.repositories import AlertPollState
from time_tl.infrastructure.db.accounts import AsyncPostgresAccountRepository
from time_tl.infrastructure.db.alerts import PostgresAlertRepository
from time_tl.infrastructure.db.base import Base
from time_tl.infrastructure.db.engine import create_engine, create_session_factory
from time_tl.infrastructure.db.models import (
    ActiveTerritoryCatalog,
    AlertScope,
    AlertSnapshot,
    OutboxMessage,
    RuntimeSetting,
    TerritoryCatalogVersion,
    User,
)
from time_tl.infrastructure.db.models import (
    AlertEpisode as AlertEpisodeRow,
)
from time_tl.infrastructure.db.models import AlertPollState as PollRow
from time_tl.infrastructure.db.models import ScheduleProfile as ScheduleProfileRow
from time_tl.infrastructure.db.territories import (
    PostgresTerritoryCatalogRepository,
    TerritoryDraftRow,
)
from time_tl.infrastructure.db.workers import (
    PostgresScheduleWorkerRepository,
    PostgresSenderRepository,
)
from time_tl.infrastructure.telegram import TelegramMessage
from time_tl.runtime.services import PostgresRegionConfirmationService

DATABASE_ENV = "TIME_TL_E2E_DATABASE_URL"
CONFIRM_ENV = "TIME_TL_E2E_ALLOW_DESTRUCTIVE"
CONFIRM_VALUE = "YES_DELETE_TIME_TL_E2E_DATA"
DATABASE_URL = os.getenv(DATABASE_ENV, "")
E2E_ENABLED = bool(DATABASE_URL) and os.getenv(CONFIRM_ENV) == CONFIRM_VALUE
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class RecordingTelegram:
    def __init__(self) -> None:
        self.sent: list[TelegramMessage] = []

    async def send(self, message: TelegramMessage) -> None:
        self.sent.append(message)


class TerminateBackendBeforeCommit:
    """Real PostgreSQL fault hook after flush and immediately before commit."""

    def __init__(self, transaction, session, database_url: str) -> None:
        self._transaction = transaction
        self._session = session
        self._database_url = database_url

    async def __aenter__(self):
        return await self._transaction.__aenter__()

    async def __aexit__(self, exc_type, exc, traceback):
        if exc_type is None:
            # This query triggers the real AsyncSession autoflush first, so the
            # snapshot, outbox insert and poll cursor update all exist inside
            # the still-uncommitted server transaction.
            backend_pid = (
                await self._session.execute(text("SELECT pg_backend_pid()"))
            ).scalar_one()
            control_engine = create_engine(self._database_url, pool_size=1, max_overflow=0)
            try:
                async with control_engine.begin() as control:
                    database_name = (
                        await control.execute(text("SELECT current_database()"))
                    ).scalar_one()
                    if "time_tl_e2e" not in str(database_name).casefold():
                        raise RuntimeError(
                            "refusing backend termination outside a time_tl_e2e database"
                        )
                    try:
                        terminated = (
                            await control.execute(
                                text("SELECT pg_terminate_backend(:pid)"),
                                {"pid": backend_pid},
                            )
                        ).scalar_one()
                    except Exception as error:
                        raise RuntimeError(
                            "PostgreSQL E2E prerequisite failed: the test role must be "
                            "allowed to terminate its own database backend"
                        ) from error
                    if terminated is not True:
                        raise RuntimeError(
                            "PostgreSQL E2E prerequisite failed: pg_terminate_backend "
                            "did not terminate the transaction backend"
                        )
            finally:
                await control_engine.dispose()
        return await self._transaction.__aexit__(exc_type, exc, traceback)


class FaultingSessionProxy:
    def __init__(self, session, database_url: str) -> None:
        self._session = session
        self._database_url = database_url

    def begin(self):
        return TerminateBackendBeforeCommit(
            self._session.begin(), self._session, self._database_url
        )

    def __getattr__(self, name):
        return getattr(self._session, name)


class FaultingSessionContext:
    def __init__(self, factory, database_url: str) -> None:
        self._session = factory()
        self._database_url = database_url

    async def __aenter__(self):
        await self._session.__aenter__()
        return FaultingSessionProxy(self._session, self._database_url)

    async def __aexit__(self, exc_type, exc, traceback):
        return await self._session.__aexit__(exc_type, exc, traceback)


@unittest.skipUnless(
    E2E_ENABLED,
    f"set {DATABASE_ENV} and {CONFIRM_ENV}={CONFIRM_VALUE} to run destructive PostgreSQL E2E",
)
class PostgreSQLRestartFaultE2E(unittest.IsolatedAsyncioTestCase):
    """Opt-in test against an isolated database whose name contains time_tl_e2e."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        asyncio.run(cls._validate_database_name())
        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        # ConfigParser treats percent signs in URL-encoded passwords as interpolation.
        config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
        # Rebuild only this disposable database so this run proves the historical
        # 0003 -> 0004 upgrade instead of accepting a schema already at head.
        asyncio.run(cls._reset_disposable_public_schema())
        command.upgrade(config, "0003_pending_import_payload")
        legacy_user_id, legacy_scope_id = asyncio.run(cls._seed_legacy_0003_shape())
        command.upgrade(config, "0004_territory_catalog")
        asyncio.run(cls._assert_0004_upgrade_preserved_legacy_rows(legacy_user_id, legacy_scope_id))
        command.downgrade(config, "0003_pending_import_payload")
        asyncio.run(
            cls._assert_0003_downgrade_preserved_legacy_rows(legacy_user_id, legacy_scope_id)
        )
        command.upgrade(config, "0004_territory_catalog")
        asyncio.run(cls._assert_0004_upgrade_preserved_legacy_rows(legacy_user_id, legacy_scope_id))
        asyncio.run(cls._drop_0005_indexes_for_legacy_shape())
        command.upgrade(config, "0005_public_data_indexes")
        asyncio.run(cls._assert_0005_indexes_and_explain())
        command.downgrade(config, "0004_territory_catalog")
        asyncio.run(cls._assert_0005_indexes_absent())
        command.upgrade(config, "0005_public_data_indexes")
        asyncio.run(cls._assert_0005_indexes_and_explain())
        asyncio.run(cls._drop_0006_indexes_for_legacy_shape())
        command.upgrade(config, "0006_outbox_terminal_retention")
        asyncio.run(cls._assert_0006_indexes_and_explain())
        asyncio.run(cls._drop_0007_guard_for_legacy_shape())
        command.upgrade(config, "0007_outbox_indeterminate")
        asyncio.run(cls._assert_0007_delivery_guard())
        command.downgrade(config, "0006_outbox_terminal_retention")
        asyncio.run(cls._assert_0007_delivery_guard_absent())
        command.upgrade(config, "0007_outbox_indeterminate")
        asyncio.run(cls._assert_0007_delivery_guard())
        asyncio.run(cls._drop_0008_tables_for_legacy_shape())
        command.upgrade(config, "0008_homework_foundation")
        asyncio.run(cls._assert_0008_homework_schema())
        command.downgrade(config, "0007_outbox_indeterminate")
        asyncio.run(cls._assert_0008_homework_schema_absent())
        command.upgrade(config, "0008_homework_foundation")
        asyncio.run(cls._assert_0008_homework_schema())
        command.downgrade(config, "0005_public_data_indexes")
        asyncio.run(cls._assert_0006_indexes_absent())
        command.upgrade(config, "0006_outbox_terminal_retention")
        asyncio.run(cls._assert_0006_indexes_and_explain())
        command.upgrade(config, "0007_outbox_indeterminate")
        asyncio.run(cls._assert_0007_delivery_guard())
        command.upgrade(config, "0008_homework_foundation")
        asyncio.run(cls._assert_0008_homework_schema())
        cls.upgrade_0003_to_0004_verified = True
        cls.downgrade_0004_to_0003_verified = True
        cls.upgrade_0004_to_0005_verified = True
        cls.downgrade_0005_to_0004_verified = True
        cls.upgrade_0005_to_0006_verified = True
        cls.downgrade_0006_to_0005_verified = True
        cls.upgrade_0006_to_0007_verified = True
        cls.downgrade_0007_to_0006_verified = True
        cls.upgrade_0007_to_0008_verified = True
        cls.downgrade_0008_to_0007_verified = True

    @staticmethod
    async def _reset_disposable_public_schema() -> None:
        """Reset only the name-guarded disposable database used by this harness."""

        engine = create_engine(DATABASE_URL, pool_size=1, max_overflow=0)
        try:
            async with engine.begin() as connection:
                database_name = (
                    await connection.execute(text("SELECT current_database()"))
                ).scalar_one()
                if "time_tl_e2e" not in str(database_name).casefold():
                    raise RuntimeError("refusing schema reset outside a time_tl_e2e database")
                await connection.execute(text("DROP SCHEMA public CASCADE"))
                await connection.execute(text("CREATE SCHEMA public"))
        finally:
            await engine.dispose()

    @staticmethod
    async def _seed_legacy_0003_shape():
        """Recreate the old schema hidden by 0001 importing current metadata."""

        engine = create_engine(DATABASE_URL, pool_size=1, max_overflow=0)
        legacy_user_id = uuid4()
        legacy_scope_id = uuid4()
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "ALTER TABLE alert_scopes "
                        "DROP CONSTRAINT IF EXISTS fk_alert_scopes_selected_territory"
                    )
                )
                await connection.execute(
                    text(
                        "ALTER TABLE alert_scopes "
                        "DROP CONSTRAINT IF EXISTS ck_alert_scopes_territory_revision_pair"
                    )
                )
                await connection.execute(
                    text("ALTER TABLE alert_scopes DROP COLUMN IF EXISTS territory_catalog_version")
                )
                await connection.execute(
                    text("ALTER TABLE alert_scopes DROP COLUMN IF EXISTS territory_provider")
                )
                await connection.execute(text("DROP TABLE IF EXISTS active_territory_catalogs"))
                await connection.execute(text("DROP TABLE IF EXISTS territories"))
                await connection.execute(text("DROP TABLE IF EXISTS territory_catalog_versions"))
                await connection.execute(
                    text(
                        "INSERT INTO users "
                        "(id, telegram_user_id, mode, confirmed_region_id, region_name) "
                        "VALUES (:id, :telegram_user_id, 'LESSONS_AND_ALERTS', '14', "
                        "'Legacy Kyiv oblast')"
                    ),
                    {"id": legacy_user_id, "telegram_user_id": 8_000_004},
                )
                await connection.execute(
                    text(
                        "INSERT INTO alert_scopes "
                        "(id, user_id, selected_region_id, applicable_region_ids) "
                        "VALUES (:id, :user_id, '14', '[\"14\"]'::jsonb)"
                    ),
                    {"id": legacy_scope_id, "user_id": legacy_user_id},
                )
        finally:
            await engine.dispose()
        return legacy_user_id, legacy_scope_id

    @staticmethod
    async def _assert_0004_upgrade_preserved_legacy_rows(legacy_user_id, legacy_scope_id) -> None:
        engine = create_engine(DATABASE_URL, pool_size=1, max_overflow=0)
        try:
            async with engine.connect() as connection:
                user = (
                    await connection.execute(
                        text(
                            "SELECT telegram_user_id, confirmed_region_id FROM users WHERE id = :id"
                        ),
                        {"id": legacy_user_id},
                    )
                ).one()
                scope = (
                    await connection.execute(
                        text(
                            "SELECT selected_region_id, applicable_region_ids, "
                            "territory_provider, territory_catalog_version "
                            "FROM alert_scopes WHERE id = :id"
                        ),
                        {"id": legacy_scope_id},
                    )
                ).one()
                revision = (
                    await connection.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one()
        finally:
            await engine.dispose()
        if tuple(user) != (8_000_004, "14"):
            raise AssertionError("0004 migration did not preserve the legacy user")
        if tuple(scope) != ("14", ["14"], None, None):
            raise AssertionError("0004 migration did not preserve the legacy alert scope")
        if revision != "0004_territory_catalog":
            raise AssertionError(f"unexpected Alembic revision after upgrade: {revision}")

    @staticmethod
    async def _assert_0003_downgrade_preserved_legacy_rows(legacy_user_id, legacy_scope_id) -> None:
        engine = create_engine(DATABASE_URL, pool_size=1, max_overflow=0)
        try:
            async with engine.connect() as connection:
                user = (
                    await connection.execute(
                        text(
                            "SELECT telegram_user_id, confirmed_region_id FROM users WHERE id = :id"
                        ),
                        {"id": legacy_user_id},
                    )
                ).one()
                scope = (
                    await connection.execute(
                        text(
                            "SELECT selected_region_id, applicable_region_ids "
                            "FROM alert_scopes WHERE id = :id"
                        ),
                        {"id": legacy_scope_id},
                    )
                ).one()
                hierarchy_tables = int(
                    (
                        await connection.execute(
                            text(
                                "SELECT count(*) FROM information_schema.tables "
                                "WHERE table_schema = 'public' AND table_name IN "
                                "('territory_catalog_versions', 'territories', "
                                "'active_territory_catalogs')"
                            )
                        )
                    ).scalar_one()
                )
                revision_columns = int(
                    (
                        await connection.execute(
                            text(
                                "SELECT count(*) FROM information_schema.columns "
                                "WHERE table_schema = 'public' "
                                "AND table_name = 'alert_scopes' AND column_name IN "
                                "('territory_provider', 'territory_catalog_version')"
                            )
                        )
                    ).scalar_one()
                )
                revision = (
                    await connection.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one()
        finally:
            await engine.dispose()
        if tuple(user) != (8_000_004, "14"):
            raise AssertionError("0004 downgrade did not preserve the legacy user")
        if tuple(scope) != ("14", ["14"]):
            raise AssertionError("0004 downgrade did not preserve the legacy alert scope")
        if hierarchy_tables != 0 or revision_columns != 0:
            raise AssertionError("0004 downgrade left territory schema objects behind")
        if revision != "0003_pending_import_payload":
            raise AssertionError(f"unexpected Alembic revision after downgrade: {revision}")

    @staticmethod
    async def _drop_0005_indexes_for_legacy_shape() -> None:
        engine = create_engine(DATABASE_URL, pool_size=1, max_overflow=0)
        try:
            async with engine.begin() as connection:
                for name in (
                    "ix_outbox_aggregate",
                    "ix_outbox_confirmed_clear_cursor",
                    "ix_pending_import_active_user_created",
                ):
                    await connection.execute(text(f'DROP INDEX IF EXISTS "{name}"'))
        finally:
            await engine.dispose()

    @staticmethod
    async def _assert_0005_indexes_absent() -> None:
        engine = create_engine(DATABASE_URL, pool_size=1, max_overflow=0)
        try:
            async with engine.connect() as connection:
                names = set(
                    (
                        await connection.execute(
                            text(
                                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
                                "AND indexname IN ('ix_outbox_aggregate', "
                                "'ix_outbox_confirmed_clear_cursor', "
                                "'ix_pending_import_active_user_created')"
                            )
                        )
                    ).scalars()
                )
        finally:
            await engine.dispose()
        if names:
            raise AssertionError(f"0005 downgrade left indexes behind: {sorted(names)}")

    @staticmethod
    async def _assert_0005_indexes_and_explain() -> None:
        engine = create_engine(DATABASE_URL, pool_size=1, max_overflow=0)
        expected = {
            "ix_outbox_aggregate",
            "ix_outbox_confirmed_clear_cursor",
            "ix_pending_import_active_user_created",
        }
        try:
            async with engine.begin() as connection:
                names = set(
                    (
                        await connection.execute(
                            text(
                                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
                                "AND indexname IN ('ix_outbox_aggregate', "
                                "'ix_outbox_confirmed_clear_cursor', "
                                "'ix_pending_import_active_user_created')"
                            )
                        )
                    ).scalars()
                )
                await connection.execute(text("SET LOCAL enable_seqscan = off"))
                aggregate_plan = (
                    await connection.execute(
                        text(
                            "EXPLAIN (FORMAT JSON) SELECT id FROM outbox_messages "
                            "WHERE aggregate_type = 'schedule' AND aggregate_id = 'probe'"
                        )
                    )
                ).scalar_one()
                clear_plan = (
                    await connection.execute(
                        text(
                            "EXPLAIN (FORMAT JSON) SELECT id FROM outbox_messages "
                            "WHERE event_type = 'ALL_CLEAR_CONFIRMED' "
                            "AND created_at >= now() - interval '20 minutes' "
                            "ORDER BY created_at, id LIMIT 100"
                        )
                    )
                ).scalar_one()
        finally:
            await engine.dispose()
        if names != expected:
            raise AssertionError(f"0005 indexes mismatch: {sorted(names)}")
        if "ix_outbox_aggregate" not in str(aggregate_plan):
            raise AssertionError("aggregate lookup EXPLAIN did not use ix_outbox_aggregate")
        if "ix_outbox_confirmed_clear_cursor" not in str(clear_plan):
            raise AssertionError(
                "confirmed-clear EXPLAIN did not use ix_outbox_confirmed_clear_cursor"
            )

    @staticmethod
    async def _drop_0006_indexes_for_legacy_shape() -> None:
        engine = create_engine(DATABASE_URL, pool_size=1, max_overflow=0)
        try:
            async with engine.begin() as connection:
                for name in (
                    "ix_outbox_delivered_retention",
                    "ix_outbox_dead_retention",
                ):
                    await connection.execute(text(f'DROP INDEX IF EXISTS "{name}"'))
        finally:
            await engine.dispose()

    @staticmethod
    async def _assert_0006_indexes_absent() -> None:
        engine = create_engine(DATABASE_URL, pool_size=1, max_overflow=0)
        try:
            async with engine.connect() as connection:
                names = set(
                    (
                        await connection.execute(
                            text(
                                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
                                "AND indexname IN ('ix_outbox_delivered_retention', "
                                "'ix_outbox_dead_retention')"
                            )
                        )
                    ).scalars()
                )
        finally:
            await engine.dispose()
        if names:
            raise AssertionError(f"0006 downgrade left indexes behind: {sorted(names)}")

    @staticmethod
    async def _assert_0006_indexes_and_explain() -> None:
        engine = create_engine(DATABASE_URL, pool_size=1, max_overflow=0)
        expected = {
            "ix_outbox_delivered_retention",
            "ix_outbox_dead_retention",
        }
        try:
            async with engine.begin() as connection:
                names = set(
                    (
                        await connection.execute(
                            text(
                                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
                                "AND indexname IN ('ix_outbox_delivered_retention', "
                                "'ix_outbox_dead_retention')"
                            )
                        )
                    ).scalars()
                )
                await connection.execute(text("SET LOCAL enable_seqscan = off"))
                delivered_plan = (
                    await connection.execute(
                        text(
                            "EXPLAIN (FORMAT JSON) SELECT id FROM outbox_messages "
                            "WHERE delivered_at IS NOT NULL AND dead_at IS NULL "
                            "AND claimed_at IS NULL "
                            "AND delivered_at <= now() - interval '90 days' "
                            "ORDER BY delivered_at, id LIMIT 100"
                        )
                    )
                ).scalar_one()
                dead_plan = (
                    await connection.execute(
                        text(
                            "EXPLAIN (FORMAT JSON) SELECT id FROM outbox_messages "
                            "WHERE dead_at IS NOT NULL AND delivered_at IS NULL "
                            "AND claimed_at IS NULL "
                            "AND dead_at <= now() - interval '90 days' "
                            "ORDER BY dead_at, id LIMIT 100"
                        )
                    )
                ).scalar_one()
        finally:
            await engine.dispose()
        if names != expected:
            raise AssertionError(f"0006 indexes mismatch: {sorted(names)}")
        if "ix_outbox_delivered_retention" not in str(delivered_plan):
            raise AssertionError(
                "delivered retention EXPLAIN did not use ix_outbox_delivered_retention"
            )
        if "ix_outbox_dead_retention" not in str(dead_plan):
            raise AssertionError("dead retention EXPLAIN did not use ix_outbox_dead_retention")

    @staticmethod
    async def _validate_database_name() -> None:
        engine = create_engine(DATABASE_URL, pool_size=1, max_overflow=0)
        try:
            async with engine.connect() as connection:
                database_name = (
                    await connection.execute(text("SELECT current_database()"))
                ).scalar_one()
        finally:
            await engine.dispose()
        if "time_tl_e2e" not in str(database_name).casefold():
            raise RuntimeError(
                "refusing destructive E2E: current_database() must contain 'time_tl_e2e'"
            )

    @staticmethod
    async def _drop_0007_guard_for_legacy_shape() -> None:
        """Remove metadata-created 0007 objects so the additive upgrade is real."""

        engine = create_engine(DATABASE_URL, pool_size=1, max_overflow=0)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "ALTER TABLE outbox_messages DROP CONSTRAINT IF EXISTS "
                        "ck_outbox_messages_indeterminate_terminal"
                    )
                )
                await connection.execute(
                    text("DROP INDEX IF EXISTS ix_outbox_indeterminate")
                )
                await connection.execute(
                    text(
                        "ALTER TABLE outbox_messages "
                        "DROP COLUMN IF EXISTS indeterminate_at"
                    )
                )
                await connection.execute(
                    text(
                        "ALTER TABLE outbox_messages "
                        "DROP COLUMN IF EXISTS dispatch_started_at"
                    )
                )
        finally:
            await engine.dispose()

    @staticmethod
    async def _assert_0007_delivery_guard() -> None:
        engine = create_engine(DATABASE_URL, pool_size=1, max_overflow=0)
        try:
            async with engine.connect() as connection:
                columns = set(
                    (
                        await connection.execute(
                            text(
                                "SELECT column_name FROM information_schema.columns "
                                "WHERE table_schema = 'public' "
                                "AND table_name = 'outbox_messages'"
                            )
                        )
                    ).scalars()
                )
                indexes = set(
                    (
                        await connection.execute(
                            text(
                                "SELECT indexname FROM pg_indexes "
                                "WHERE schemaname = 'public' "
                                "AND tablename = 'outbox_messages'"
                            )
                        )
                    ).scalars()
                )
                constraints = set(
                    (
                        await connection.execute(
                            text(
                                "SELECT conname FROM pg_constraint "
                                "WHERE conrelid = 'outbox_messages'::regclass"
                            )
                        )
                    ).scalars()
                )
        finally:
            await engine.dispose()
        if not {"dispatch_started_at", "indeterminate_at"}.issubset(columns):
            raise AssertionError("0007 delivery columns are missing")
        if "ix_outbox_indeterminate" not in indexes:
            raise AssertionError("0007 indeterminate index is missing")
        if "ck_outbox_messages_indeterminate_terminal" not in constraints:
            raise AssertionError("0007 terminal constraint is missing")

    @staticmethod
    async def _assert_0007_delivery_guard_absent() -> None:
        engine = create_engine(DATABASE_URL, pool_size=1, max_overflow=0)
        try:
            async with engine.connect() as connection:
                columns = set(
                    (
                        await connection.execute(
                            text(
                                "SELECT column_name FROM information_schema.columns "
                                "WHERE table_schema = 'public' "
                                "AND table_name = 'outbox_messages'"
                            )
                        )
                    ).scalars()
                )
        finally:
            await engine.dispose()
        if {"dispatch_started_at", "indeterminate_at"} & columns:
            raise AssertionError("0007 delivery columns survived downgrade")

    @staticmethod
    async def _drop_0008_tables_for_legacy_shape() -> None:
        """Remove metadata-created homework tables so the 0008 upgrade is real."""

        engine = create_engine(DATABASE_URL, pool_size=1, max_overflow=0)
        try:
            async with engine.begin() as connection:
                for table_name in (
                    "pending_homework_drafts",
                    "homework_user_states",
                    "homework_assignments",
                    "schedule_subjects",
                ):
                    await connection.execute(text(f'DROP TABLE IF EXISTS "{table_name}" CASCADE'))
        finally:
            await engine.dispose()

    @staticmethod
    async def _assert_0008_homework_schema() -> None:
        expected = {
            "schedule_subjects",
            "homework_assignments",
            "homework_user_states",
            "pending_homework_drafts",
        }
        engine = create_engine(DATABASE_URL, pool_size=1, max_overflow=0)
        try:
            async with engine.connect() as connection:
                tables = set(
                    (
                        await connection.execute(
                            text(
                                "SELECT table_name FROM information_schema.tables "
                                "WHERE table_schema = 'public' AND table_name = ANY(:names)"
                            ),
                            {"names": sorted(expected)},
                        )
                    ).scalars()
                )
                revision = (
                    await connection.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one()
        finally:
            await engine.dispose()
        if tables != expected:
            raise AssertionError(f"0008 homework tables mismatch: {sorted(tables)}")
        if revision != "0008_homework_foundation":
            raise AssertionError(f"unexpected Alembic revision after 0008 upgrade: {revision}")

    @staticmethod
    async def _assert_0008_homework_schema_absent() -> None:
        expected = {
            "schedule_subjects",
            "homework_assignments",
            "homework_user_states",
            "pending_homework_drafts",
        }
        engine = create_engine(DATABASE_URL, pool_size=1, max_overflow=0)
        try:
            async with engine.connect() as connection:
                count = int(
                    (
                        await connection.execute(
                            text(
                                "SELECT count(*) FROM information_schema.tables "
                                "WHERE table_schema = 'public' AND table_name = ANY(:names)"
                            ),
                            {"names": sorted(expected)},
                        )
                    ).scalar_one()
                )
                revision = (
                    await connection.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one()
        finally:
            await engine.dispose()
        if count != 0:
            raise AssertionError("0008 homework tables survived downgrade")
        if revision != "0007_outbox_indeterminate":
            raise AssertionError(f"unexpected Alembic revision after 0008 downgrade: {revision}")

    async def asyncSetUp(self) -> None:
        self.engine = create_engine(DATABASE_URL, pool_size=2, max_overflow=0)
        self.factory = create_session_factory(self.engine)
        await self._clean_test_database()

    async def asyncTearDown(self) -> None:
        try:
            await self._clean_test_database()
        finally:
            await self.engine.dispose()

    async def _clean_test_database(self) -> None:
        async with self.engine.begin() as connection:
            database_name = (
                await connection.execute(text("SELECT current_database()"))
            ).scalar_one()
            if "time_tl_e2e" not in str(database_name).casefold():
                raise RuntimeError("refusing cleanup outside a time_tl_e2e database")
            preparer = connection.dialect.identifier_preparer
            table_names = [
                preparer.quote(table.name) for table in reversed(Base.metadata.sorted_tables)
            ]
            await connection.execute(
                text(f"TRUNCATE TABLE {', '.join(table_names)} RESTART IDENTITY CASCADE")
            )
            await connection.execute(
                insert(PollRow).values(singleton_id=1, fetch_revision=0, circuit_open=False)
            )

    async def _restart_database_clients(self) -> None:
        await self.engine.dispose()
        self.engine = create_engine(DATABASE_URL, pool_size=2, max_overflow=0)
        self.factory = create_session_factory(self.engine)

    async def _snapshot_state(self) -> AlertState:
        async with self.factory() as session:
            value = (await session.execute(select(AlertSnapshot.state))).scalar_one()
            return AlertState(value)

    async def _outbox_count(self, event_type: str | None = None) -> int:
        async with self.factory() as session:
            statement = select(func.count()).select_from(OutboxMessage)
            if event_type is not None:
                statement = statement.where(OutboxMessage.event_type == event_type)
            return int((await session.execute(statement)).scalar_one())

    @staticmethod
    def _catalog_rows(*, renamed: bool = False) -> tuple[TerritoryDraftRow, ...]:
        suffix = " updated" if renamed else ""
        return (
            TerritoryDraftRow("14", f"Kyiv oblast{suffix}", "OBLAST", None, "UA32"),
            TerritoryDraftRow("1401", f"Kyiv raion{suffix}", "RAION", "14", "UA3202"),
            TerritoryDraftRow(
                "140101",
                f"Kyiv hromada{suffix}",
                "HROMADA",
                "1401",
                "UA32020010000012345",
            ),
        )

    async def _load_and_activate_catalog(
        self, catalog_version: str, *, renamed: bool = False
    ) -> PostgresTerritoryCatalogRepository:
        repository = PostgresTerritoryCatalogRepository(self.factory)
        source_hash = ("b" if renamed else "a") * 64
        await repository.load_draft(
            provider="alerts.in.ua",
            catalog_version=catalog_version,
            source_revision=f"e2e-{catalog_version}",
            source_sha256=source_hash,
            rows=self._catalog_rows(renamed=renamed),
            selectable_count=3,
            hidden_nonselectable_count=0,
            unresolved_total=0,
            reconciliation_report={"unresolved_total": 0},
        )
        await repository.activate(
            provider="alerts.in.ua",
            catalog_version=catalog_version,
            expected_source_sha256=source_hash,
            expected_record_count=3,
            expected_unresolved_total=0,
        )
        return repository

    def test_upgrade_0003_to_0008_and_downgrades_are_verified(self) -> None:
        self.assertTrue(self.upgrade_0003_to_0004_verified)
        self.assertTrue(self.downgrade_0004_to_0003_verified)
        self.assertTrue(self.upgrade_0004_to_0005_verified)
        self.assertTrue(self.downgrade_0005_to_0004_verified)
        self.assertTrue(self.upgrade_0005_to_0006_verified)
        self.assertTrue(self.downgrade_0006_to_0005_verified)
        self.assertTrue(self.upgrade_0006_to_0007_verified)
        self.assertTrue(self.downgrade_0007_to_0006_verified)
        self.assertTrue(self.upgrade_0007_to_0008_verified)
        self.assertTrue(self.downgrade_0008_to_0007_verified)

    async def test_terminal_outbox_retention_is_bounded_and_skips_locked_rows(self) -> None:
        now = datetime.now(UTC)
        old = now - timedelta(days=91)
        fresh = now - timedelta(days=1)

        def message(
            event_key: str,
            *,
            delivered_at: datetime | None = None,
            dead_at: datetime | None = None,
            claimed_at: datetime | None = None,
            attempts: int = 0,
        ) -> OutboxMessage:
            return OutboxMessage(
                event_key=event_key,
                aggregate_type="retention-e2e",
                aggregate_id=event_key,
                event_type="RETENTION_PROBE",
                payload={},
                available_at=old,
                claimed_at=claimed_at,
                delivered_at=delivered_at,
                dead_at=dead_at,
                attempts=attempts,
            )

        async with self.factory() as session:
            async with session.begin():
                session.add_all(
                    [
                        message("old-delivered", delivered_at=old, attempts=2),
                        message("old-dead", dead_at=old, attempts=3),
                        message(
                            "old-claimed",
                            delivered_at=old,
                            claimed_at=now,
                            attempts=4,
                        ),
                        message("fresh-delivered", delivered_at=fresh, attempts=5),
                        message("fresh-dead", dead_at=fresh, attempts=6),
                        message("pending", attempts=7),
                    ]
                )

        repository = PostgresSenderRepository(
            self.factory, stale_after_seconds=120, clock=lambda: now
        )
        self.assertEqual(await repository.prune_terminal_outbox(now, limit=1), 1)
        self.assertEqual(await repository.prune_terminal_outbox(now, limit=1), 1)
        self.assertEqual(await repository.prune_terminal_outbox(now, limit=1), 0)

        async with self.factory() as session:
            survivors = dict(
                (
                    await session.execute(
                        select(OutboxMessage.event_key, OutboxMessage.attempts).where(
                            OutboxMessage.aggregate_type == "retention-e2e"
                        )
                    )
                ).all()
            )
        self.assertEqual(
            survivors,
            {
                "old-claimed": 4,
                "fresh-delivered": 5,
                "fresh-dead": 6,
                "pending": 7,
            },
        )

        async with self.factory() as session:
            async with session.begin():
                session.add_all(
                    [
                        message("locked-old-delivered", delivered_at=old, attempts=8),
                        message("unlocked-old-delivered", delivered_at=old, attempts=9),
                    ]
                )

        async with self.factory() as locking_session:
            async with locking_session.begin():
                await locking_session.execute(
                    select(OutboxMessage)
                    .where(OutboxMessage.event_key == "locked-old-delivered")
                    .with_for_update()
                )
                self.assertEqual(await repository.prune_terminal_outbox(now, limit=1), 1)
                async with self.factory() as checking_session:
                    remaining_during_lock = set(
                        (
                            await checking_session.execute(
                                select(OutboxMessage.event_key).where(
                                    OutboxMessage.event_key.in_(
                                        ("locked-old-delivered", "unlocked-old-delivered")
                                    )
                                )
                            )
                        ).scalars()
                    )
                self.assertEqual(remaining_during_lock, {"locked-old-delivered"})

        self.assertEqual(await repository.prune_terminal_outbox(now, limit=1), 1)
        async with self.factory() as session:
            remaining = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(OutboxMessage)
                        .where(
                            OutboxMessage.event_key.in_(
                                ("locked-old-delivered", "unlocked-old-delivered")
                            )
                        )
                    )
                ).scalar_one()
            )
        self.assertEqual(remaining, 0)

    async def test_atomic_rollback_restart_two_clear_return_and_sender_dedupe(self) -> None:
        telegram_user_id = 8_000_021
        base = datetime.now(UTC)
        worker_now = base + timedelta(seconds=20)
        worker_local = worker_now.astimezone(ZoneInfo("Europe/Kyiv"))
        if (worker_local.hour, worker_local.minute) == (23, 59):
            worker_now += timedelta(minutes=1)
        catalog = await self._load_and_activate_catalog("e2e-v1")
        account_repository = AsyncPostgresAccountRepository(self.factory)
        account = await account_repository.get_or_create(telegram_user_id)
        account.mode = BotNotificationMode.LESSONS_AND_SIRENS
        account.region_id = "14"
        account.region_name = "Київська область"
        account.reminder_lead_minutes = 5
        local_day = worker_now.astimezone(ZoneInfo("Europe/Kyiv")).date()
        account.profiles = {
            "e2e-school": ScheduleProfile(
                "e2e-school",
                "E2E School",
                overrides=(
                    DateOverride(
                        day=local_day,
                        day_off=False,
                        lessons=(Lesson(time(0), time(23, 59), "E2E Algebra"),),
                    ),
                ),
                active=True,
            )
        }
        await account_repository.save(account)

        await PostgresRegionConfirmationService(self.factory, catalog).confirm_region(account)
        alerts = PostgresAlertRepository(self.factory)
        active_revision = (await alerts.poll_state()).fetch_revision + 1

        episode = AlertEpisode("14", "AIR", "e2e-episode")
        active = OfficialAlertSnapshot(
            update_id="e2e-active-1",
            fetch_revision=active_revision,
            kind=ProviderSnapshotKind.FULL_200,
            covered_region_ids=frozenset({"14", "1401", "140101"}),
            active_episodes=(episode,),
            fetched_at=base,
            is_complete=True,
            is_fresh=True,
        )
        created = await alerts.apply_snapshot_and_poll_state_atomically(
            active,
            AlertPollState(fetch_revision=active_revision, last_modified="active-1"),
        )
        self.assertEqual(len(created), 1)
        self.assertEqual(await self._snapshot_state(), AlertState.ACTIVE)
        self.assertEqual(await self._outbox_count(), 1)

        await self._restart_database_clients()
        alerts = PostgresAlertRepository(self.factory)
        await alerts.apply_failure_atomically(SourceFailure(SourceFailureKind.TIMEOUT))
        self.assertEqual(await self._snapshot_state(), AlertState.STALE)

        first_clear = OfficialAlertSnapshot(
            update_id="e2e-clear-candidate",
            fetch_revision=active_revision + 1,
            kind=ProviderSnapshotKind.FULL_200,
            covered_region_ids=frozenset({"14", "1401", "140101"}),
            active_episodes=(),
            fetched_at=worker_now - timedelta(seconds=10),
            is_complete=True,
            is_fresh=True,
        )
        await alerts.apply_snapshot_and_poll_state_atomically(
            first_clear,
            AlertPollState(fetch_revision=active_revision + 1, last_modified="clear-1"),
        )
        self.assertEqual(await self._snapshot_state(), AlertState.STALE)
        self.assertEqual(await self._outbox_count("ALL_CLEAR_CONFIRMED"), 0)

        second_clear = OfficialAlertSnapshot(
            update_id="e2e-clear-confirmed",
            fetch_revision=active_revision + 2,
            kind=ProviderSnapshotKind.FULL_200,
            covered_region_ids=frozenset({"14", "1401", "140101"}),
            active_episodes=(),
            fetched_at=worker_now,
            is_complete=True,
            is_fresh=True,
        )
        await alerts.apply_snapshot_and_poll_state_atomically(
            second_clear,
            AlertPollState(fetch_revision=active_revision + 2, last_modified="clear-2"),
        )
        self.assertEqual(await self._snapshot_state(), AlertState.CLEAR_CONFIRMED)
        self.assertEqual(await self._outbox_count("ALL_CLEAR_CONFIRMED"), 1)

        def faulting_factory():
            return FaultingSessionContext(self.factory, DATABASE_URL)

        faulting_alerts = PostgresAlertRepository(faulting_factory)  # type: ignore[arg-type]
        rolled_back_realert = OfficialAlertSnapshot(
            update_id="e2e-realert-must-rollback",
            fetch_revision=active_revision + 3,
            kind=ProviderSnapshotKind.FULL_200,
            covered_region_ids=frozenset({"14", "1401", "140101"}),
            active_episodes=(AlertEpisode("14", "AIR", "e2e-episode-2"),),
            fetched_at=worker_now + timedelta(seconds=1),
            is_complete=True,
            is_fresh=True,
        )
        with self.assertRaises(DBAPIError):
            await faulting_alerts.apply_snapshot_and_poll_state_atomically(
                rolled_back_realert,
                AlertPollState(
                    fetch_revision=active_revision + 3,
                    last_modified="must-rollback",
                ),
            )

        await self._restart_database_clients()
        async with self.factory() as session:
            poll = await session.get(PollRow, 1)
            self.assertIsNotNone(poll)
            self.assertEqual(poll.fetch_revision, active_revision + 2)
            self.assertEqual(poll.last_modified, "clear-2")
        self.assertEqual(await self._snapshot_state(), AlertState.CLEAR_CONFIRMED)
        self.assertEqual(await self._outbox_count(), 2)
        self.assertEqual(await self._outbox_count("ALERT_STARTED"), 1)

        schedule_repository = PostgresScheduleWorkerRepository(
            self.factory, stale_after_seconds=120
        )
        schedule_worker = ScheduleWorker(schedule_repository)
        self.assertEqual(await schedule_worker.run_once(worker_now), 1)
        await self._restart_database_clients()
        schedule_worker = ScheduleWorker(
            PostgresScheduleWorkerRepository(self.factory, stale_after_seconds=120)
        )
        self.assertEqual(await schedule_worker.run_once(worker_now), 0)
        self.assertEqual(await self._outbox_count("RETURN_TO_ACTIVE_LESSON"), 1)

        await self._restart_database_clients()
        recording = RecordingTelegram()
        sender_repository = PostgresSenderRepository(
            self.factory, stale_after_seconds=120, clock=lambda: worker_now
        )
        sender = SenderWorker(
            repository=sender_repository,
            transport=recording,
            messages_per_second=1_000,
        )

        async def no_sleep(_seconds: float) -> None:
            return None

        self.assertEqual(await sender.run_once(worker_now, sleep=no_sleep), 2)
        self.assertEqual(await sender.run_once(worker_now, sleep=no_sleep), 0)
        self.assertEqual(len(recording.sent), 2)
        self.assertTrue(all(item.text.strip() for item in recording.sent))
        async with self.factory() as session:
            delivered_types = set(
                (
                    await session.execute(
                        select(OutboxMessage.event_type).where(
                            OutboxMessage.delivered_at.is_not(None)
                        )
                    )
                ).scalars()
            )
        self.assertEqual(
            delivered_types,
            {"ALL_CLEAR_CONFIRMED", "RETURN_TO_ACTIVE_LESSON"},
        )

    async def test_post_send_commit_failure_is_indeterminate_and_never_resent(self) -> None:
        now = datetime.now(UTC)
        telegram_user_id = 8_000_022
        async with self.factory() as session:
            async with session.begin():
                user = User(
                    telegram_user_id=telegram_user_id,
                    mode="LESSONS_ONLY",
                    reminder_lead_minutes=5,
                )
                session.add(user)
                await session.flush()
                profile = ScheduleProfileRow(
                    user_id=user.id,
                    public_id="indeterminate-e2e",
                    name="Indeterminate E2E",
                    is_active=True,
                    version=1,
                )
                session.add(profile)
                outbox = OutboxMessage(
                    event_key="post-send-commit-failure",
                    aggregate_type="schedule",
                    aggregate_id=str(user.id),
                    event_type="LESSON_REMINDER",
                    payload={
                        "telegram_chat_id": telegram_user_id,
                        "profile_revision": 1,
                        "starts_at": (now + timedelta(minutes=10)).isoformat(),
                    },
                    available_at=now,
                    claimed_at=now,
                )
                session.add(outbox)
                await session.flush()
                message_id = str(outbox.id)

        factory_calls = 0

        def post_send_fault_factory():
            nonlocal factory_calls
            factory_calls += 1
            if factory_calls == 1:
                return self.factory()
            return FaultingSessionContext(self.factory, DATABASE_URL)

        repository = PostgresSenderRepository(
            post_send_fault_factory,  # type: ignore[arg-type]
            stale_after_seconds=120,
            clock=lambda: now,
        )
        sent = 0

        async def deliver() -> None:
            nonlocal sent
            sent += 1

        with self.assertRaises(DBAPIError):
            await repository.deliver_if_authorized(
                message_id,
                now,
                deliver,
                expected_recipient=DeliveryRecipient(telegram_user_id),
            )
        self.assertEqual(sent, 1)

        await self._restart_database_clients()
        async with self.factory() as session:
            persisted = await session.get(OutboxMessage, outbox.id)
            self.assertIsNotNone(persisted)
            self.assertEqual(persisted.dispatch_started_at, now)
            self.assertIsNone(persisted.delivered_at)
            self.assertIsNone(persisted.dead_at)

        recovery_at = now + timedelta(minutes=6)
        recovery = PostgresSenderRepository(
            self.factory,
            stale_after_seconds=120,
            clock=lambda: recovery_at,
        )
        self.assertEqual(
            (), await recovery.claim_batch_skip_locked(recovery_at, limit=10)
        )
        self.assertEqual(
            (), await recovery.claim_batch_skip_locked(recovery_at, limit=10)
        )
        self.assertEqual(sent, 1)
        async with self.factory() as session:
            quarantined = await session.get(OutboxMessage, outbox.id)
            self.assertIsNotNone(quarantined)
            self.assertEqual(quarantined.dead_at, recovery_at)
            self.assertEqual(quarantined.indeterminate_at, recovery_at)
            self.assertEqual(
                quarantined.last_error_code, "delivery_outcome_indeterminate"
            )

    async def test_catalog_activation_serializes_and_preserves_user_data(self) -> None:
        catalog = await self._load_and_activate_catalog("e2e-preserve-v1")
        telegram_user_id = 8_000_029
        account_repository = AsyncPostgresAccountRepository(self.factory)
        account = await account_repository.get_or_create(telegram_user_id)
        account.mode = BotNotificationMode.LESSONS_AND_SIRENS
        account.region_id = "140101"
        account.region_name = "Kyiv hromada"
        account.reminder_lead_minutes = 11
        account.profiles = {
            "preserved-profile": ScheduleProfile(
                "preserved-profile",
                "Preserved profile",
                overrides=(
                    DateOverride(
                        day=datetime.now(ZoneInfo("Europe/Kyiv")).date(),
                        day_off=False,
                        lessons=(Lesson(time(8), time(8, 45), "Preserved lesson"),),
                    ),
                ),
                active=True,
            )
        }
        await account_repository.save(account)
        await PostgresRegionConfirmationService(self.factory, catalog).confirm_region(account)

        alerts = PostgresAlertRepository(self.factory)
        active = OfficialAlertSnapshot(
            update_id="preservation-active",
            fetch_revision=2,
            kind=ProviderSnapshotKind.FULL_200,
            covered_region_ids=frozenset({"14", "1401", "140101"}),
            active_episodes=(AlertEpisode("14", "AIR", "preservation-episode"),),
            fetched_at=datetime.now(UTC),
            is_complete=True,
            is_fresh=True,
        )
        created = await alerts.apply_snapshot_and_poll_state_atomically(
            active,
            AlertPollState(fetch_revision=2, last_modified="preservation-v1"),
        )
        self.assertEqual(len(created), 1)
        self.assertEqual(await self._outbox_count(), 1)

        v2_hash = "b" * 64
        await catalog.load_draft(
            provider="alerts.in.ua",
            catalog_version="e2e-preserve-v2",
            source_revision="e2e-preserve-v2",
            source_sha256=v2_hash,
            rows=self._catalog_rows(renamed=True),
            selectable_count=3,
            hidden_nonselectable_count=0,
            unresolved_total=0,
            reconciliation_report={"unresolved_total": 0},
        )
        confirmation = PostgresRegionConfirmationService(self.factory, catalog)
        await asyncio.gather(
            catalog.activate(
                provider="alerts.in.ua",
                catalog_version="e2e-preserve-v2",
                expected_source_sha256=v2_hash,
                expected_record_count=3,
                expected_unresolved_total=0,
            ),
            confirmation.confirm_region(account),
        )

        await self._restart_database_clients()
        catalog = PostgresTerritoryCatalogRepository(self.factory)
        self.assertEqual(
            await catalog.assert_runtime_ready(provider="alerts.in.ua"),
            "e2e-preserve-v2",
        )
        restored = await AsyncPostgresAccountRepository(self.factory).get_or_create(
            telegram_user_id
        )
        self.assertEqual(restored.mode, BotNotificationMode.LESSONS_AND_SIRENS)
        self.assertEqual(restored.region_id, "140101")
        # Either operation may win the advisory lock.  Both serial orders are
        # valid: confirmation-before-activation keeps the confirmed display
        # label, while activation-before-confirmation refreshes it from v2.
        self.assertIn(restored.region_name, {"Kyiv hromada", "Kyiv hromada updated"})
        self.assertEqual(restored.reminder_lead_minutes, 11)
        self.assertEqual(tuple(restored.profiles), ("preserved-profile",))
        self.assertEqual(
            restored.profiles["preserved-profile"].overrides[0].lessons[0].title,
            "Preserved lesson",
        )

        async with self.factory() as session:
            scope = (
                await session.execute(
                    select(AlertScope).join(User).where(User.telegram_user_id == telegram_user_id)
                )
            ).scalar_one()
            snapshot = await session.get(AlertSnapshot, scope.id)
            episode_count = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(AlertEpisodeRow)
                        .where(AlertEpisodeRow.scope_id == scope.id)
                    )
                ).scalar_one()
            )
            pointer = await session.get(ActiveTerritoryCatalog, "alerts.in.ua")
            old_version = await session.get(
                TerritoryCatalogVersion, ("alerts.in.ua", "e2e-preserve-v1")
            )
            poll = await session.get(PollRow, 1)
        self.assertEqual(scope.territory_provider, "alerts.in.ua")
        self.assertEqual(scope.territory_catalog_version, "e2e-preserve-v2")
        self.assertEqual(scope.applicable_region_ids, ["14", "1401", "140101"])
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.state, "UNKNOWN")
        self.assertIsNone(snapshot.last_fetch_revision)
        self.assertIsNone(snapshot.last_full_active_episodes)
        self.assertEqual(snapshot.processed_update_ids, [])
        self.assertFalse(snapshot.pending_clear_candidate)
        self.assertEqual(episode_count, 0)
        self.assertIsNotNone(pointer)
        self.assertEqual(pointer.catalog_version, "e2e-preserve-v2")
        self.assertIsNotNone(old_version)
        self.assertEqual(old_version.status, "RETIRED")
        self.assertIsNotNone(poll)
        self.assertEqual(poll.fetch_revision, 3)
        self.assertIsNone(poll.last_modified)
        self.assertIsNone(poll.last_success_at)
        self.assertEqual(await self._outbox_count(), 1)
        sender_repository = PostgresSenderRepository(
            self.factory,
            stale_after_seconds=120,
            clock=lambda: datetime.now(UTC),
        )
        self.assertEqual(
            await sender_repository.claim_batch_skip_locked(datetime.now(UTC), limit=10),
            (),
        )
        async with self.factory() as session:
            stale_outbox = (await session.execute(select(OutboxMessage))).scalar_one()
        self.assertIsNotNone(stale_outbox.dead_at)
        self.assertEqual(stale_outbox.last_error_code, "delivery_policy_changed")

    async def test_account_delete_serializes_with_sender_and_prevents_later_send(self) -> None:
        target_chat_id = 8_000_041
        other_chat_id = 8_000_042
        now = datetime.now(UTC)
        accounts = AsyncPostgresAccountRepository(self.factory)
        target = await accounts.get_or_create(target_chat_id)
        target.mode = BotNotificationMode.LESSONS
        target.profiles = {
            "delete-race": ScheduleProfile("delete-race", "Delete race", active=True)
        }
        await accounts.save(target)
        other = await accounts.get_or_create(other_chat_id)
        other.mode = BotNotificationMode.LESSONS
        other.profiles = {
            "config-race": ScheduleProfile("config-race", "Config race", active=True)
        }
        await accounts.save(other)

        async with self.factory() as session:
            async with session.begin():
                session.add_all(
                    (
                        RuntimeSetting(
                            key="owner_configuration_state",
                            value={"active_revision": 1},
                            updated_by="e2e",
                        ),
                        RuntimeSetting(
                            key="owner_configuration_active",
                            value={"revision": 1, "staging_allowlist": [target_chat_id]},
                            updated_by="e2e",
                        ),
                    )
                )

        async with self.factory() as session:
            async with session.begin():
                target_row = (
                    await session.execute(
                        select(User).where(User.telegram_user_id == target_chat_id)
                    )
                ).scalar_one()
                outbox = OutboxMessage(
                    event_key="delete-race-reminder",
                    aggregate_type="schedule",
                    aggregate_id=str(target_row.id),
                    event_type="LESSON_REMINDER",
                    payload={
                        "lesson_title": "Race lesson",
                        "profile_revision": 1,
                        "starts_at": (now + timedelta(minutes=5)).isoformat(),
                    },
                    available_at=now,
                    claimed_at=now,
                )
                session.add(outbox)
                await session.flush()
                message_id = str(outbox.id)

        repository = PostgresSenderRepository(
            self.factory,
            stale_after_seconds=120,
            expected_owner_config_revision=1,
            expected_staging_allowlist=(target_chat_id,),
            clock=lambda: now,
        )
        entered_transport = asyncio.Event()
        release_transport = asyncio.Event()
        sent = 0

        async def deliver() -> None:
            nonlocal sent
            entered_transport.set()
            await release_transport.wait()
            sent += 1

        delivery_task = asyncio.create_task(
            repository.deliver_if_authorized(
                message_id,
                now,
                deliver,
                expected_recipient=DeliveryRecipient(target_chat_id),
            )
        )
        await asyncio.wait_for(entered_transport.wait(), timeout=5)

        async def activate_next_revision() -> None:
            async with self.factory() as session:
                async with session.begin():
                    state = (
                        await session.execute(
                            select(RuntimeSetting)
                            .where(RuntimeSetting.key == "owner_configuration_state")
                            .with_for_update()
                        )
                    ).scalar_one()
                    active = (
                        await session.execute(
                            select(RuntimeSetting)
                            .where(RuntimeSetting.key == "owner_configuration_active")
                            .with_for_update()
                        )
                    ).scalar_one()
                    state.value = {"active_revision": 2}
                    active.value = {
                        "revision": 2,
                        "staging_allowlist": [other_chat_id],
                    }

        activation_task = asyncio.create_task(activate_next_revision())
        delete_task = asyncio.create_task(accounts.delete_account(target_chat_id))
        await asyncio.sleep(0.05)
        self.assertFalse(activation_task.done())
        self.assertFalse(delete_task.done())
        release_transport.set()

        self.assertTrue(await asyncio.wait_for(delivery_task, timeout=5))
        await asyncio.wait_for(activation_task, timeout=5)
        self.assertTrue(await asyncio.wait_for(delete_task, timeout=5))
        self.assertEqual(sent, 1)

        async with self.factory() as session:
            async with session.begin():
                other_row = (
                    await session.execute(
                        select(User).where(User.telegram_user_id == other_chat_id)
                    )
                ).scalar_one()
                stale_sender_message = OutboxMessage(
                    event_key="stale-config-reminder",
                    aggregate_type="schedule",
                    aggregate_id=str(other_row.id),
                    event_type="LESSON_REMINDER",
                    payload={
                        "lesson_title": "Config race lesson",
                        "profile_revision": 1,
                        "starts_at": (now + timedelta(minutes=5)).isoformat(),
                    },
                    available_at=now,
                    claimed_at=now,
                )
                session.add(stale_sender_message)
                await session.flush()
                stale_message_id = str(stale_sender_message.id)

        stale_callback_called = False

        async def stale_delivery() -> None:
            nonlocal stale_callback_called
            stale_callback_called = True

        self.assertFalse(
            await repository.deliver_if_authorized(
                stale_message_id,
                now,
                stale_delivery,
                expected_recipient=DeliveryRecipient(other_chat_id),
            )
        )
        self.assertFalse(stale_callback_called)

        async with self.factory() as session:
            target_count = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(User)
                        .where(User.telegram_user_id == target_chat_id)
                    )
                ).scalar_one()
            )
            other_count = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(User)
                        .where(User.telegram_user_id == other_chat_id)
                    )
                ).scalar_one()
            )
            orphan_count = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(OutboxMessage)
                        .where(OutboxMessage.event_key == "delete-race-reminder")
                    )
                ).scalar_one()
            )
        self.assertEqual((target_count, other_count, orphan_count), (0, 1, 0))

        callback_called = False

        async def forbidden_delivery() -> None:
            nonlocal callback_called
            callback_called = True

        with self.assertRaises(LookupError):
            await repository.deliver_if_authorized(
                message_id,
                now,
                forbidden_delivery,
                expected_recipient=DeliveryRecipient(target_chat_id),
            )
        self.assertFalse(callback_called)


if __name__ == "__main__":
    unittest.main()
