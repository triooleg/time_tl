from __future__ import annotations

import time
import unittest
from datetime import UTC, datetime, timedelta
from datetime import time as wall_time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from time_tl.domain.alerts import (
    ActiveEpisode,
    AlertState,
    OfficialAlertSnapshot,
    ProviderSnapshotKind,
    RegionScope,
)
from time_tl.domain.alerts import AlertEpisode as DomainEpisode
from time_tl.domain.alerts import AlertSnapshot as DomainSnapshot
from time_tl.domain.schedule import Lesson, ScheduleProfile
from time_tl.infrastructure.alerts.repositories import AlertPollState
from time_tl.infrastructure.db.accounts import AsyncPostgresAccountRepository
from time_tl.infrastructure.db.alerts import PostgresAlertRepository
from time_tl.infrastructure.db.import_sessions import PostgresImportSessionRepository, digest_secret
from time_tl.infrastructure.db.models import (
    AlertPollState as PollRow,
)
from time_tl.infrastructure.db.models import (
    AlertScope,
    AlertSnapshot,
    OutboxMessage,
    PendingImportSession,
    RuleLesson,
    User,
)
from time_tl.infrastructure.db.models import (
    ScheduleProfile as ProfileRow,
)
from time_tl.infrastructure.db.workers import (
    PostgresScheduleWorkerRepository,
    PostgresSenderRepository,
    PostgresWorkerHeartbeatRepository,
    _provider_is_fresh,
)

AUTHORIZATION_AT = datetime(2026, 9, 21, 6, tzinfo=UTC)
STALE_AFTER = timedelta(seconds=120)


class Result:
    def __init__(self, *, scalar=None, scalars=()):
        self._scalar = scalar
        self._scalars = tuple(scalars)

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return list(self._scalars)

    def __iter__(self):
        return iter(self._scalars)


class ProfileDiffSession:
    def __init__(self, existing):
        self.existing = existing
        self.statements = []
        self.added = []
        self.flush = AsyncMock()

    async def execute(self, statement):
        self.statements.append(statement)
        if len(self.statements) == 1:
            return Result(scalars=self.existing)
        return Result()

    def add(self, row):
        self.added.append(row)


class AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


class PollSession:
    def __init__(self, row):
        self.row = row
        self.begin_count = 0

    def begin(self):
        self.begin_count += 1
        return AsyncContext(None)

    async def get(self, model, key):
        return self.row

    async def execute(self, _statement):
        return Result(scalar=self.row)


class MutationSession:
    def __init__(self):
        self.added = []
        self.statements = []
        self.flush = AsyncMock()

    def add(self, value):
        self.added.append(value)

    async def execute(self, statement):
        self.statements.append(statement)
        return Result()


class HeartbeatSession(MutationSession):
    def begin(self):
        return AsyncContext(None)


class SnapshotSession:
    def __init__(self, poll_row):
        self.poll_row = poll_row
        self.calls = 0

    def begin(self):
        return AsyncContext(None)

    async def execute(self, _statement):
        self.calls += 1
        if self.calls == 1:
            return Result(scalar=self.poll_row)
        return Result(scalars=())


class DeliverySession:
    def __init__(self, objects, *, query_results=()):
        self.objects = objects
        self.query_results = list(query_results)

    async def get(self, model, _key):
        return self.objects.get(model)

    async def execute(self, _statement):
        value = self.query_results.pop(0) if self.query_results else None
        return Result(scalar=value)


class FinalDeliverySession:
    def __init__(self, objects):
        self.objects = objects

    def begin(self):
        return AsyncContext(None)

    async def get(self, model, _key):
        return self.objects.get(model)

    async def execute(self, statement):
        descriptions = statement.column_descriptions
        entity = descriptions[0].get("entity") if descriptions else None
        return Result(scalar=self.objects.get(entity))


