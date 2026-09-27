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

from time_tl.application.sender_worker import DeliveryRecipient
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
    WorkerHeartbeat,
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
RECIPIENT = DeliveryRecipient(42, "31", "м. Київ")


def bind_scope(scope: AlertScope) -> dict[str, object]:
    scope.territory_provider = "alerts.in.ua"
    scope.territory_catalog_version = "2026-09-24"
    return {
        "provider": "alerts.in.ua",
        "catalog_version": "2026-09-24",
        "selected_region_id": scope.selected_region_id,
        "applicable_region_ids": sorted(set(scope.applicable_region_ids)),
    }


def alert_payload(scope: AlertScope, **values: object) -> dict[str, object]:
    return {**values, "scope_binding": bind_scope(scope)}


class Result:
    def __init__(self, *, scalar=None, scalars=()):
        self._scalar = scalar
        self._scalars = tuple(scalars)

    def scalar_one_or_none(self):
        return self._scalar

    def scalar_one(self):
        if self._scalar is None:
            raise LookupError("expected one row")
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


class ClaimSession:
    def __init__(self, objects, *, results):
        self.objects = objects
        self.results = list(results)
        self.cooldown = WorkerHeartbeat(
            worker_name="sender-global-cooldown",
            instance_id="shared",
            status="READY",
            heartbeat_at=AUTHORIZATION_AT,
            details={},
        )

    def begin(self):
        return AsyncContext(None)

    async def get(self, model, _key):
        return self.objects.get(model)

    async def execute(self, statement):
        table = getattr(statement, "table", None)
        if getattr(table, "name", None) == "worker_heartbeats":
            return Result()
        descriptions = getattr(statement, "column_descriptions", ())
        entity = descriptions[0].get("entity") if descriptions else None
        if entity is WorkerHeartbeat:
            return Result(scalar=self.cooldown)
        return self.results.pop(0)


class RetentionSession:
    def __init__(self, *results):
        self.results = list(results)
        self.statements = []
        self.deleted = []

    def begin(self):
        return AsyncContext(None)

    async def execute(self, statement):
        self.statements.append(statement)
        return Result(scalars=self.results.pop(0) if self.results else ())

    async def delete(self, row):
        self.deleted.append(row)


