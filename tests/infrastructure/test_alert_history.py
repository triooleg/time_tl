"""Real PostgreSQL tests restricted to an explicit isolated loopback database."""

import asyncio
import importlib.util
import os
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import select, text

from time_tl.application.sender_worker import DeliveryRecipient
from time_tl.domain.alert_statistics import KYIV
from time_tl.domain.alerts import AlertEpisode as DomainEpisode
from time_tl.domain.alerts import (
    AlertSnapshot,
    AlertState,
    OfficialAlertSnapshot,
    ProviderSnapshotKind,
    TransitionKind,
)
from time_tl.infrastructure.alerts.repositories import AlertPollState
from time_tl.infrastructure.db.alert_history import (
    PostgresAlertReportPlanner,
    assert_alert_history_ready,
    daily_report_deliverable,
    record_snapshot,
    record_source_gap,
)
from time_tl.infrastructure.db.alerts import PostgresAlertRepository
from time_tl.infrastructure.db.base import Base
from time_tl.infrastructure.db.engine import create_engine, create_session_factory
from time_tl.infrastructure.db.models import (
    AlertEpisode as EpisodeRow,
)
from time_tl.infrastructure.db.models import (
    AlertHistoryCursor,
    AlertHistoryPeriod,
    AlertHistorySegment,
    AlertScope,
    OutboxMessage,
    Territory,
    TerritoryCatalogVersion,
    User,
)
from time_tl.infrastructure.db.models import (
    AlertPollState as PollRow,
)
from time_tl.infrastructure.db.models import (
    AlertSnapshot as SnapshotRow,
)

DSN = os.environ.get("ALERT_HISTORY_TEST_DSN", "")
MIGRATION_FILE = (
    Path(__file__).resolve().parents[2] / "migrations" / "versions" / "0015_alert_history.py"
)


