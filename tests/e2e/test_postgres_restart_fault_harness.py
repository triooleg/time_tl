from __future__ import annotations

import asyncio
import os
import unittest
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from alembic import command
from alembic.config import Config
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import DBAPIError

from time_tl.application.schedule_worker import ScheduleWorker
from time_tl.application.sender_worker import SenderWorker
from time_tl.bot.models import NotificationMode as BotNotificationMode
from time_tl.domain.alerts import (
    AlertEpisode,
    AlertState,
    OfficialAlertSnapshot,
    ProviderSnapshotKind,
    RegionScope,
    SourceFailure,
    SourceFailureKind,
)
from time_tl.domain.schedule import DateOverride, Lesson, ScheduleProfile
from time_tl.infrastructure.alerts.repositories import AlertPollState
from time_tl.infrastructure.db.accounts import AsyncPostgresAccountRepository
from time_tl.infrastructure.db.alerts import PostgresAlertRepository
from time_tl.infrastructure.db.base import Base
from time_tl.infrastructure.db.engine import create_engine, create_session_factory
from time_tl.infrastructure.db.models import AlertPollState as PollRow
from time_tl.infrastructure.db.models import AlertSnapshot, OutboxMessage
from time_tl.infrastructure.db.workers import (
    PostgresScheduleWorkerRepository,
    PostgresSenderRepository,
)
from time_tl.infrastructure.telegram import TelegramMessage

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
            control_engine = create_engine(
                self._database_url, pool_size=1, max_overflow=0
            )
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
        command.upgrade(config, "head")

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
                preparer.quote(table.name)
                for table in reversed(Base.metadata.sorted_tables)
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

    async def test_atomic_rollback_restart_two_clear_return_and_sender_dedupe(self) -> None:
        telegram_user_id = 8_000_021
        base = datetime.now(UTC)
        worker_now = base + timedelta(seconds=20)
        worker_local = worker_now.astimezone(ZoneInfo("Europe/Kyiv"))
        if (worker_local.hour, worker_local.minute) == (23, 59):
            worker_now += timedelta(minutes=1)
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

        alerts = PostgresAlertRepository(self.factory)
        scope = RegionScope.from_hierarchy("14")
        await alerts.confirm_region_scope_for_telegram_user(
            telegram_user_id,
            confirmed_region_id="14",
            region_name="Київська область",
            scope=scope,
        )

        episode = AlertEpisode("14", "AIR", "e2e-episode")
        active = OfficialAlertSnapshot(
            update_id="e2e-active-1",
            fetch_revision=1,
            kind=ProviderSnapshotKind.FULL_200,
            covered_region_ids=frozenset({"14"}),
            active_episodes=(episode,),
            fetched_at=base,
            is_complete=True,
            is_fresh=True,
        )
        created = await alerts.apply_snapshot_and_poll_state_atomically(
            active, AlertPollState(fetch_revision=1, last_modified="active-1")
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
            fetch_revision=2,
            kind=ProviderSnapshotKind.FULL_200,
            covered_region_ids=frozenset({"14"}),
            active_episodes=(),
            fetched_at=worker_now - timedelta(seconds=10),
            is_complete=True,
            is_fresh=True,
        )
        await alerts.apply_snapshot_and_poll_state_atomically(
            first_clear, AlertPollState(fetch_revision=2, last_modified="clear-1")
        )
        self.assertEqual(await self._snapshot_state(), AlertState.STALE)
        self.assertEqual(await self._outbox_count("ALL_CLEAR_CONFIRMED"), 0)

        second_clear = OfficialAlertSnapshot(
            update_id="e2e-clear-confirmed",
            fetch_revision=3,
            kind=ProviderSnapshotKind.FULL_200,
            covered_region_ids=frozenset({"14"}),
            active_episodes=(),
            fetched_at=worker_now,
            is_complete=True,
            is_fresh=True,
        )
        await alerts.apply_snapshot_and_poll_state_atomically(
            second_clear, AlertPollState(fetch_revision=3, last_modified="clear-2")
        )
        self.assertEqual(await self._snapshot_state(), AlertState.CLEAR_CONFIRMED)
        self.assertEqual(await self._outbox_count("ALL_CLEAR_CONFIRMED"), 1)

        def faulting_factory():
            return FaultingSessionContext(self.factory, DATABASE_URL)

        faulting_alerts = PostgresAlertRepository(faulting_factory)  # type: ignore[arg-type]
        rolled_back_realert = OfficialAlertSnapshot(
            update_id="e2e-realert-must-rollback",
            fetch_revision=4,
            kind=ProviderSnapshotKind.FULL_200,
            covered_region_ids=frozenset({"14"}),
            active_episodes=(AlertEpisode("14", "AIR", "e2e-episode-2"),),
            fetched_at=worker_now + timedelta(seconds=1),
            is_complete=True,
            is_fresh=True,
        )
        with self.assertRaises(DBAPIError):
            await faulting_alerts.apply_snapshot_and_poll_state_atomically(
                rolled_back_realert,
                AlertPollState(fetch_revision=4, last_modified="must-rollback"),
            )

        await self._restart_database_clients()
        async with self.factory() as session:
            poll = await session.get(PollRow, 1)
            self.assertIsNotNone(poll)
            self.assertEqual(poll.fetch_revision, 3)
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
        self.assertTrue(any("підключайтеся" in item.text for item in recording.sent))
        self.assertTrue(any("Відбій" in item.text for item in recording.sent))


if __name__ == "__main__":
    unittest.main()
