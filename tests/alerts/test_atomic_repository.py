from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from time_tl.application.alert_worker import AlertWorker
from time_tl.domain.alerts import (
    AlertEpisode,
    AlertSnapshot,
    AlertState,
    AlertStateMachine,
    OfficialAlertSnapshot,
    ProviderSnapshotKind,
    RegionScope,
    TransitionKind,
)
from time_tl.infrastructure.alerts.repositories import (
    AlertPollState,
    InMemoryAlertRepository,
)
from time_tl.runtime.local_alerts import JsonAlertRepository

NOW = datetime(2026, 9, 23, 8, tzinfo=UTC)
SCOPE = RegionScope.from_hierarchy("14")
ACTIVE_EPISODE = AlertEpisode("14", "air_raid", "oblast-air-raid:14")


def update(
    revision: int,
    update_id: str,
    *,
    active: bool,
    kind: ProviderSnapshotKind = ProviderSnapshotKind.FULL_200,
) -> OfficialAlertSnapshot:
    return OfficialAlertSnapshot(
        update_id=update_id,
        fetch_revision=revision,
        kind=kind,
        covered_region_ids=frozenset({"14"}),
        active_episodes=(ACTIVE_EPISODE,) if active else (),
        fetched_at=NOW,
        is_complete=True,
        is_fresh=True,
    )


@pytest.mark.asyncio
async def test_in_memory_exception_commits_neither_snapshot_nor_poll_state() -> None:
    repository = InMemoryAlertRepository([("first", SCOPE), ("second", SCOPE)])
    original = AlertStateMachine.apply_snapshot
    calls = 0

    def fail_on_second(machine: AlertStateMachine, snapshot: OfficialAlertSnapshot):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected crash")
        return original(machine, snapshot)

    with patch.object(AlertStateMachine, "apply_snapshot", fail_on_second):
        with pytest.raises(RuntimeError, match="injected crash"):
            await repository.apply_snapshot_and_poll_state_atomically(
                update(1, "active-1", active=True),
                AlertPollState(fetch_revision=1, last_modified="m-active"),
            )

    assert await repository.snapshot_for("first") is None
    assert await repository.snapshot_for("second") is None
    assert await repository.poll_state() == AlertPollState()
    assert await repository.outbox() == ()


@pytest.mark.asyncio
async def test_reused_non_monotonic_active_cannot_publish_marker_or_false_clear() -> None:
    repository = InMemoryAlertRepository([("student", SCOPE)])
    await repository.apply_snapshot_and_poll_state_atomically(
        update(1, "clear-1", active=False),
        AlertPollState(fetch_revision=1, last_modified="m-clear"),
    )

    # Inject the durable mismatch left by the legacy two-transaction worker:
    # the per-scope revision is ahead while the poll cursor still points to
    # the old validated CLEAR response.
    repository._snapshots["student"] = AlertSnapshot(  # noqa: SLF001
        state=AlertState.CLEAR_CONFIRMED,
        last_full_active_episodes=(),
        processed_update_ids=frozenset({"clear-1"}),
        last_fetch_revision=2,
    )

    rejected = await repository.apply_snapshot_and_poll_state_atomically(
        update(2, "active-reused-2", active=True),
        AlertPollState(fetch_revision=2, last_modified="m-active"),
    )

    assert rejected == ()
    assert await repository.poll_state() == AlertPollState(
        fetch_revision=1, last_modified="m-clear"
    )
    reset = await repository.snapshot_for("student")
    assert reset is not None
    assert reset.state is AlertState.UNKNOWN
    assert reset.last_full_active_episodes is None
    assert reset.last_fetch_revision is None

    # The same revision is now safe to retry. Its ACTIVE baseline commits with
    # the marker; later 304 observations can only confirm ACTIVE, never CLEAR.
    await repository.apply_snapshot_and_poll_state_atomically(
        update(2, "active-reused-2", active=True),
        AlertPollState(fetch_revision=2, last_modified="m-active"),
    )
    for revision in (3, 4):
        await repository.apply_snapshot_and_poll_state_atomically(
            update(
                revision,
                f"not-modified-{revision}",
                active=False,
                kind=ProviderSnapshotKind.NOT_MODIFIED_304,
            ),
            AlertPollState(fetch_revision=revision, last_modified="m-active"),
        )

    snapshot = await repository.snapshot_for("student")
    assert snapshot is not None
    assert snapshot.state is AlertState.ACTIVE
    assert all(
        item.transition_kind is not TransitionKind.ALL_CLEAR_CONFIRMED
        for item in await repository.outbox()
    )