@unittest.skipUnless(DSN, "Explicit isolated PostgreSQL required")
class AlertHistoryPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if DSN != "postgresql+asyncpg://postgres@127.0.0.1:5432/time_tl_alert_history_test":
            raise RuntimeError("Refusing a non-isolated alert history test database")
        self.engine = create_engine(DSN)
        self.factory = create_session_factory(self.engine)
        async with self.engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA public CASCADE"))
            await connection.execute(text("CREATE SCHEMA public"))
            await connection.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync,
                    tables=[
                        User.__table__,
                        TerritoryCatalogVersion.__table__,
                        Territory.__table__,
                        AlertScope.__table__,
                        OutboxMessage.__table__,
                        AlertHistoryPeriod.__table__,
                        AlertHistorySegment.__table__,
                        AlertHistoryCursor.__table__,
                        PollRow.__table__,
                        SnapshotRow.__table__,
                        EpisodeRow.__table__,
                    ],
                )
            )
            await connection.execute(
                text("CREATE TABLE alembic_version(version_num varchar(32) PRIMARY KEY)")
            )
            await connection.execute(
                text("INSERT INTO alembic_version VALUES('0015_alert_history')")
            )
        self.user_id, self.scope_id = uuid4(), uuid4()
        async with self.factory() as session, session.begin():
            session.add(
                TerritoryCatalogVersion(
                    provider="alerts.in.ua",
                    catalog_version="test",
                    status="ACTIVE",
                    source_revision="fixture",
                    source_sha256="0" * 64,
                    record_count=1,
                    selectable_count=1,
                    hidden_nonselectable_count=0,
                    unresolved_total=0,
                )
            )
            await session.flush()
            session.add(
                Territory(
                    provider="alerts.in.ua",
                    catalog_version="test",
                    region_id="31",
                    name="TEST",
                    kind="HROMADA",
                    sort_key="test",
                )
            )
            session.add(
                User(
                    id=self.user_id,
                    telegram_user_id=42,
                    mode="ALERTS_ONLY",
                    confirmed_region_id="31",
                    region_name="TEST",
                )
            )
            await session.flush()
            session.add(
                AlertScope(
                    id=self.scope_id,
                    user_id=self.user_id,
                    selected_region_id="31",
                    applicable_region_ids=["31", "14"],
                    territory_provider="alerts.in.ua",
                    territory_catalog_version="test",
                )
            )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def tick(self, at, state, kind=TransitionKind.NONE):
        async with self.factory() as session, session.begin():
            scope = await session.get(AlertScope, self.scope_id)
            return await record_snapshot(
                session, scope, None, AlertSnapshot(state=state), SimpleNamespace(kind=kind), at
            )

    async def test_observed_start_and_confirmed_clear_duration_survives_new_session(self):
        at = datetime(2026, 11, 7, 18, tzinfo=KYIV)
        await self.tick(at, AlertState.CLEAR_CONFIRMED)
        await self.tick(at + timedelta(seconds=30), AlertState.ACTIVE, TransitionKind.ALERT_STARTED)
        await self.tick(at + timedelta(seconds=60), AlertState.ACTIVE)
        timing = await self.tick(
            at + timedelta(seconds=90),
            AlertState.CLEAR_CONFIRMED,
            TransitionKind.ALL_CLEAR_CONFIRMED,
        )
        self.assertEqual(timing["duration_seconds"], 60)
        self.assertTrue(timing["duration_is_approximate"])
        async with self.factory() as session:
            periods = (await session.scalars(select(AlertHistoryPeriod))).all()
            self.assertEqual(len(periods), 1)
            self.assertFalse(periods[0].has_gaps)
            self.assertIsNotNone(periods[0].ended_at)

    async def test_unknown_does_not_close_or_count_unobserved_gap(self):
        at = datetime(2026, 11, 7, 18, tzinfo=KYIV)
        await self.tick(at, AlertState.CLEAR_CONFIRMED)
        await self.tick(at + timedelta(seconds=30), AlertState.ACTIVE, TransitionKind.ALERT_STARTED)
        await self.tick(at + timedelta(seconds=60), AlertState.ACTIVE)
        async with self.factory() as session, session.begin():
            await record_source_gap(session, self.scope_id)
        async with self.factory() as session:
            period = (await session.scalars(select(AlertHistoryPeriod))).one()
            self.assertIsNone(period.ended_at)
        await self.tick(at + timedelta(minutes=20), AlertState.ACTIVE)
        timing = await self.tick(
            at + timedelta(minutes=21),
            AlertState.CLEAR_CONFIRMED,
            TransitionKind.ALL_CLEAR_CONFIRMED,
        )
        self.assertIsNone(timing["duration_seconds"])
        self.assertEqual(timing["duration_unknown_reason"], "source_gap")
        async with self.factory() as session:
            active = (
                await session.scalars(select(AlertHistorySegment).where(AlertHistorySegment.active))
            ).all()
            self.assertEqual(sum((r.ended_at - r.started_at).total_seconds() for r in active), 90)

    async def test_bootstrap_active_is_explicitly_unknown_at_clear(self):
        at = datetime(2026, 11, 7, 18, tzinfo=KYIV)
        await self.tick(at, AlertState.ACTIVE, TransitionKind.ALERT_STARTED)
        timing = await self.tick(
            at + timedelta(seconds=30),
            AlertState.CLEAR_CONFIRMED,
            TransitionKind.ALL_CLEAR_CONFIRMED,
        )
        self.assertIsNone(timing["duration_seconds"])
        self.assertEqual(timing["duration_unknown_reason"], "start_unobserved")

    async def test_report_outbox_once_per_anchored_window_and_final_consent(self):
        at = datetime(2026, 11, 7, 21, 58, tzinfo=KYIV)
        await self.tick(at, AlertState.CLEAR_CONFIRMED)
        await self.tick(at + timedelta(seconds=30), AlertState.ACTIVE, TransitionKind.ALERT_STARTED)
        await self.tick(
            at + timedelta(seconds=90),
            AlertState.CLEAR_CONFIRMED,
            TransitionKind.ALL_CLEAR_CONFIRMED,
        )
        await self.tick(at + timedelta(minutes=2, seconds=10), AlertState.CLEAR_CONFIRMED)
        now = at + timedelta(minutes=3)
        planner = PostgresAlertReportPlanner(self.factory)
        self.assertEqual(await planner.plan(now), 1)
        self.assertEqual(
            await PostgresAlertReportPlanner(self.factory).plan(now + timedelta(seconds=20)), 0
        )
        async with self.factory() as session, session.begin():
            row = (await session.scalars(select(OutboxMessage))).one()
            self.assertEqual(row.payload["alert_count"], 1)
            self.assertEqual(row.payload["duration_seconds"], 60)
            self.assertTrue(row.payload["is_partial"])
            self.assertTrue(
                await daily_report_deliverable(
                    session, row, now, expected_recipient=DeliveryRecipient(42, "31", "TEST")
                )
            )
            self.assertFalse(
                await daily_report_deliverable(
                    session, row, now, expected_recipient=DeliveryRecipient(43, "31", "TEST")
                )
            )
            self.assertFalse(
                await daily_report_deliverable(session, row, now + timedelta(minutes=10))
            )
            user = await session.get(User, self.user_id)
            user.mode = "LESSONS_ONLY"
            await session.flush()
            self.assertFalse(await daily_report_deliverable(session, row, now))

    async def test_region_binding_change_does_not_transfer_history(self):
        at = datetime(2026, 11, 7, 21, 58, tzinfo=KYIV)
        await self.tick(at, AlertState.CLEAR_CONFIRMED)
        await self.tick(at + timedelta(seconds=30), AlertState.ACTIVE, TransitionKind.ALERT_STARTED)
        async with self.factory() as session, session.begin():
            scope = await session.get(AlertScope, self.scope_id)
            scope.applicable_region_ids = ["31"]
        await self.tick(at + timedelta(minutes=1), AlertState.ACTIVE)
        async with self.factory() as session, session.begin():
            scope = await session.get(AlertScope, self.scope_id)
            scope.applicable_region_ids = ["31", "14"]
        await self.tick(at + timedelta(seconds=90), AlertState.ACTIVE)
        timing = await self.tick(
            at + timedelta(minutes=2),
            AlertState.CLEAR_CONFIRMED,
            TransitionKind.ALL_CLEAR_CONFIRMED,
        )
        self.assertIsNone(timing["duration_seconds"])
        self.assertEqual(timing["duration_unknown_reason"], "start_unobserved")
        await PostgresAlertReportPlanner(self.factory).plan(at + timedelta(minutes=3))
        async with self.factory() as session:
            row = (await session.scalars(select(OutboxMessage))).one()
            self.assertEqual(row.payload["ongoing_count"], 0)
            periods = (await session.scalars(select(AlertHistoryPeriod))).all()
            abandoned = [period for period in periods if period.tracking_ended_at is not None]
            self.assertEqual(len(abandoned), 2)
            self.assertTrue(all(period.ended_at is None for period in abandoned))

    async def test_schema_guard_checks_migration_and_types(self):
        await assert_alert_history_ready(self.factory)
        async with self.engine.begin() as connection:
            await connection.execute(
                text("UPDATE alembic_version SET version_num='0014_calendar_reminders'")
            )
        with self.assertRaisesRegex(RuntimeError, "schema_not_ready"):
            await assert_alert_history_ready(self.factory)

    async def test_real_repository_preserves_two_poll_clear_and_attaches_duration(self):
        at = datetime(2026, 11, 7, 18, tzinfo=KYIV)
        async with self.factory() as session, session.begin():
            session.add(
                PollRow(
                    singleton_id=1, fetch_revision=0, circuit_open=False, consecutive_failures=0
                )
            )
        repository = PostgresAlertRepository(self.factory, history_enabled=True)
        parent = DomainEpisode("14", "air_raid", "parent-episode", "yellow")
        child = DomainEpisode("31", "air_raid", "child-episode", "red")
        for revision, episodes in enumerate(((), (parent, child), (parent,), (), (), ()), start=1):
            now = at + timedelta(seconds=30 * (revision - 1))
            update = OfficialAlertSnapshot(
                update_id=f"test-{revision}",
                fetch_revision=revision,
                kind=ProviderSnapshotKind.FULL_200,
                covered_region_ids=frozenset({"14", "31"}),
                active_episodes=episodes,
                fetched_at=now,
                is_complete=True,
                is_fresh=True,
            )
            rows = await repository.apply_snapshot_and_poll_state_atomically(
                update, AlertPollState(fetch_revision=revision)
            )
            if revision == 4:
                self.assertEqual(rows, ())
                async with self.factory() as session:
                    period = (await session.scalars(select(AlertHistoryPeriod))).one()
                    self.assertIsNone(period.ended_at)
        async with self.factory() as session:
            rows = (
                await session.scalars(select(OutboxMessage).order_by(OutboxMessage.created_at))
            ).all()
            self.assertEqual(
                sorted(row.event_type for row in rows), ["ALERT_STARTED", "ALL_CLEAR_CONFIRMED"]
            )
            started = next(row for row in rows if row.event_type == "ALERT_STARTED")
            self.assertEqual(started.payload["alert_level"], "red")
            clear = next(row for row in rows if row.event_type == "ALL_CLEAR_CONFIRMED")
            self.assertNotIn("alert_level", clear.payload)
            self.assertEqual(clear.payload["duration_seconds"], 90)
            self.assertTrue(clear.payload["duration_is_approximate"])
            periods = (await session.scalars(select(AlertHistoryPeriod))).all()
            self.assertEqual(len(periods), 1)
            self.assertFalse(periods[0].has_gaps)

    async def test_complete_alembic_runner_upgrades_and_reverses_head14(self):
        from alembic import command
        from alembic.config import Config

        async with self.engine.begin() as connection:

            def prepare_full_head14(sync):
                for model in (AlertHistoryCursor, AlertHistorySegment, AlertHistoryPeriod):
                    model.__table__.drop(sync)
                legacy = [
                    table
                    for table in Base.metadata.sorted_tables
                    if not table.name.startswith("alert_history_")
                ]
                Base.metadata.create_all(sync, tables=legacy)

            await connection.run_sync(prepare_full_head14)
            await connection.execute(
                text("UPDATE alembic_version SET version_num='0014_calendar_reminders'")
            )
        config = Config()
        config.set_main_option("script_location", str(MIGRATION_FILE.parents[1]))
        config.set_main_option("sqlalchemy.url", DSN)
        await asyncio.to_thread(command.upgrade, config, "0015_alert_history")
        await assert_alert_history_ready(self.factory)
        async with self.factory() as session:
            self.assertEqual(
                (await session.execute(text("SELECT count(*) FROM users"))).scalar_one(), 1
            )
            self.assertEqual(
                (
                    await session.execute(text("SELECT count(*) FROM alert_history_periods"))
                ).scalar_one(),
                0,
            )
        await asyncio.to_thread(command.upgrade, config, "head")
        await asyncio.to_thread(command.downgrade, config, "0014_calendar_reminders")
        with self.assertRaisesRegex(RuntimeError, "schema_not_ready"):
            await assert_alert_history_ready(self.factory)
        await asyncio.to_thread(command.upgrade, config, "head")
        await assert_alert_history_ready(self.factory)

    async def test_actual_0015_forward_ddl_on_existing_0014_schema(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        specification = importlib.util.spec_from_file_location(
            "native_history_migration", MIGRATION_FILE
        )
        migration = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(migration)
        async with self.engine.begin() as connection:

            def run_ddl(sync):
                for model in (AlertHistoryCursor, AlertHistorySegment, AlertHistoryPeriod):
                    model.__table__.drop(sync)
                with Operations.context(MigrationContext.configure(sync)):
                    migration.upgrade()
                    # Re-running verifies fresh0001 metadata compatibility too.
                    migration.upgrade()

            await connection.execute(
                text("UPDATE alembic_version SET version_num='0014_calendar_reminders'")
            )
            await connection.run_sync(run_ddl)
            # Standalone DDL test: Alembic's runner normally advances this row.
            await connection.execute(
                text("UPDATE alembic_version SET version_num='0015_alert_history'")
            )
        await assert_alert_history_ready(self.factory)

    async def test_invalid_one_target_rolls_back_its_savepoint_and_keeps_other_users(self):
        from time_tl.infrastructure.db import alert_history

        other_id, other_scope = uuid4(), uuid4()
        async with self.factory() as session, session.begin():
            session.add(
                User(
                    id=other_id,
                    telegram_user_id=43,
                    mode="ALERTS_ONLY",
                    confirmed_region_id="31",
                    region_name="TEST",
                )
            )
            await session.flush()
            session.add(
                AlertScope(
                    id=other_scope,
                    user_id=other_id,
                    selected_region_id="31",
                    applicable_region_ids=["31"],
                    territory_provider="alerts.in.ua",
                    territory_catalog_version="test",
                )
            )
        original = alert_history._statistics
        calls = 0

        async def injected(session, scope, start, end):
            nonlocal calls
            calls += 1
            if calls == 1:
                session.add(
                    OutboxMessage(
                        event_key="must-rollback",
                        aggregate_type="alert_scope",
                        aggregate_id=str(scope.id),
                        event_type="ALERT_DAILY_SUMMARY",
                        payload={},
                    )
                )
                await session.flush()
                raise ValueError("test-invalid-derived-history")
            return await original(session, scope, start, end)

        with patch.object(alert_history, "_statistics", injected):
            self.assertEqual(
                await PostgresAlertReportPlanner(self.factory).plan(
                    datetime(2026, 11, 7, 22, 1, tzinfo=KYIV)
                ),
                1,
            )
        async with self.factory() as session:
            rows = (await session.scalars(select(OutboxMessage))).all()
            self.assertEqual(len(rows), 1)
            self.assertNotEqual(rows[0].event_key, "must-rollback")

    async def _change_binding_after_report(self, state):
        now = datetime(2026, 11, 7, 22, 1, tzinfo=KYIV)
        self.assertEqual(await PostgresAlertReportPlanner(self.factory).plan(now), 1)
        async with self.factory() as session, session.begin():
            row = (await session.scalars(select(OutboxMessage))).one()
            if state == "delivered":
                row.delivered_at = now
                row.dispatch_started_at = now
                row.claimed_at = now
            elif state == "ambiguous":
                row.dispatch_started_at = now
                row.claimed_at = now
                row.indeterminate_at = now
                row.dead_at = now
            else:
                row.dead_at = now
            scope = await session.get(AlertScope, self.scope_id)
            scope.applicable_region_ids = ["31"]
        return now

    async def test_delivered_old_binding_never_repeats_same_daily_window(self):
        now = await self._change_binding_after_report("delivered")
        self.assertEqual(
            await PostgresAlertReportPlanner(self.factory).plan(now + timedelta(seconds=10)), 0
        )

    async def test_ambiguous_old_binding_never_repeats_same_daily_window(self):
        now = await self._change_binding_after_report("ambiguous")
        self.assertEqual(
            await PostgresAlertReportPlanner(self.factory).plan(now + timedelta(seconds=10)), 0
        )

    async def test_unadmitted_cancelled_binding_can_be_replaced_but_final_fence_rechecks(self):
        now = await self._change_binding_after_report("cancelled")
        self.assertEqual(
            await PostgresAlertReportPlanner(self.factory).plan(now + timedelta(seconds=10)), 1
        )
        async with self.factory() as session, session.begin():
            rows = (await session.scalars(select(OutboxMessage))).all()
            old = next(row for row in rows if row.dead_at is not None)
            new = next(row for row in rows if row.dead_at is None)
            self.assertFalse(await daily_report_deliverable(session, old, now, lock=True))
            self.assertTrue(await daily_report_deliverable(session, new, now, lock=True))
            old.claimed_at = now
            await session.flush()
            self.assertFalse(await daily_report_deliverable(session, new, now, lock=True))


if __name__ == "__main__":
    unittest.main()
