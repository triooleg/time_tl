from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from time_tl.domain.alerts import (
    ActiveEpisode,
    AlertState,
    AlertStateMachine,
    OfficialAlertSnapshot,
    RegionScope,
    SourceFailure,
    SourceFailureKind,
    TransitionKind,
)
from time_tl.domain.alerts import (
    AlertEpisode as DomainEpisode,
)
from time_tl.domain.alerts import (
    AlertSnapshot as DomainSnapshot,
)
from time_tl.infrastructure.alerts.repositories import AlertOutboxItem, AlertPollState

from .models import (
    AlertEpisode,
    AlertScope,
    AlertSnapshot,
    OutboxMessage,
    User,
)
from .models import (
    AlertPollState as PollRow,
)


class PostgresAlertRepository:
    """Durable fail-closed alert state and outbox in one DB transaction."""

    PROCESSED_ID_LIMIT = 256

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def register_scope_for_telegram_user(
        self, telegram_user_id: int, scope: RegionScope
    ) -> None:
        """Persist verified hierarchy; a hierarchy change resets state to UNKNOWN."""
        from .models import User

        async with self._factory() as session:
            async with session.begin():
                user = (
                    await session.execute(
                        select(User)
                        .where(User.telegram_user_id == telegram_user_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if user is None:
                    raise LookupError(telegram_user_id)
                row = (
                    await session.execute(
                        select(AlertScope).where(AlertScope.user_id == user.id).with_for_update()
                    )
                ).scalar_one_or_none()
                ids = sorted(scope.applicable_region_ids)
                changed = bool(
                    row
                    and (
                        row.selected_region_id != scope.selected_region_id
                        or sorted(row.applicable_region_ids) != ids
                    )
                )
                if row is None:
                    row = AlertScope(
                        user_id=user.id,
                        selected_region_id=scope.selected_region_id,
                        applicable_region_ids=ids,
                    )
                    session.add(row)
                    await session.flush()
                else:
                    row.selected_region_id = scope.selected_region_id
                    row.applicable_region_ids = ids
                if changed:
                    await session.execute(
                        delete(AlertEpisode).where(AlertEpisode.scope_id == row.id)
                    )
                    snapshot = await session.get(AlertSnapshot, row.id)
                    if snapshot is not None:
                        snapshot.state = AlertState.UNKNOWN.value
                        snapshot.last_fetch_revision = None
                        snapshot.last_full_active_episodes = None
                        snapshot.processed_update_ids = []
                        snapshot.pending_clear_candidate = False

    async def confirm_region_scope_for_telegram_user(
        self,
        telegram_user_id: int,
        *,
        confirmed_region_id: str,
        region_name: str,
        scope: RegionScope,
    ) -> None:
        """Atomically and idempotently confirm user region plus verified hierarchy."""
        if confirmed_region_id != scope.selected_region_id:
            raise ValueError("confirmed region and alert scope must identify the same region")
        async with self._factory() as session:
            async with session.begin():
                user = (
                    await session.execute(
                        select(User)
                        .where(User.telegram_user_id == telegram_user_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if user is None:
                    raise LookupError(telegram_user_id)
                row = (
                    await session.execute(
                        select(AlertScope).where(AlertScope.user_id == user.id).with_for_update()
                    )
                ).scalar_one_or_none()
                user.confirmed_region_id = confirmed_region_id
                user.region_name = region_name
                ids = sorted(scope.applicable_region_ids)
                changed = bool(
                    row
                    and (
                        row.selected_region_id != scope.selected_region_id
                        or sorted(row.applicable_region_ids) != ids
                    )
                )
                if row is None:
                    session.add(
                        AlertScope(
                            user_id=user.id,
                            selected_region_id=scope.selected_region_id,
                            applicable_region_ids=ids,
                        )
                    )
                    return
                row.selected_region_id = scope.selected_region_id
                row.applicable_region_ids = ids
                if changed:
                    await session.execute(
                        delete(AlertEpisode).where(AlertEpisode.scope_id == row.id)
                    )
                    snapshot = await session.get(AlertSnapshot, row.id)
                    if snapshot is not None:
                        snapshot.state = AlertState.UNKNOWN.value
                        snapshot.last_fetch_revision = None
                        snapshot.last_full_active_episodes = None
                        snapshot.processed_update_ids = []
                        snapshot.pending_clear_candidate = False

    @staticmethod
    def mode_allows_alert_notifications(mode: str | None) -> bool:
        return mode in {"LESSONS_AND_ALERTS", "ALERTS_ONLY"}

    async def poll_state(self) -> AlertPollState:
        async with self._factory() as session:
            row = await session.get(PollRow, 1)
            if row is None:
                raise RuntimeError("alert_poll_state singleton is missing; run migrations")
            retry = 0.0
            if row.retry_not_before is not None:
                seconds = max(0.0, (row.retry_not_before - datetime.now(UTC)).total_seconds())
                retry = time.monotonic() + seconds
            return AlertPollState(
                fetch_revision=row.fetch_revision,
                last_modified=row.last_modified,
                circuit_open=row.circuit_open,
                retry_not_before_monotonic=retry,
                consecutive_failures=row.consecutive_failures,
            )

    async def set_poll_state(self, state: AlertPollState) -> None:
        async with self._factory() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(PollRow).where(PollRow.singleton_id == 1).with_for_update()
                    )
                ).scalar_one_or_none()
                if row is None:
                    raise RuntimeError("alert_poll_state singleton is missing; run migrations")
                if state.fetch_revision != row.fetch_revision + 1:
                    raise ValueError("fetch revision must be the exact next durable revision")
                self._store_poll_state(row, state)

    async def apply_snapshot_and_poll_state_atomically(
        self, update: OfficialAlertSnapshot, poll_state: AlertPollState
    ) -> tuple[AlertOutboxItem, ...]:
        created: list[AlertOutboxItem] = []
        async with self._factory() as session:
            async with session.begin():
                poll_row = (
                    await session.execute(
                        select(PollRow).where(PollRow.singleton_id == 1).with_for_update()
                    )
                ).scalar_one_or_none()
                if poll_row is None:
                    raise RuntimeError("alert_poll_state singleton is missing; run migrations")
                if poll_state.fetch_revision != update.fetch_revision:
                    raise ValueError("poll state and snapshot revisions must match")
                if poll_state.fetch_revision != poll_row.fetch_revision + 1:
                    raise ValueError("fetch revision must be the exact next durable revision")
                scopes = list(
                    (
                        await session.execute(
                            select(AlertScope).order_by(AlertScope.id).with_for_update()
                        )
                    ).scalars()
                )
                evaluated = []
                rejected = False
                for scope in scopes:
                    original = await self._load_snapshot(session, scope.id)
                    machine = AlertStateMachine(self._scope(scope), original)
                    transition = machine.apply_snapshot(update)
                    rejected = rejected or (
                        transition.reason == SourceFailureKind.NON_MONOTONIC_REVISION.value
                    )
                    evaluated.append((scope, original, machine.snapshot(), transition))

                if rejected:
                    failure = SourceFailure(SourceFailureKind.PROCESS_RESTART)
                    for scope, original, _snapshot, _transition in evaluated:
                        machine = AlertStateMachine(self._scope(scope), original)
                        machine.apply_failure(failure)
                        await self._store_snapshot(session, scope.id, machine.snapshot())
                    return ()

                # Only an accepted provider snapshot reaches this point. Source
                # freshness, snapshots, outbox and cursor commit together.
                poll_row.last_success_at = update.fetched_at
                for scope, _original, snapshot, transition in evaluated:
                    await self._store_snapshot(session, scope.id, snapshot)
                    if transition.kind in {
                        TransitionKind.ALERT_STARTED,
                        TransitionKind.ALL_CLEAR_CONFIRMED,
                    }:
                        user = await session.get(User, scope.user_id)
                        if user is None:
                            raise RuntimeError("alert scope owner is missing")
                        if not self.mode_allows_alert_notifications(user.mode):
                            continue
                        scope_binding = self._scope_binding(scope)
                        if scope_binding is None:
                            # Legacy/unversioned scopes cannot safely bind an
                            # event to a reviewed territory hierarchy revision.
                            continue
                        key = f"{scope.id}:{transition.kind.value}:{transition.event_id}"
                        statement = (
                            insert(OutboxMessage)
                            .values(
                                event_key=key,
                                aggregate_type="alert_scope",
                                aggregate_id=str(scope.id),
                                event_type=transition.kind.value,
                                payload={
                                    "scope_id": str(scope.id),
                                    "transition_kind": transition.kind.value,
                                    "event_id": transition.event_id,
                                    "telegram_chat_id": user.telegram_user_id,
                                    "scope_binding": scope_binding,
                                },
                            )
                            .on_conflict_do_nothing(index_elements=[OutboxMessage.event_key])
                            .returning(OutboxMessage.event_key)
                        )
                        inserted = (await session.execute(statement)).scalar_one_or_none()
                        if inserted:
                            created.append(AlertOutboxItem(key, str(scope.id), transition.kind))
                self._store_poll_state(poll_row, poll_state)
        return tuple(created)

    @staticmethod
    def _store_poll_state(row: PollRow, state: AlertPollState) -> None:
        row.fetch_revision = state.fetch_revision
        row.last_modified = state.last_modified
        row.circuit_open = state.circuit_open
        row.consecutive_failures = state.consecutive_failures
        remaining = max(0.0, state.retry_not_before_monotonic - time.monotonic())
        row.retry_not_before = (
            datetime.now(UTC) + timedelta(seconds=remaining) if remaining else None
        )

    async def apply_failure_atomically(self, failure: SourceFailure) -> None:
        async with self._factory() as session:
            async with session.begin():
                for scope in (
                    await session.execute(
                        select(AlertScope).order_by(AlertScope.id).with_for_update()
                    )
                ).scalars():
                    machine = AlertStateMachine(
                        self._scope(scope), await self._load_snapshot(session, scope.id)
                    )
                    machine.apply_failure(failure)
                    await self._store_snapshot(session, scope.id, machine.snapshot())

    async def apply_failure_and_poll_state_atomically(
        self, failure: SourceFailure, poll_state: AlertPollState
    ) -> None:
        """Commit failure state and its exact-next cursor as one transaction."""

        async with self._factory() as session:
            async with session.begin():
                poll_row = (
                    await session.execute(
                        select(PollRow).where(PollRow.singleton_id == 1).with_for_update()
                    )
                ).scalar_one_or_none()
                if poll_row is None:
                    raise RuntimeError("alert_poll_state singleton is missing; run migrations")
                if poll_state.fetch_revision != poll_row.fetch_revision + 1:
                    raise ValueError("fetch revision must be the exact next durable revision")
                scopes = list(
                    (
                        await session.execute(
                            select(AlertScope).order_by(AlertScope.id).with_for_update()
                        )
                    ).scalars()
                )
                for scope in scopes:
                    machine = AlertStateMachine(
                        self._scope(scope), await self._load_snapshot(session, scope.id)
                    )
                    machine.apply_failure(failure)
                    await self._store_snapshot(session, scope.id, machine.snapshot())
                self._store_poll_state(poll_row, poll_state)

    async def apply_process_restart_atomically(self) -> None:
        """Invalidate freshness and conditionals before the first provider request."""
        async with self._factory() as session:
            async with session.begin():
                poll_row = (
                    await session.execute(
                        select(PollRow).where(PollRow.singleton_id == 1).with_for_update()
                    )
                ).scalar_one_or_none()
                if poll_row is None:
                    raise RuntimeError("alert_poll_state singleton is missing; run migrations")
                scopes = list(
                    (
                        await session.execute(
                            select(AlertScope).order_by(AlertScope.id).with_for_update()
                        )
                    ).scalars()
                )
                failure = SourceFailure(SourceFailureKind.PROCESS_RESTART)
                for scope in scopes:
                    machine = AlertStateMachine(
                        self._scope(scope), await self._load_snapshot(session, scope.id)
                    )
                    machine.apply_failure(failure)
                    await self._store_snapshot(session, scope.id, machine.snapshot())
                poll_row.fetch_revision += 1
                poll_row.last_modified = None
                poll_row.last_success_at = None

    @staticmethod
    def _scope(row: AlertScope) -> RegionScope:
        values = frozenset(str(item) for item in row.applicable_region_ids)
        return RegionScope(row.selected_region_id, values)

    @staticmethod
    def _scope_binding(row: AlertScope) -> dict[str, object] | None:
        provider = (row.territory_provider or "").strip()
        catalog_version = (row.territory_catalog_version or "").strip()
        if not provider or not catalog_version:
            return None
        return {
            "provider": provider,
            "catalog_version": catalog_version,
            "selected_region_id": row.selected_region_id,
            "applicable_region_ids": sorted(set(row.applicable_region_ids)),
        }

    async def _load_snapshot(self, session: AsyncSession, scope_id: UUID) -> DomainSnapshot | None:
        row = await session.get(AlertSnapshot, scope_id)
        if row is None:
            return None
        episodes = (
            (await session.execute(select(AlertEpisode).where(AlertEpisode.scope_id == scope_id)))
            .scalars()
            .all()
        )
        active = tuple(
            ActiveEpisode(
                DomainEpisode(item.location_uid, item.alert_type, item.episode_id),
                item.active_revision,
            )
            for item in episodes
        )
        baseline = (
            None
            if row.last_full_active_episodes is None
            else tuple(self._episode(item) for item in row.last_full_active_episodes)
        )
        return DomainSnapshot(
            state=AlertState(row.state),
            active_episodes=active,
            last_full_active_episodes=baseline,
            processed_update_ids=frozenset(row.processed_update_ids),
            last_fetch_revision=row.last_fetch_revision,
            pending_clear_candidate=row.pending_clear_candidate,
        )

    async def _store_snapshot(
        self, session: AsyncSession, scope_id: UUID, snapshot: DomainSnapshot
    ) -> None:
        row = await session.get(AlertSnapshot, scope_id)
        if row is None:
            row = AlertSnapshot(scope_id=scope_id)
            session.add(row)
        row.state = snapshot.state.value
        row.last_fetch_revision = snapshot.last_fetch_revision
        row.last_full_active_episodes = (
            None
            if snapshot.last_full_active_episodes is None
            else [self._episode_data(item) for item in snapshot.last_full_active_episodes]
        )
        row.processed_update_ids = sorted(snapshot.processed_update_ids)[-self.PROCESSED_ID_LIMIT :]
        row.pending_clear_candidate = snapshot.pending_clear_candidate
        row.updated_at = datetime.now(UTC)
        # ``AlertEpisode`` references ``alert_snapshots.scope_id``. The
        # mappers intentionally do not expose an ORM relationship, so the unit
        # of work cannot infer their dependency order when both rows are new.
        # Persist the parent explicitly before adding any child episodes while
        # keeping all writes inside the caller's transaction.
        await session.flush((row,))
        await session.execute(delete(AlertEpisode).where(AlertEpisode.scope_id == scope_id))
        for item in snapshot.active_episodes:
            session.add(
                AlertEpisode(
                    scope_id=scope_id,
                    location_uid=item.episode.location_uid,
                    alert_type=item.episode.alert_type,
                    episode_id=item.episode.episode_id,
                    active_revision=item.active_revision,
                )
            )

    @staticmethod
    def _episode_data(item: DomainEpisode) -> dict[str, str]:
        return {
            "location_uid": item.location_uid,
            "alert_type": item.alert_type,
            "episode_id": item.episode_id,
        }

    @staticmethod
    def _episode(item: dict[str, str]) -> DomainEpisode:
        return DomainEpisode(item["location_uid"], item["alert_type"], item["episode_id"])