@pytest.mark.asyncio
async def test_identical_replay_is_idempotent() -> None:
    repository = InMemoryAlertRepository([("student", SCOPE)])
    active = update(1, "active-1", active=True)
    poll = AlertPollState(fetch_revision=1, last_modified="m-active")

    first = await repository.apply_snapshot_and_poll_state_atomically(active, poll)
    second = await repository.apply_snapshot_and_poll_state_atomically(active, poll)

    assert len(first) == 1
    assert second == ()
    assert len(await repository.outbox()) == 1
    assert await repository.poll_state() == poll


@pytest.mark.asyncio
async def test_json_persist_failure_exposes_neither_half_of_success(tmp_path: Path) -> None:
    path = tmp_path / "alerts.json"
    repository = JsonAlertRepository(path, [("student", SCOPE)])
    active = update(1, "active-1", active=True)
    poll = AlertPollState(fetch_revision=1, last_modified="m-active")

    with patch.object(repository, "_persist_values", side_effect=OSError("injected crash")):
        with pytest.raises(OSError, match="injected crash"):
            await repository.apply_snapshot_and_poll_state_atomically(active, poll)

    assert await repository.snapshot_for("student") is None
    assert await repository.poll_state() == AlertPollState()
    assert await repository.outbox() == ()
    assert not path.exists()

    await repository.apply_snapshot_and_poll_state_atomically(active, poll)
    restored = JsonAlertRepository(path, [("student", SCOPE)])
    assert (await restored.snapshot_for("student")).state is AlertState.ACTIVE  # type: ignore[union-attr]
    assert await restored.poll_state() == poll
    assert len(await restored.outbox()) == 1


@pytest.mark.asyncio
async def test_process_restart_is_atomic_and_never_enqueues_clear() -> None:
    repository = InMemoryAlertRepository([("student", SCOPE)])
    await repository.apply_snapshot_and_poll_state_atomically(
        update(1, "active-1", active=True),
        AlertPollState(fetch_revision=1, last_modified="m-active"),
    )
    before = await repository.outbox()

    await repository.apply_process_restart_atomically()

    snapshot = await repository.snapshot_for("student")
    assert snapshot is not None
    assert snapshot.state is AlertState.STALE
    assert {item.episode for item in snapshot.active_episodes} == {ACTIVE_EPISODE}
    assert (await repository.poll_state()).last_modified is None
    assert await repository.outbox() == before

    await repository.apply_snapshot_and_poll_state_atomically(
        update(3, "empty-3", active=False),
        AlertPollState(fetch_revision=3, last_modified="m-empty-3"),
    )
    first = await repository.snapshot_for("student")
    assert first is not None
    assert first.state is AlertState.STALE
    assert not any(
        item.transition_kind is TransitionKind.ALL_CLEAR_CONFIRMED
        for item in await repository.outbox()
    )


@pytest.mark.asyncio
async def test_losing_alert_worker_replica_never_applies_restart_fence() -> None:
    lease = AsyncMock()
    lease.acquire.return_value = False
    repository = AsyncMock()
    worker = AlertWorker(
        provider=AsyncMock(),
        repository=repository,
        lease=lease,
        monotonic=lambda: 0.0,
        jitter=lambda _low, _high: 0.0,
    )

    assert await worker.acquire_and_prepare() is False

    repository.apply_process_restart_atomically.assert_not_awaited()
    lease.release.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_is_not_prepared_until_restart_fence_commits() -> None:
    lease = AsyncMock()
    lease.acquire.return_value = True
    entered = asyncio.Event()
    release = asyncio.Event()
    repository = AsyncMock()

    async def blocking_fence() -> None:
        entered.set()
        await release.wait()

    repository.apply_process_restart_atomically.side_effect = blocking_fence
    worker = AlertWorker(
        provider=AsyncMock(),
        repository=repository,
        lease=lease,
        monotonic=lambda: 0.0,
        jitter=lambda _low, _high: 0.0,
    )

    task = asyncio.create_task(worker.acquire_and_prepare())
    await entered.wait()
    assert worker._prepared is False  # noqa: SLF001
    release.set()
    assert await task is True
    assert worker._prepared is True  # noqa: SLF001
    await lease.release()