class SafetyPersistenceRegressionTests(unittest.IsolatedAsyncioTestCase):
    def test_provider_freshness_boundary_is_inclusive_then_fails_closed(self):
        last_success = AUTHORIZATION_AT - STALE_AFTER
        self.assertTrue(_provider_is_fresh(last_success, AUTHORIZATION_AT, STALE_AFTER))
        self.assertFalse(
            _provider_is_fresh(
                last_success,
                AUTHORIZATION_AT + timedelta(microseconds=1),
                STALE_AFTER,
            )
        )
        self.assertFalse(_provider_is_fresh(None, AUTHORIZATION_AT, STALE_AFTER))

    async def test_terminal_outbox_retention_is_bounded_and_skip_locked(self):
        delivered = OutboxMessage(
            id=uuid4(),
            aggregate_type="schedule",
            aggregate_id=str(uuid4()),
            event_key="old-delivered",
            event_type="LESSON_REMINDER",
            payload={},
            attempts=3,
            delivered_at=AUTHORIZATION_AT - timedelta(days=91),
        )
        dead = OutboxMessage(
            id=uuid4(),
            aggregate_type="schedule",
            aggregate_id=str(uuid4()),
            event_key="old-dead",
            event_type="LESSON_REMINDER",
            payload={},
            attempts=6,
            dead_at=AUTHORIZATION_AT - timedelta(days=91),
        )
        session = RetentionSession((delivered,), (dead,))
        repo = PostgresSenderRepository(  # type: ignore[arg-type]
            lambda: AsyncContext(session), stale_after_seconds=120
        )

        deleted = await repo.prune_terminal_outbox(AUTHORIZATION_AT, limit=2)

        self.assertEqual(2, deleted)
        self.assertEqual([delivered, dead], session.deleted)
        self.assertEqual((3, 6), (delivered.attempts, dead.attempts))
        self.assertEqual(2, len(session.statements))
        delivered_sql = str(session.statements[0].compile(dialect=postgresql.dialect()))
        dead_sql = str(session.statements[1].compile(dialect=postgresql.dialect()))
        for sql in (delivered_sql, dead_sql):
            self.assertIn("outbox_messages.claimed_at IS NULL", sql)
            self.assertIn("FOR UPDATE SKIP LOCKED", sql)
        self.assertIn("outbox_messages.delivered_at <=", delivered_sql)
        self.assertIn("outbox_messages.dead_at IS NULL", delivered_sql)
        self.assertIn("outbox_messages.dead_at <=", dead_sql)
        self.assertIn("outbox_messages.delivered_at IS NULL", dead_sql)

        with self.assertRaisesRegex(ValueError, "between 1 and 100"):
            await repo.prune_terminal_outbox(AUTHORIZATION_AT, limit=101)

    async def test_shared_sender_cooldown_blocks_claiming_before_retry_after(self):
        session = ClaimSession({}, results=())
        session.cooldown.status = "COOLDOWN"
        session.cooldown.details = {
            "cooldown_until": (AUTHORIZATION_AT + timedelta(seconds=30)).isoformat()
        }
        repo = PostgresSenderRepository(  # type: ignore[arg-type]
            lambda: AsyncContext(session), stale_after_seconds=120
        )

        self.assertEqual((), await repo.claim_batch_skip_locked(AUTHORIZATION_AT, limit=10))
        self.assertEqual([], session.results)

    async def test_telegram_429_extends_shared_sender_cooldown(self):
        session = SimpleNamespace(begin=lambda: AsyncContext(None))
        message = OutboxMessage(
            id=uuid4(),
            aggregate_type="schedule",
            aggregate_id=str(uuid4()),
            event_key="retry-after",
            event_type="LESSON_REMINDER",
            payload={},
            attempts=0,
        )
        cooldown = WorkerHeartbeat(
            worker_name="sender-global-cooldown",
            instance_id="shared",
            status="COOLDOWN",
            heartbeat_at=AUTHORIZATION_AT,
            details={"cooldown_until": (AUTHORIZATION_AT + timedelta(seconds=60)).isoformat()},
        )
        repo = PostgresSenderRepository(  # type: ignore[arg-type]
            lambda: AsyncContext(session),
            stale_after_seconds=120,
            clock=lambda: AUTHORIZATION_AT,
        )
        retry_at = AUTHORIZATION_AT + timedelta(seconds=30)
        with (
            patch.object(repo, "_locked", AsyncMock(return_value=message)),
            patch.object(
                repo,
                "_lock_sender_cooldown",
                AsyncMock(return_value=cooldown),
            ),
        ):
            await repo.mark_retry(
                str(message.id),
                available_at=retry_at,
                error_code="telegram_429",
            )

        self.assertEqual("COOLDOWN", cooldown.status)
        self.assertEqual(
            (AUTHORIZATION_AT + timedelta(seconds=60)).isoformat(),
            cooldown.details["cooldown_until"],
        )
        self.assertEqual(1, message.attempts)
        self.assertEqual(retry_at, message.available_at)

    async def test_claim_enriches_recipient_ephemerally_without_persisting_region(self):
        user = User(
            id=uuid4(),
            telegram_user_id=42,
            mode="ALERTS_ONLY",
            confirmed_region_id="31",
            region_name="м. Київ",
        )
        scope = AlertScope(
            id=uuid4(),
            user_id=user.id,
            selected_region_id="31",
            applicable_region_ids=["31"],
        )
        snapshot = AlertSnapshot(scope_id=scope.id, state=AlertState.ACTIVE.value)
        poll = PollRow(singleton_id=1, fetch_revision=1, circuit_open=False)
        poll.last_success_at = AUTHORIZATION_AT
        row = OutboxMessage(
            id=uuid4(),
            aggregate_type="alert_scope",
            aggregate_id=str(scope.id),
            event_key="claim-alert",
            event_type="ALERT_STARTED",
            payload=alert_payload(scope, telegram_chat_id=999),
        )
        session = ClaimSession(
            {
                User: user,
                AlertScope: scope,
                AlertSnapshot: snapshot,
                PollRow: poll,
            },
            results=(Result(scalars=(row,)),),
        )
        repo = PostgresSenderRepository(  # type: ignore[arg-type]
            lambda: AsyncContext(session), stale_after_seconds=120
        )

        claimed = await repo.claim_batch_skip_locked(AUTHORIZATION_AT, limit=10)

        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].recipient, RECIPIENT)
        self.assertEqual(claimed[0].payload["telegram_chat_id"], 42)
        self.assertEqual(claimed[0].payload["confirmed_region_id"], "31")
        self.assertEqual(claimed[0].payload["confirmed_region_name"], "м. Київ")
        self.assertEqual(row.payload["telegram_chat_id"], 999)
        self.assertEqual(row.payload["scope_binding"], bind_scope(scope))
        self.assertEqual(row.claimed_at, AUTHORIZATION_AT)

    async def test_claim_quarantines_region_message_when_confirmation_is_incomplete(self):
        user = User(
            id=uuid4(),
            telegram_user_id=42,
            mode="ALERTS_ONLY",
            confirmed_region_id="31",
            region_name=None,
        )
        scope = AlertScope(
            id=uuid4(),
            user_id=user.id,
            selected_region_id="31",
            applicable_region_ids=["31"],
        )
        snapshot = AlertSnapshot(scope_id=scope.id, state=AlertState.ACTIVE.value)
        poll = PollRow(singleton_id=1, fetch_revision=1, circuit_open=False)
        poll.last_success_at = AUTHORIZATION_AT
        row = OutboxMessage(
            id=uuid4(),
            aggregate_type="alert_scope",
            aggregate_id=str(scope.id),
            event_key="claim-missing-region",
            event_type="ALERT_STARTED",
            payload=alert_payload(scope, telegram_chat_id=42),
        )
        session = ClaimSession(
            {
                User: user,
                AlertScope: scope,
                AlertSnapshot: snapshot,
                PollRow: poll,
            },
            results=(Result(scalars=(row,)),),
        )
        repo = PostgresSenderRepository(  # type: ignore[arg-type]
            lambda: AsyncContext(session), stale_after_seconds=120
        )

        claimed = await repo.claim_batch_skip_locked(AUTHORIZATION_AT, limit=10)

        self.assertEqual(claimed, ())
        self.assertEqual(row.last_error_code, "delivery_recipient_unavailable")
        self.assertEqual(row.dead_at, AUTHORIZATION_AT)
        self.assertIsNone(row.claimed_at)

    async def test_claim_allows_lessons_only_user_without_region(self):
        user = User(id=uuid4(), telegram_user_id=42, mode="LESSONS_ONLY")
        profile = ProfileRow(
            id=uuid4(),
            user_id=user.id,
            public_id="school",
            name="School",
            is_active=True,
            version=1,
        )
        row = OutboxMessage(
            id=uuid4(),
            aggregate_type="schedule",
            aggregate_id=str(user.id),
            event_key="claim-reminder",
            event_type="LESSON_REMINDER",
            payload={"telegram_chat_id": 999, "profile_revision": 1},
        )
        session = ClaimSession(
            {User: user},
            results=(Result(scalars=(row,)), Result(scalar=profile)),
        )
        repo = PostgresSenderRepository(  # type: ignore[arg-type]
            lambda: AsyncContext(session), stale_after_seconds=120
        )

        claimed = await repo.claim_batch_skip_locked(AUTHORIZATION_AT, limit=10)

        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].recipient, DeliveryRecipient(42))
        self.assertNotIn("confirmed_region_id", claimed[0].payload)
        self.assertNotIn("confirmed_region_name", claimed[0].payload)
        self.assertEqual(row.payload["telegram_chat_id"], 999)

    async def test_claim_defers_alert_events_until_stale_snapshot_recovers(self):
        for event_type, recovered_state in (
            ("ALERT_STARTED", AlertState.ACTIVE.value),
            ("ALL_CLEAR_CONFIRMED", AlertState.CLEAR_CONFIRMED.value),
        ):
            with self.subTest(event_type=event_type):
                user = User(
                    id=uuid4(),
                    telegram_user_id=42,
                    mode="ALERTS_ONLY",
                    confirmed_region_id="31",
                    region_name="м. Київ",
                )
                scope = AlertScope(
                    id=uuid4(),
                    user_id=user.id,
                    selected_region_id="31",
                    applicable_region_ids=["31"],
                )
                snapshot = AlertSnapshot(scope_id=scope.id, state=AlertState.STALE.value)
                poll = PollRow(singleton_id=1, fetch_revision=1, circuit_open=False)
                poll.last_success_at = AUTHORIZATION_AT
                row = OutboxMessage(
                    id=uuid4(),
                    aggregate_type="alert_scope",
                    aggregate_id=str(scope.id),
                    event_key=f"transient:{event_type}",
                    event_type=event_type,
                    payload=alert_payload(scope, telegram_chat_id=42),
                    attempts=0,
                )
                session = ClaimSession(
                    {
                        User: user,
                        AlertScope: scope,
                        AlertSnapshot: snapshot,
                        PollRow: poll,
                    },
                    results=(Result(scalars=(row,)), Result(scalars=(row,))),
                )
                repo = PostgresSenderRepository(  # type: ignore[arg-type]
                    lambda session=session: AsyncContext(session),
                    stale_after_seconds=120,
                )

                first = await repo.claim_batch_skip_locked(AUTHORIZATION_AT, limit=10)

                self.assertEqual(first, ())
                self.assertIsNone(row.dead_at)
                self.assertIsNone(row.claimed_at)
                self.assertEqual(row.available_at, AUTHORIZATION_AT + timedelta(seconds=15))
                self.assertEqual(row.attempts, 0)

                recovery_at = AUTHORIZATION_AT + timedelta(seconds=15)
                snapshot.state = recovered_state
                poll.last_success_at = recovery_at
                recovered = await repo.claim_batch_skip_locked(recovery_at, limit=10)

                self.assertEqual(len(recovered), 1)
                self.assertEqual(recovered[0].message_id, str(row.id))
                self.assertEqual(row.claimed_at, recovery_at)
                self.assertIsNone(row.dead_at)
                self.assertEqual(row.attempts, 0)

    async def test_claim_permanently_rejects_old_or_missing_scope_binding(self):
        for binding in (
            None,
            {
                "provider": "alerts.in.ua",
                "catalog_version": "old-catalog",
                "selected_region_id": "31",
                "applicable_region_ids": ["31"],
            },
        ):
            with self.subTest(binding=binding):
                user = User(
                    id=uuid4(),
                    telegram_user_id=42,
                    mode="ALERTS_ONLY",
                    confirmed_region_id="31",
                    region_name="м. Київ",
                )
                scope = AlertScope(
                    id=uuid4(),
                    user_id=user.id,
                    selected_region_id="31",
                    applicable_region_ids=["31"],
                )
                bind_scope(scope)
                snapshot = AlertSnapshot(scope_id=scope.id, state=AlertState.STALE.value)
                poll = PollRow(singleton_id=1, fetch_revision=1, circuit_open=False)
                poll.last_success_at = AUTHORIZATION_AT
                payload: dict[str, object] = {"telegram_chat_id": 42}
                if binding is not None:
                    payload["scope_binding"] = binding
                row = OutboxMessage(
                    id=uuid4(),
                    aggregate_type="alert_scope",
                    aggregate_id=str(scope.id),
                    event_key=f"old-binding:{binding is None}",
                    event_type="ALL_CLEAR_CONFIRMED",
                    payload=payload,
                )
                session = ClaimSession(
                    {
                        User: user,
                        AlertScope: scope,
                        AlertSnapshot: snapshot,
                        PollRow: poll,
                    },
                    results=(Result(scalars=(row,)),),
                )
                repo = PostgresSenderRepository(  # type: ignore[arg-type]
                    lambda session=session: AsyncContext(session),
                    stale_after_seconds=120,
                )

                self.assertEqual((), await repo.claim_batch_skip_locked(AUTHORIZATION_AT, limit=10))
                self.assertEqual(row.dead_at, AUTHORIZATION_AT)
                self.assertEqual(row.last_error_code, "delivery_policy_changed")

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

    async def test_alert_outbox_payload_is_bound_to_exact_scope_identity(self):
        poll_row = PollRow(singleton_id=1, fetch_revision=0, circuit_open=False)
        user = User(id=uuid4(), telegram_user_id=42, mode="ALERTS_ONLY")
        scope = AlertScope(
            id=uuid4(),
            user_id=user.id,
            selected_region_id="31",
            applicable_region_ids=["31", "14", "31"],
        )
        expected_binding = bind_scope(scope)

        class EnqueueSession:
            def __init__(self):
                self.calls = 0
                self.insert_statement = None

            def begin(self):
                return AsyncContext(None)

            async def execute(self, statement):
                self.calls += 1
                if self.calls == 1:
                    return Result(scalar=poll_row)
                if self.calls == 2:
                    return Result(scalars=(scope,))
                self.insert_statement = statement
                return Result(scalar="inserted")

            async def get(self, model, _key):
                return user if model is User else None

        session = EnqueueSession()
        repo = PostgresAlertRepository(lambda: AsyncContext(session))  # type: ignore[arg-type]
        update = OfficialAlertSnapshot(
            update_id="bound-active",
            fetch_revision=1,
            kind=ProviderSnapshotKind.FULL_200,
            covered_region_ids=frozenset({"31", "14"}),
            active_episodes=(DomainEpisode("31", "air_raid", "episode-1"),),
            fetched_at=AUTHORIZATION_AT,
            is_complete=True,
            is_fresh=True,
        )

        with (
            patch.object(repo, "_load_snapshot", AsyncMock(return_value=None)),
            patch.object(repo, "_store_snapshot", AsyncMock()),
        ):
            created = await repo.apply_snapshot_and_poll_state_atomically(
                update,
                AlertPollState(fetch_revision=1, last_modified="marker"),
            )

        self.assertEqual(len(created), 1)
        self.assertIsNotNone(session.insert_statement)
        params = session.insert_statement.compile(  # type: ignore[union-attr]
            dialect=postgresql.dialect()
        ).params
        payload = next(
            value
            for value in params.values()
            if isinstance(value, dict) and "scope_binding" in value
        )
        self.assertEqual(payload["scope_binding"], expected_binding)

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
        self.assertEqual(poll_row.fetch_revision, 8)

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
        user = User(
            id=uuid4(),
            telegram_user_id=42,
            mode="LESSONS_AND_ALERTS",
            confirmed_region_id="31",
            region_name="м. Київ",
        )
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
            payload=alert_payload(scope, telegram_chat_id=42),
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
        user = User(
            id=uuid4(),
            telegram_user_id=42,
            mode="LESSONS_AND_ALERTS",
            confirmed_region_id="31",
            region_name="м. Київ",
        )
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
            session = DeliverySession({User: user, AlertSnapshot: snapshot}, query_results=(scope,))
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
        user = User(
            id=uuid4(),
            telegram_user_id=42,
            mode="LESSONS_AND_ALERTS",
            confirmed_region_id="31",
            region_name="м. Київ",
        )
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
            payload=alert_payload(scope, telegram_chat_id=42),
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
        allowed = await repo.deliver_if_authorized(
            str(message.id), now, deliver, expected_recipient=RECIPIENT
        )

        self.assertFalse(allowed)
        self.assertFalse(sent)
        self.assertIsNone(message.dead_at)
        self.assertIsNone(message.claimed_at)
        self.assertEqual(message.available_at, now + timedelta(seconds=15))
        self.assertEqual(message.last_error_code, "delivery_policy_temporarily_unavailable")

        snapshot.state = AlertState.ACTIVE.value
        message.claimed_at = now + timedelta(seconds=15)
        recovered = await repo.deliver_if_authorized(
            str(message.id),
            now + timedelta(seconds=15),
            deliver,
            expected_recipient=RECIPIENT,
        )
        self.assertTrue(recovered)
        self.assertTrue(sent)
        self.assertIsNotNone(message.delivered_at)

    async def test_final_authorization_uses_delivery_time_freshness_boundary(self):
        async def attempt(
            last_success_at: datetime,
            *,
            clock_values: tuple[datetime, datetime] = (
                AUTHORIZATION_AT,
                AUTHORIZATION_AT,
            ),
        ):
            user = User(
                id=uuid4(),
                telegram_user_id=42,
                mode="ALERTS_ONLY",
                confirmed_region_id="31",
                region_name="м. Київ",
            )
            scope = AlertScope(
                id=uuid4(),
                user_id=user.id,
                selected_region_id="31",
                applicable_region_ids=["31"],
            )
            snapshot = AlertSnapshot(scope_id=scope.id, state=AlertState.CLEAR_CONFIRMED.value)
            poll = PollRow(singleton_id=1, fetch_revision=5, circuit_open=False)
            poll.last_success_at = last_success_at
            message = OutboxMessage(
                id=uuid4(),
                aggregate_type="alert_scope",
                aggregate_id=str(scope.id),
                event_key=f"clear:{last_success_at.isoformat()}",
                event_type="ALL_CLEAR_CONFIRMED",
                payload=alert_payload(
                    scope,
                    telegram_chat_id=42,
                    confirmed_region_id="31",
                    confirmed_region_name="м. Київ",
                ),
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
                str(message.id),
                AUTHORIZATION_AT,
                deliver,
                expected_recipient=RECIPIENT,
            )
            return allowed, sent, message

        at_boundary = await attempt(AUTHORIZATION_AT - STALE_AFTER)
        after_boundary = await attempt(AUTHORIZATION_AT - STALE_AFTER - timedelta(microseconds=1))
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
            "delivery_policy_temporarily_unavailable",
            after_boundary[2].last_error_code,
        )
        self.assertEqual(
            "delivery_policy_temporarily_unavailable",
            aged_while_locks_were_acquired[2].last_error_code,
        )

    async def test_final_authorization_blocks_chat_or_region_change_after_claim(self):
        for chat_id, region_id, region_name in (
            (99, "31", "м. Київ"),
            (42, "9", "Дніпропетровська область"),
        ):
            user = User(
                id=uuid4(),
                telegram_user_id=chat_id,
                mode="ALERTS_ONLY",
                confirmed_region_id=region_id,
                region_name=region_name,
            )
            scope = AlertScope(
                id=uuid4(),
                user_id=user.id,
                selected_region_id=region_id,
                applicable_region_ids=[region_id],
            )
            snapshot = AlertSnapshot(scope_id=scope.id, state=AlertState.ACTIVE.value)
            poll = PollRow(singleton_id=1, fetch_revision=1, circuit_open=False)
            poll.last_success_at = AUTHORIZATION_AT
            message = OutboxMessage(
                id=uuid4(),
                aggregate_type="alert_scope",
                aggregate_id=str(scope.id),
                event_key=f"changed-recipient:{chat_id}:{region_id}",
                event_type="ALERT_STARTED",
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
            repo = PostgresSenderRepository(  # type: ignore[arg-type]
                lambda session=session: AsyncContext(session),
                stale_after_seconds=120,
                clock=lambda: AUTHORIZATION_AT,
            )
            sent = False

            async def deliver():
                nonlocal sent
                sent = True

            allowed = await repo.deliver_if_authorized(
                str(message.id),
                AUTHORIZATION_AT,
                deliver,
                expected_recipient=RECIPIENT,
            )

            self.assertFalse(allowed)
            self.assertFalse(sent)
            self.assertEqual(message.last_error_code, "final_delivery_policy_changed")

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

        allowed = await repo.deliver_if_authorized(
            str(message.id), now, deliver, expected_recipient=RECIPIENT
        )

        self.assertFalse(allowed)
        self.assertFalse(sent)
        self.assertEqual(message.last_error_code, "final_delivery_policy_changed")

    async def test_final_authorization_blocks_stale_combined_lesson_messages(self):
        now = datetime(2026, 9, 21, 6, tzinfo=UTC)
        user = User(
            id=uuid4(),
            telegram_user_id=42,
            mode="LESSONS_AND_ALERTS",
            confirmed_region_id="31",
            region_name="м. Київ",
        )
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
        poll = PollRow(
            singleton_id=1,
            fetch_revision=1,
            circuit_open=False,
            last_success_at=now,
        )
        binding = bind_scope(scope)
        for event_type in ("LESSON_REMINDER", "RETURN_TO_ACTIVE_LESSON"):
            payload: dict[str, object] = {
                "telegram_chat_id": 42,
                "profile_revision": 1,
            }
            if event_type == "RETURN_TO_ACTIVE_LESSON":
                payload["valid_until"] = (now + timedelta(minutes=1)).isoformat()
                payload["scope_binding"] = binding
            message = OutboxMessage(
                id=uuid4(),
                aggregate_type="schedule",
                aggregate_id=str(user.id),
                event_key=f"claimed-{event_type}",
                event_type=event_type,
                payload=payload,
                claimed_at=now,
            )
            session = FinalDeliverySession(
                {
                    OutboxMessage: message,
                    User: user,
                    AlertScope: scope,
                    AlertSnapshot: snapshot,
                    ProfileRow: profile,
                    PollRow: poll,
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

            allowed = await repo.deliver_if_authorized(
                str(message.id), now, deliver, expected_recipient=RECIPIENT
            )

            self.assertFalse(allowed)
            self.assertFalse(sent)
            if event_type == "RETURN_TO_ACTIVE_LESSON":
                self.assertIsNone(message.dead_at)
                self.assertEqual(
                    message.last_error_code,
                    "delivery_policy_temporarily_unavailable",
                )
                snapshot.state = AlertState.CLEAR_CONFIRMED.value
                message.claimed_at = now + timedelta(seconds=15)
                recovered = await repo.deliver_if_authorized(
                    str(message.id),
                    now + timedelta(seconds=15),
                    deliver,
                    expected_recipient=RECIPIENT,
                )
                self.assertTrue(recovered)
                self.assertTrue(sent)
                snapshot.state = AlertState.STALE.value
            else:
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
        session = FinalDeliverySession({OutboxMessage: message, User: user, ProfileRow: profile})
        repo = PostgresSenderRepository(  # type: ignore[arg-type]
            lambda: AsyncContext(session), stale_after_seconds=120, clock=lambda: now
        )
        sent = False

        async def deliver():
            nonlocal sent
            sent = True

        allowed = await repo.deliver_if_authorized(
            str(message.id), now, deliver, expected_recipient=RECIPIENT
        )

        self.assertFalse(allowed)
        self.assertFalse(sent)
        self.assertEqual(message.last_error_code, "final_delivery_policy_changed")

    async def test_return_expiry_is_rechecked_after_policy_locks_at_exact_boundary(self):
        valid_until = datetime(2026, 9, 21, 6, 5, tzinfo=UTC)

        async def attempt(send_at: datetime):
            authorization_at = valid_until - timedelta(seconds=1)
            user = User(
                id=uuid4(),
                telegram_user_id=42,
                mode="LESSONS_AND_ALERTS",
                confirmed_region_id="31",
                region_name="м. Київ",
            )
            scope = AlertScope(
                id=uuid4(),
                user_id=user.id,
                selected_region_id="31",
                applicable_region_ids=["31"],
            )
            binding = bind_scope(scope)
            snapshot = AlertSnapshot(scope_id=scope.id, state=AlertState.CLEAR_CONFIRMED.value)
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
                    "confirmed_region_id": "31",
                    "confirmed_region_name": "м. Київ",
                    "scope_binding": binding,
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
                str(message.id),
                authorization_at,
                deliver,
                expected_recipient=RECIPIENT,
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
        session = FinalDeliverySession({OutboxMessage: message, User: user, ProfileRow: profile})
        repo = PostgresSenderRepository(  # type: ignore[arg-type]
            lambda: AsyncContext(session), stale_after_seconds=120, clock=lambda: now
        )

        async def fail_delivery():
            raise RuntimeError("telegram unavailable")

        with self.assertRaisesRegex(RuntimeError, "telegram unavailable"):
            await repo.deliver_if_authorized(
                str(message.id),
                now,
                fail_delivery,
                expected_recipient=RECIPIENT,
            )

        retry_at = datetime(2026, 9, 21, 6, 0, 2, tzinfo=UTC)
        await repo.mark_retry(str(message.id), available_at=retry_at, error_code="transport_error")
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