class SafetyPersistenceRegressionTests(unittest.IsolatedAsyncioTestCase):
    def test_provider_freshness_boundary_is_inclusive_then_fails_closed(self):
        last_success = AUTHORIZATION_AT - STALE_AFTER
        self.assertTrue(
            _provider_is_fresh(last_success, AUTHORIZATION_AT, STALE_AFTER)
        )
        self.assertFalse(
            _provider_is_fresh(
                last_success,
                AUTHORIZATION_AT + timedelta(microseconds=1),
                STALE_AFTER,
            )
        )
        self.assertFalse(_provider_is_fresh(None, AUTHORIZATION_AT, STALE_AFTER))

    async def test_stale_provider_suppresses_confirmed_clear_schedule_scan(self):
        poll = PollRow(singleton_id=1, fetch_revision=4, circuit_open=False)
        poll.last_success_at = AUTHORIZATION_AT - STALE_AFTER - timedelta(seconds=1)
        session = DeliverySession({PollRow: poll})
        repo = PostgresScheduleWorkerRepository(  # type: ignore[arg-type]
            lambda: AsyncContext(session), stale_after_seconds=120
        )

        self.assertEqual((), await repo.list_confirmed_clears(AUTHORIZATION_AT))

    async def test_validated_snapshot_persists_provider_success_timestamp(self):
        poll_row = PollRow(singleton_id=1, fetch_revision=0, circuit_open=False)
        poll_row.last_success_at = None
        session = SnapshotSession(poll_row)
        repo = PostgresAlertRepository(lambda: AsyncContext(session))  # type: ignore[arg-type]

        update = OfficialAlertSnapshot(
                update_id="validated-empty",
                fetch_revision=1,
                kind=ProviderSnapshotKind.FULL_200,
                covered_region_ids=frozenset(),
                active_episodes=(),
                fetched_at=datetime(2026, 9, 21, tzinfo=UTC),
                is_complete=True,
                is_fresh=True,
            )
        await repo.apply_snapshot_and_poll_state_atomically(
            update,
            AlertPollState(fetch_revision=1, last_modified="marker"),
        )

        self.assertEqual(poll_row.last_success_at, datetime(2026, 9, 21, tzinfo=UTC))
        self.assertEqual(poll_row.fetch_revision, 1)
        self.assertEqual(poll_row.last_modified, "marker")

    async def test_postgres_restart_invalidates_marker_and_freshness_atomically(self):
        poll_row = PollRow(
            singleton_id=1,
            fetch_revision=7,
            last_modified="persisted-marker",
            circuit_open=False,
        )
        poll_row.last_success_at = AUTHORIZATION_AT
        session = SnapshotSession(poll_row)
        repo = PostgresAlertRepository(lambda: AsyncContext(session))  # type: ignore[arg-type]

        await repo.apply_process_restart_atomically()

        self.assertEqual(session.calls, 2)
        self.assertIsNone(poll_row.last_modified)
        self.assertIsNone(poll_row.last_success_at)
        self.assertEqual(poll_row.fetch_revision, 7)

    async def test_postgres_restart_preserves_active_episode_as_stale_without_outbox(self):
        poll_row = PollRow(
            singleton_id=1,
            fetch_revision=7,
            last_modified="persisted-marker",
            circuit_open=False,
        )
        poll_row.last_success_at = AUTHORIZATION_AT
        scope = SimpleNamespace(
            id=uuid4(),
            selected_region_id="14",
            applicable_region_ids=["14"],
        )

        class RestartSession(SnapshotSession):
            async def execute(self, _statement):
                self.calls += 1
                if self.calls == 1:
                    return Result(scalar=self.poll_row)
                return Result(scalars=(scope,))

        session = RestartSession(poll_row)
        repo = PostgresAlertRepository(lambda: AsyncContext(session))  # type: ignore[arg-type]
        episode = DomainEpisode("14", "air_raid", "oblast-air-raid:14")
        original = DomainSnapshot(
            state=AlertState.ACTIVE,
            active_episodes=(ActiveEpisode(episode, 7),),
            last_full_active_episodes=(episode,),
            processed_update_ids=frozenset({"active-7"}),
            last_fetch_revision=7,
        )
        with (
            patch.object(repo, "_load_snapshot", AsyncMock(return_value=original)),
            patch.object(repo, "_store_snapshot", AsyncMock()) as store,
        ):
            await repo.apply_process_restart_atomically()

        stored = store.await_args.args[2]
        self.assertEqual(stored.state, AlertState.STALE)
        self.assertEqual(tuple(item.episode for item in stored.active_episodes), (episode,))
        self.assertIsNone(stored.last_fetch_revision)
        self.assertIsNone(stored.last_full_active_episodes)
        self.assertEqual(session.calls, 2)
        self.assertIsNone(poll_row.last_success_at)

    async def test_snapshot_poll_state_transaction_rolls_back_injected_commit_failure(self):
        poll_row = PollRow(
            singleton_id=1,
            fetch_revision=7,
            last_modified="old-marker",
            circuit_open=False,
        )
        old_success = datetime(2026, 9, 20, tzinfo=UTC)
        poll_row.last_success_at = old_success

        class RollbackContext:
            async def __aenter__(self):
                return None

            async def __aexit__(self, exc_type, *_args):
                if exc_type is not None:
                    poll_row.fetch_revision = 7
                    poll_row.last_modified = "old-marker"
                    poll_row.last_success_at = old_success
                return False

        session = SnapshotSession(poll_row)
        session.begin = lambda: RollbackContext()  # type: ignore[method-assign]
        repo = PostgresAlertRepository(lambda: AsyncContext(session))  # type: ignore[arg-type]
        incoming = OfficialAlertSnapshot(
            update_id="validated-active",
            fetch_revision=8,
            kind=ProviderSnapshotKind.FULL_200,
            covered_region_ids=frozenset(),
            active_episodes=(),
            fetched_at=datetime(2026, 9, 23, tzinfo=UTC),
            is_complete=True,
            is_fresh=True,
        )

        with patch.object(repo, "_store_poll_state", side_effect=RuntimeError("commit crash")):
            with self.assertRaisesRegex(RuntimeError, "commit crash"):
                await repo.apply_snapshot_and_poll_state_atomically(
                    incoming,
                    AlertPollState(fetch_revision=8, last_modified="new-marker"),
                )

        self.assertEqual(poll_row.fetch_revision, 7)
        self.assertEqual(poll_row.last_modified, "old-marker")
        self.assertEqual(poll_row.last_success_at, old_success)

    async def test_lesson_edit_preserves_exact_owned_storage_reference(self):
        lesson_id = uuid4()
        rule_id = uuid4()
        existing = RuleLesson(
            id=lesson_id,
            rule_id=rule_id,
            position=0,
            start_time=wall_time(9),
            end_time=wall_time(10),
            title="Old",
            enabled=True,
        )
        session = MutationSession()
        repo = AsyncPostgresAccountRepository(None)  # type: ignore[arg-type]

        await repo._sync_lessons(
            session,  # type: ignore[arg-type]
            [existing],
            (
                Lesson(
                    wall_time(9),
                    wall_time(10),
                    "Renamed",
                    storage_ref=f"lesson:rule:{lesson_id}",
                ),
            ),
            parent_id=rule_id,
            kind="rule",
        )

        self.assertEqual(existing.id, lesson_id)
        self.assertEqual(existing.title, "Renamed")
        self.assertEqual(session.added, [])
        self.assertEqual(session.statements, [])

    async def test_sender_revokes_queued_alert_when_current_state_changed(self):
        user = User(id=uuid4(), telegram_user_id=42, mode="LESSONS_AND_ALERTS")
        scope = AlertScope(
            id=uuid4(),
            user_id=user.id,
            selected_region_id="31",
            applicable_region_ids=["31"],
        )
        snapshot = AlertSnapshot(scope_id=scope.id, state=AlertState.CLEAR_CONFIRMED.value)
        message = OutboxMessage(
            aggregate_type="alert_scope",
            aggregate_id=str(scope.id),
            event_key="alert",
            event_type="ALERT_STARTED",
            payload={"telegram_chat_id": 42},
        )

        poll = PollRow(singleton_id=1, fetch_revision=1, circuit_open=False)
        poll.last_success_at = AUTHORIZATION_AT

        async def get(model, key):
            return {
                AlertScope: scope,
                User: user,
                AlertSnapshot: snapshot,
                PollRow: poll,
            }.get(model)

        session = SimpleNamespace(get=get)
        allowed = await PostgresSenderRepository._is_currently_deliverable(
            session,  # type: ignore[arg-type]
            message,
            authorization_at=AUTHORIZATION_AT,
            stale_after=STALE_AFTER,
        )
        self.assertFalse(allowed)
        snapshot.state = AlertState.ACTIVE.value
        self.assertTrue(
            await PostgresSenderRepository._is_currently_deliverable(
                session,  # type: ignore[arg-type]
                message,
                authorization_at=AUTHORIZATION_AT,
                stale_after=STALE_AFTER,
            )
        )

    async def test_lessons_only_revokes_queued_alert_clear_and_return(self):
        user = User(id=uuid4(), telegram_user_id=42, mode="LESSONS_ONLY")
        scope = AlertScope(
            id=uuid4(),
            user_id=user.id,
            selected_region_id="31",
            applicable_region_ids=["31"],
        )
        snapshot = AlertSnapshot(scope_id=scope.id, state=AlertState.ACTIVE.value)
        session = DeliverySession({AlertScope: scope, User: user, AlertSnapshot: snapshot})
        for event_type in ("ALERT_STARTED", "ALL_CLEAR_CONFIRMED"):
            message = OutboxMessage(
                aggregate_type="alert_scope",
                aggregate_id=str(scope.id),
                event_key=event_type,
                event_type=event_type,
                payload={"telegram_chat_id": 42},
            )
            self.assertFalse(
                await PostgresSenderRepository._is_currently_deliverable(
                    session,  # type: ignore[arg-type]
                    message,
                    authorization_at=AUTHORIZATION_AT,
                    stale_after=STALE_AFTER,
                )
            )

        return_message = OutboxMessage(
            aggregate_type="schedule",
            aggregate_id=str(user.id),
            event_key="return",
            event_type="RETURN_TO_ACTIVE_LESSON",
            payload={"telegram_chat_id": 42, "profile_revision": 1},
        )
        self.assertFalse(
            await PostgresSenderRepository._is_currently_deliverable(
                session,  # type: ignore[arg-type]
                return_message,
                authorization_at=AUTHORIZATION_AT,
                stale_after=STALE_AFTER,
            )
        )

    async def test_stale_state_revokes_alert_clear_and_combined_lesson_messages(self):
        user = User(id=uuid4(), telegram_user_id=42, mode="LESSONS_AND_ALERTS")
        scope = AlertScope(
            id=uuid4(),
            user_id=user.id,
            selected_region_id="31",
            applicable_region_ids=["31"],
        )
        snapshot = AlertSnapshot(scope_id=scope.id, state=AlertState.STALE.value)
        for event_type in ("ALERT_STARTED", "ALL_CLEAR_CONFIRMED"):
            session = DeliverySession({AlertScope: scope, User: user, AlertSnapshot: snapshot})
            message = OutboxMessage(
                aggregate_type="alert_scope",
                aggregate_id=str(scope.id),
                event_key=event_type,
                event_type=event_type,
                payload={"telegram_chat_id": 42},
            )
            self.assertFalse(
                await PostgresSenderRepository._is_currently_deliverable(
                    session,  # type: ignore[arg-type]
                    message,
                    authorization_at=AUTHORIZATION_AT,
                    stale_after=STALE_AFTER,
                )
            )

        for event_type in ("LESSON_REMINDER", "RETURN_TO_ACTIVE_LESSON"):
            session = DeliverySession(
                {User: user, AlertSnapshot: snapshot}, query_results=(scope,)
            )
            message = OutboxMessage(
                aggregate_type="schedule",
                aggregate_id=str(user.id),
                event_key=event_type,
                event_type=event_type,
                payload={"telegram_chat_id": 42, "profile_revision": 1},
            )
            self.assertFalse(
                await PostgresSenderRepository._is_currently_deliverable(
                    session,  # type: ignore[arg-type]
                    message,
                    authorization_at=AUTHORIZATION_AT,
                    stale_after=STALE_AFTER,
                )
            )

    async def test_final_authorization_blocks_alert_state_change_after_claim(self):
        now = datetime(2026, 9, 21, 6, tzinfo=UTC)
        user = User(id=uuid4(), telegram_user_id=42, mode="LESSONS_AND_ALERTS")
        scope = AlertScope(
            id=uuid4(),
            user_id=user.id,
            selected_region_id="31",
            applicable_region_ids=["31"],
        )
        snapshot = AlertSnapshot(scope_id=scope.id, state=AlertState.ACTIVE.value)
        message = OutboxMessage(
            id=uuid4(),
            aggregate_type="alert_scope",
            aggregate_id=str(scope.id),
            event_key="claimed-alert",
            event_type="ALERT_STARTED",
            payload={"telegram_chat_id": 42},
            claimed_at=now,
        )
        session = FinalDeliverySession(
            {
                OutboxMessage: message,
                AlertScope: scope,
                User: user,
                AlertSnapshot: snapshot,
                PollRow: PollRow(
                    singleton_id=1,
                    fetch_revision=1,
                    circuit_open=False,
                    last_success_at=now,
                ),
            }
        )
        repo = PostgresSenderRepository(  # type: ignore[arg-type]
            lambda: AsyncContext(session), stale_after_seconds=120, clock=lambda: now
        )
        sent = False

        async def deliver():
            nonlocal sent
            sent = True

        # This transition occurs after claim_batch_skip_locked returned.
        snapshot.state = AlertState.STALE.value
        allowed = await repo.deliver_if_authorized(str(message.id), now, deliver)

        self.assertFalse(allowed)
        self.assertFalse(sent)
        self.assertEqual(message.dead_at, now)
        self.assertEqual(message.last_error_code, "final_delivery_policy_changed")

    async def test_final_authorization_uses_delivery_time_freshness_boundary(self):
        async def attempt(
            last_success_at: datetime,
            *,
            clock_values: tuple[datetime, datetime] = (
                AUTHORIZATION_AT,
                AUTHORIZATION_AT,
            ),
        ):
            user = User(id=uuid4(), telegram_user_id=42, mode="ALERTS_ONLY")
            scope = AlertScope(
                id=uuid4(),
                user_id=user.id,
                selected_region_id="31",
                applicable_region_ids=["31"],
            )
            snapshot = AlertSnapshot(
                scope_id=scope.id, state=AlertState.CLEAR_CONFIRMED.value
            )
            poll = PollRow(singleton_id=1, fetch_revision=5, circuit_open=False)
            poll.last_success_at = last_success_at
            message = OutboxMessage(
                id=uuid4(),
                aggregate_type="alert_scope",
                aggregate_id=str(scope.id),
                event_key=f"clear:{last_success_at.isoformat()}",
                event_type="ALL_CLEAR_CONFIRMED",
                payload={"telegram_chat_id": 42},
                claimed_at=AUTHORIZATION_AT,
            )
            session = FinalDeliverySession(
                {
                    OutboxMessage: message,
                    PollRow: poll,
                    AlertScope: scope,
                    User: user,
                    AlertSnapshot: snapshot,
                }
            )
            current_times = iter(clock_values)
            repo = PostgresSenderRepository(  # type: ignore[arg-type]
                lambda: AsyncContext(session),
                stale_after_seconds=120,
                clock=lambda: next(current_times),
            )
            sent = False

            async def deliver():
                nonlocal sent
                sent = True

            allowed = await repo.deliver_if_authorized(
                str(message.id), AUTHORIZATION_AT, deliver
            )
            return allowed, sent, message

        at_boundary = await attempt(AUTHORIZATION_AT - STALE_AFTER)
        after_boundary = await attempt(
            AUTHORIZATION_AT - STALE_AFTER - timedelta(microseconds=1)
        )
        aged_while_locks_were_acquired = await attempt(
            AUTHORIZATION_AT - STALE_AFTER,
            clock_values=(
                AUTHORIZATION_AT,
                AUTHORIZATION_AT + timedelta(microseconds=1),
            ),
        )

        self.assertEqual((True, True), at_boundary[:2])
        self.assertEqual((False, False), after_boundary[:2])
        self.assertEqual((False, False), aged_while_locks_were_acquired[:2])
        self.assertEqual(
            "final_delivery_policy_changed", after_boundary[2].last_error_code
        )
        self.assertEqual(
            "final_delivery_policy_changed",
            aged_while_locks_were_acquired[2].last_error_code,
        )

    async def test_final_authorization_blocks_mode_change_after_claim(self):
        now = datetime(2026, 9, 21, 6, tzinfo=UTC)
        user = User(id=uuid4(), telegram_user_id=42, mode="LESSONS_ONLY")
        scope = AlertScope(
            id=uuid4(),
            user_id=user.id,
            selected_region_id="31",
            applicable_region_ids=["31"],
        )
        snapshot = AlertSnapshot(scope_id=scope.id, state=AlertState.CLEAR_CONFIRMED.value)
        message = OutboxMessage(
            id=uuid4(),
            aggregate_type="alert_scope",
            aggregate_id=str(scope.id),
            event_key="claimed-clear",
            event_type="ALL_CLEAR_CONFIRMED",
            payload={"telegram_chat_id": 42},
            claimed_at=now,
        )
        session = FinalDeliverySession(
            {
                OutboxMessage: message,
                AlertScope: scope,
                User: user,
                AlertSnapshot: snapshot,
            }
        )
        repo = PostgresSenderRepository(  # type: ignore[arg-type]
            lambda: AsyncContext(session), stale_after_seconds=120, clock=lambda: now
        )
        sent = False

        async def deliver():
            nonlocal sent
            sent = True

        allowed = await repo.deliver_if_authorized(str(message.id), now, deliver)

        self.assertFalse(allowed)
        self.assertFalse(sent)
        self.assertEqual(message.last_error_code, "final_delivery_policy_changed")

    async def test_final_authorization_blocks_stale_combined_lesson_messages(self):
        now = datetime(2026, 9, 21, 6, tzinfo=UTC)
        user = User(id=uuid4(), telegram_user_id=42, mode="LESSONS_AND_ALERTS")
        scope = AlertScope(
            id=uuid4(),
            user_id=user.id,
            selected_region_id="31",
            applicable_region_ids=["31"],
        )
        snapshot = AlertSnapshot(scope_id=scope.id, state=AlertState.STALE.value)
        profile = ProfileRow(
            id=uuid4(),
            user_id=user.id,
            public_id="school",
            name="School",
            is_active=True,
            version=1,
        )
        for event_type in ("LESSON_REMINDER", "RETURN_TO_ACTIVE_LESSON"):
            message = OutboxMessage(
                id=uuid4(),
                aggregate_type="schedule",
                aggregate_id=str(user.id),
                event_key=f"claimed-{event_type}",
                event_type=event_type,
                payload={"telegram_chat_id": 42, "profile_revision": 1},
                claimed_at=now,
            )
            session = FinalDeliverySession(
                {
                    OutboxMessage: message,
                    User: user,
                    AlertScope: scope,
                    AlertSnapshot: snapshot,
                    ProfileRow: profile,
                }
            )
            repo = PostgresSenderRepository(
                lambda session=session: AsyncContext(session),  # type: ignore[arg-type]
                stale_after_seconds=120,
                clock=lambda: now,
            )
            sent = False

            async def deliver():
                nonlocal sent
                sent = True

            allowed = await repo.deliver_if_authorized(str(message.id), now, deliver)

            self.assertFalse(allowed)
            self.assertFalse(sent)
            self.assertEqual(message.last_error_code, "final_delivery_policy_changed")

    async def test_final_authorization_blocks_profile_revision_change_after_claim(self):
        now = datetime(2026, 9, 21, 6, tzinfo=UTC)
        user = User(id=uuid4(), telegram_user_id=42, mode="LESSONS_ONLY")
        profile = ProfileRow(
            id=uuid4(),
            user_id=user.id,
            public_id="school",
            name="School",
            is_active=True,
            version=2,
        )
        message = OutboxMessage(
            id=uuid4(),
            aggregate_type="schedule",
            aggregate_id=str(user.id),
            event_key="claimed-reminder",
            event_type="LESSON_REMINDER",
            payload={"telegram_chat_id": 42, "profile_revision": 1},
            claimed_at=now,
        )
        session = FinalDeliverySession(
            {OutboxMessage: message, User: user, ProfileRow: profile}
        )
        repo = PostgresSenderRepository(  # type: ignore[arg-type]
            lambda: AsyncContext(session), stale_after_seconds=120, clock=lambda: now
        )
        sent = False

        async def deliver():
            nonlocal sent
            sent = True

        allowed = await repo.deliver_if_authorized(str(message.id), now, deliver)

        self.assertFalse(allowed)
        self.assertFalse(sent)
        self.assertEqual(message.last_error_code, "final_delivery_policy_changed")

    async def test_return_expiry_is_rechecked_after_policy_locks_at_exact_boundary(self):
        valid_until = datetime(2026, 9, 21, 6, 5, tzinfo=UTC)

        async def attempt(send_at: datetime):
            authorization_at = valid_until - timedelta(seconds=1)
            user = User(id=uuid4(), telegram_user_id=42, mode="LESSONS_AND_ALERTS")
            scope = AlertScope(
                id=uuid4(),
                user_id=user.id,
                selected_region_id="31",
                applicable_region_ids=["31"],
            )
            snapshot = AlertSnapshot(
                scope_id=scope.id, state=AlertState.CLEAR_CONFIRMED.value
            )
            profile = ProfileRow(
                id=uuid4(),
                user_id=user.id,
                public_id="school",
                name="School",
                is_active=True,
                version=1,
            )
            poll = PollRow(singleton_id=1, fetch_revision=1, circuit_open=False)
            poll.last_success_at = authorization_at
            message = OutboxMessage(
                id=uuid4(),
                aggregate_type="schedule",
                aggregate_id=str(user.id),
                event_key=f"return:{send_at.isoformat()}",
                event_type="RETURN_TO_ACTIVE_LESSON",
                payload={
                    "telegram_chat_id": 42,
                    "profile_revision": 1,
                    "valid_until": valid_until.isoformat(),
                },
                claimed_at=authorization_at,
            )
            session = FinalDeliverySession(
                {
                    OutboxMessage: message,
                    User: user,
                    PollRow: poll,
                    AlertScope: scope,
                    AlertSnapshot: snapshot,
                    ProfileRow: profile,
                }
            )
            clock_values = iter((authorization_at, send_at))
            repo = PostgresSenderRepository(  # type: ignore[arg-type]
                lambda: AsyncContext(session),
                stale_after_seconds=120,
                clock=lambda: next(clock_values),
            )
            sent = False

            async def deliver():
                nonlocal sent
                sent = True

            allowed = await repo.deliver_if_authorized(
                str(message.id), authorization_at, deliver
            )
            return allowed, sent, message

        before = await attempt(valid_until - timedelta(microseconds=1))
        at_boundary = await attempt(valid_until)

        self.assertEqual((True, True), before[:2])
        self.assertEqual((False, False), at_boundary[:2])
        self.assertEqual("final_delivery_expired", at_boundary[2].last_error_code)
        self.assertEqual(valid_until, at_boundary[2].dead_at)

    async def test_transport_failure_after_authorization_keeps_retry_semantics(self):
        now = datetime(2026, 9, 21, 6, tzinfo=UTC)
        user = User(id=uuid4(), telegram_user_id=42, mode="LESSONS_ONLY")
        profile = ProfileRow(
            id=uuid4(),
            user_id=user.id,
            public_id="school",
            name="School",
            is_active=True,
            version=1,
        )
        message = OutboxMessage(
            id=uuid4(),
            aggregate_type="schedule",
            aggregate_id=str(user.id),
            event_key="retry-reminder",
            event_type="LESSON_REMINDER",
            payload={"telegram_chat_id": 42, "profile_revision": 1},
            claimed_at=now,
            attempts=0,
        )
        session = FinalDeliverySession(
            {OutboxMessage: message, User: user, ProfileRow: profile}
        )
        repo = PostgresSenderRepository(  # type: ignore[arg-type]
            lambda: AsyncContext(session), stale_after_seconds=120, clock=lambda: now
        )

        async def fail_delivery():
            raise RuntimeError("telegram unavailable")

        with self.assertRaisesRegex(RuntimeError, "telegram unavailable"):
            await repo.deliver_if_authorized(str(message.id), now, fail_delivery)

        retry_at = datetime(2026, 9, 21, 6, 0, 2, tzinfo=UTC)
        await repo.mark_retry(
            str(message.id), available_at=retry_at, error_code="transport_error"
        )
        self.assertEqual(message.attempts, 1)
        self.assertEqual(message.available_at, retry_at)
        self.assertIsNone(message.claimed_at)
        self.assertEqual(message.last_error_code, "transport_error")

    async def test_removed_lesson_reference_is_not_retargeted_to_replacement(self):
        old_id = uuid4()
        rule_id = uuid4()
        existing = RuleLesson(
            id=old_id,
            rule_id=rule_id,
            position=0,
            start_time=wall_time(9),
            end_time=wall_time(10),
            title="Removed",
            enabled=True,
        )
        session = MutationSession()
        repo = AsyncPostgresAccountRepository(None)  # type: ignore[arg-type]

        await repo._sync_lessons(
            session,  # type: ignore[arg-type]
            [existing],
            (Lesson(wall_time(11), wall_time(12), "Replacement"),),
            parent_id=rule_id,
            kind="rule",
        )

        self.assertEqual(len(session.added), 1)
        self.assertNotEqual(session.added[0].id, old_id)
        self.assertTrue(session.statements, "old lesson must be explicitly deleted")

    async def test_worker_heartbeat_is_postgres_upsert(self):
        session = HeartbeatSession()
        repo = PostgresWorkerHeartbeatRepository(lambda: AsyncContext(session))  # type: ignore[arg-type]
        await repo.upsert(
            worker_name="sender-worker",
            instance_id="instance-1",
            status="running",
            heartbeat_at=datetime(2026, 9, 21, tzinfo=UTC),
            details={"queue": 2},
        )
        sql = str(session.statements[0].compile(dialect=postgresql.dialect()))
        self.assertIn("ON CONFLICT (worker_name) DO UPDATE", sql)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            await repo.upsert(
                worker_name="sender-worker",
                instance_id="instance-1",
                status="running",
                heartbeat_at=datetime(2026, 9, 21),
            )

    async def test_scalar_account_save_preserves_profile_identity_and_references(self):
        internal_id = uuid4()
        existing = ProfileRow(
            id=internal_id,
            user_id=uuid4(),
            public_id="school",
            name="Old",
            timezone="Europe/Kyiv",
            is_active=True,
            version=4,
        )
        session = ProfileDiffSession([existing])
        repo = AsyncPostgresAccountRepository(None)  # type: ignore[arg-type]
        repo._load_profile = AsyncMock(return_value=ScheduleProfile("school", "Old", active=True))  # type: ignore[method-assign]

        await repo._sync_profiles(
            session, existing.user_id, {"school": ScheduleProfile("school", "Renamed", active=True)}
        )

        self.assertEqual(existing.id, internal_id)
        self.assertEqual(existing.name, "Renamed")
        self.assertEqual(existing.version, 5)
        sql = [str(item.compile(dialect=postgresql.dialect())) for item in session.statements]
        self.assertFalse(any(item.startswith("DELETE FROM schedule_profiles") for item in sql))
        self.assertFalse(any("pending_import_sessions" in item for item in sql))
        self.assertFalse(any("share_sessions" in item for item in sql))

    def test_lessons_only_mode_never_allows_alert_or_all_clear_outbox(self):
        self.assertFalse(PostgresAlertRepository.mode_allows_alert_notifications("LESSONS_ONLY"))
        self.assertFalse(PostgresAlertRepository.mode_allows_alert_notifications(None))
        self.assertTrue(
            PostgresAlertRepository.mode_allows_alert_notifications("LESSONS_AND_ALERTS")
        )
        self.assertTrue(PostgresAlertRepository.mode_allows_alert_notifications("ALERTS_ONLY"))

    async def test_poll_failure_backoff_and_circuit_survive_repository_restart(self):
        row = PollRow(
            singleton_id=1,
            fetch_revision=8,
            last_modified="stamp",
            circuit_open=False,
            consecutive_failures=0,
        )
        row.retry_not_before = None
        session = PollSession(row)

        def factory():
            return AsyncContext(session)

        first = PostgresAlertRepository(factory)  # type: ignore[arg-type]
        retry = time.monotonic() + 30
        await first.set_poll_state(
            AlertPollState(
                fetch_revision=9,
                last_modified="new",
                circuit_open=True,
                retry_not_before_monotonic=retry,
                consecutive_failures=7,
            )
        )

        restored = await PostgresAlertRepository(factory).poll_state()  # type: ignore[arg-type]
        self.assertEqual(restored.fetch_revision, 9)
        self.assertEqual(restored.consecutive_failures, 7)
        self.assertTrue(restored.circuit_open)
        self.assertGreater(restored.retry_not_before_monotonic, time.monotonic())

    async def test_import_lock_can_be_bound_to_owner_in_same_query(self):
        owner_id = uuid4()
        row = PendingImportSession(
            token_hash=digest_secret("token"),
            user_id=owner_id,
            fingerprint_hash=digest_secret("fingerprint"),
        )
        session = SimpleNamespace(execute=AsyncMock(return_value=Result(scalar=row)))
        repo = PostgresImportSessionRepository(session)
        await repo._locked("token", expected_user_id=owner_id)
        sql = str(session.execute.await_args.args[0].compile(dialect=postgresql.dialect()))
        self.assertIn("pending_import_sessions.user_id", sql)
        self.assertIn("FOR UPDATE", sql)

    def test_offline_poll_state_ddl_contains_failure_counter(self):
        ddl = str(CreateTable(PollRow.__table__).compile(dialect=postgresql.dialect()))
        self.assertIn("consecutive_failures", ddl)
        self.assertIn("failures_nonnegative", ddl)

    async def test_region_confirmation_is_idempotent_and_uses_one_transaction(self):
        user = SimpleNamespace(
            id=uuid4(), telegram_user_id=42, confirmed_region_id="old", region_name="Old"
        )
        scope_row = SimpleNamespace(
            id=uuid4(),
            user_id=user.id,
            selected_region_id="31",
            applicable_region_ids=["3", "31"],
        )

        class ScopeSession(PollSession):
            def __init__(self):
                super().__init__(None)
                self.calls = 0
                self.added = []
                self.get = AsyncMock()

            async def execute(self, _statement):
                self.calls += 1
                return Result(scalar=user if self.calls == 1 else scope_row)

            def add(self, value):
                self.added.append(value)

        session = ScopeSession()
        repo = PostgresAlertRepository(lambda: AsyncContext(session))  # type: ignore[arg-type]
        await repo.confirm_region_scope_for_telegram_user(
            42,
            confirmed_region_id="31",
            region_name="Kyiv",
            scope=RegionScope("31", frozenset({"31", "3"})),
        )
        self.assertEqual(session.begin_count, 1)
        self.assertEqual(user.confirmed_region_id, "31")
        self.assertEqual(user.region_name, "Kyiv")
        session.get.assert_not_awaited()
        self.assertEqual(session.added, [])


if __name__ == "__main__":
    unittest.main()
