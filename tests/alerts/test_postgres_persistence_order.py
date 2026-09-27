from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from time_tl.domain.alerts import (
    ActiveEpisode,
    AlertEpisode,
    AlertSnapshot,
    AlertState,
    OfficialAlertSnapshot,
    ProviderSnapshotKind,
    SourceFailure,
    SourceFailureKind,
)
from time_tl.infrastructure.alerts.repositories import AlertPollState
from time_tl.infrastructure.db.alerts import PostgresAlertRepository
from time_tl.infrastructure.db.models import (
    AlertEpisode as AlertEpisodeRow,
)
from time_tl.infrastructure.db.models import AlertPollState as PollRow
from time_tl.infrastructure.db.models import (
    AlertSnapshot as AlertSnapshotRow,
)


class RecordingSession:
    def __init__(self) -> None:
        self.events: list[tuple[str, tuple[str, ...]]] = []

    async def get(self, model: type[Any], identity: UUID) -> None:
        assert model is AlertSnapshotRow
        assert isinstance(identity, UUID)
        return None

    def add(self, row: object) -> None:
        self.events.append(("add", (type(row).__name__,)))

    async def flush(self, objects: tuple[object, ...]) -> None:
        self.events.append(("flush", tuple(type(row).__name__ for row in objects)))

    async def execute(self, statement: Any) -> None:
        assert statement is not None
        self.events.append(("execute", ()))


@pytest.mark.asyncio
async def test_new_snapshot_is_flushed_before_episode_children_are_added() -> None:
    scope_id = uuid4()
    snapshot = AlertSnapshot(
        state=AlertState.ACTIVE,
        active_episodes=(
            ActiveEpisode(AlertEpisode("14", "air_raid", "episode-1"), active_revision=1),
        ),
        last_full_active_episodes=(AlertEpisode("14", "air_raid", "episode-1"),),
        processed_update_ids=frozenset({"active-1"}),
        last_fetch_revision=1,
    )
    repository = PostgresAlertRepository(None)  # type: ignore[arg-type]
    session = RecordingSession()

    await repository._store_snapshot(session, scope_id, snapshot)  # type: ignore[arg-type]  # noqa: SLF001

    assert session.events == [
        ("add", (AlertSnapshotRow.__name__,)),
        ("flush", (AlertSnapshotRow.__name__,)),
        ("execute", ()),
        ("add", (AlertEpisodeRow.__name__,)),
    ]


class Result:
    def __init__(self, *, scalar: Any = None, rows: tuple[Any, ...] = ()) -> None:
        self.scalar = scalar
        self.rows = rows

    def scalar_one_or_none(self) -> Any:
        return self.scalar

    def scalars(self) -> tuple[Any, ...]:
        return self.rows


class Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: Any) -> None:
        return None


class PollSession:
    def __init__(self, poll: PollRow, *, scopes: tuple[Any, ...] = ()) -> None:
        self.poll = poll
        self.scopes = scopes
        self.statements: list[str] = []

    async def __aenter__(self) -> PollSession:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    def begin(self) -> Transaction:
        return Transaction()

    async def execute(self, statement: Any) -> Result:
        rendered = str(statement)
        self.statements.append(rendered)
        if len(self.statements) == 1:
            return Result(scalar=self.poll)
        return Result(rows=self.scopes)

    async def get(self, model: type[Any], identity: int) -> Any:
        assert model is PollRow
        assert identity == 1
        return self.poll


class PollFactory:
    def __init__(self, session: PollSession) -> None:
        self.session = session

    def __call__(self) -> PollSession:
        return self.session


def provider_update(
    revision: int, *, kind: ProviderSnapshotKind = ProviderSnapshotKind.FULL_200
) -> OfficialAlertSnapshot:
    return OfficialAlertSnapshot(
        update_id=f"provider-{revision}",
        fetch_revision=revision,
        kind=kind,
        covered_region_ids=frozenset({"14"}),
        active_episodes=(),
        fetched_at=datetime(2026, 9, 24, 12, tzinfo=UTC),
        is_complete=True,
        is_fresh=True,
    )


@pytest.mark.asyncio
async def test_inflight_pre_activation_304_cannot_restore_marker_or_freshness() -> None:
    poll = PollRow(
        singleton_id=1,
        fetch_revision=8,
        last_modified=None,
        last_success_at=None,
        circuit_open=False,
    )
    session = PollSession(poll)
    repository = PostgresAlertRepository(PollFactory(session))  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="exact next"):
        await repository.apply_snapshot_and_poll_state_atomically(
            provider_update(8, kind=ProviderSnapshotKind.NOT_MODIFIED_304),
            AlertPollState(fetch_revision=8, last_modified="retired-marker"),
        )

    assert len(session.statements) == 1
    assert poll.fetch_revision == 8
    assert poll.last_modified is None
    assert poll.last_success_at is None


@pytest.mark.asyncio
async def test_inflight_pre_activation_failure_cannot_advance_reset_cursor() -> None:
    poll = PollRow(
        singleton_id=1,
        fetch_revision=8,
        last_modified=None,
        last_success_at=None,
        circuit_open=False,
    )
    session = PollSession(poll)
    repository = PostgresAlertRepository(PollFactory(session))  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="exact next"):
        await repository.apply_failure_and_poll_state_atomically(
            SourceFailure(SourceFailureKind.TIMEOUT),
            AlertPollState(fetch_revision=8, consecutive_failures=1),
        )

    assert len(session.statements) == 1
    assert poll.fetch_revision == 8
    assert poll.last_modified is None


@pytest.mark.asyncio
async def test_first_post_activation_poll_is_unconditional_and_exact_next_full_recovers() -> None:
    poll = PollRow(
        singleton_id=1,
        fetch_revision=8,
        last_modified=None,
        last_success_at=None,
        circuit_open=False,
    )
    session = PollSession(poll)
    repository = PostgresAlertRepository(PollFactory(session))  # type: ignore[arg-type]

    restored = await repository.poll_state()
    assert restored.fetch_revision == 8
    assert restored.last_modified is None

    await repository.apply_snapshot_and_poll_state_atomically(
        provider_update(9),
        AlertPollState(fetch_revision=9, last_modified="fresh-marker"),
    )

    assert poll.fetch_revision == 9
    assert poll.last_modified == "fresh-marker"
    assert poll.last_success_at == datetime(2026, 9, 24, 12, tzinfo=UTC)


@pytest.mark.asyncio
async def test_failure_state_and_exact_next_cursor_share_one_transaction() -> None:
    poll = PollRow(
        singleton_id=1,
        fetch_revision=8,
        last_modified=None,
        last_success_at=None,
        circuit_open=False,
    )
    session = PollSession(poll)
    repository = PostgresAlertRepository(PollFactory(session))  # type: ignore[arg-type]

    await repository.apply_failure_and_poll_state_atomically(
        SourceFailure(SourceFailureKind.TIMEOUT),
        AlertPollState(
            fetch_revision=9,
            last_modified=None,
            consecutive_failures=1,
        ),
    )

    assert len(session.statements) == 2
    assert "alert_poll_state" in session.statements[0]
    assert "FOR UPDATE" in session.statements[0]
    assert "alert_scopes" in session.statements[1]
    assert "FOR UPDATE" in session.statements[1]
    assert poll.fetch_revision == 9
    assert poll.consecutive_failures == 1
