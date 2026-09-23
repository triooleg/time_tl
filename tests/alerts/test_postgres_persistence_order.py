from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from time_tl.domain.alerts import (
    ActiveEpisode,
    AlertEpisode,
    AlertSnapshot,
    AlertState,
)
from time_tl.infrastructure.db.alerts import PostgresAlertRepository
from time_tl.infrastructure.db.models import (
    AlertEpisode as AlertEpisodeRow,
)
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
