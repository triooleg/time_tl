from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from time_tl.domain.alerts import (
    AlertEpisode as DomainEpisode,
)
from time_tl.domain.alerts import (
    AlertStateMachine,
    OfficialAlertSnapshot,
    ProviderSnapshotKind,
    RegionScope,
    TransitionKind,
)
from time_tl.infrastructure.db.accounts import AsyncPostgresAccountRepository
from time_tl.infrastructure.db.models import AlertScope, AlertSnapshot, User


class ScalarResult:
    def __init__(self, value: Any = None) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value


class ModeTransitionSession:
    def __init__(self, results: list[ScalarResult]) -> None:
        self.results = results
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> ScalarResult:
        self.statements.append(statement)
        return self.results.pop(0)


def _scope(user: User) -> AlertScope:
    return AlertScope(
        id=uuid4(),
        user_id=user.id,
        selected_region_id="901",
        applicable_region_ids=["9", "90", "901"],
        territory_provider="alerts.in.ua",
        territory_catalog_version="reviewed-v1",
    )


def _snapshot(scope: AlertScope) -> AlertSnapshot:
    return AlertSnapshot(
        scope_id=scope.id,
        state="ACTIVE",
        last_fetch_revision=17,
        last_full_active_episodes=[{"episode_id": "active"}],
        processed_update_ids=["active-17"],
        pending_clear_candidate=True,
        updated_at=datetime(2026, 9, 24, tzinfo=UTC),
    )


def _compiled_values(statement: Any) -> tuple[object, ...]:
    return tuple(statement.compile().params.values())


@pytest.mark.asyncio
async def test_disabled_to_enabled_resets_alert_state_and_invalidates_pending_rows() -> None:
    user = User(
        id=uuid4(),
        telegram_user_id=42,
        mode="LESSONS_ONLY",
        alerts_enabled=False,
        lessons_enabled=True,
        confirmed_region_id="901",
        region_name="Кам'янська громада",
        reminder_lead_minutes=9,
    )
    scope = _scope(user)
    snapshot = _snapshot(scope)
    session = ModeTransitionSession(
        [ScalarResult(scope), ScalarResult(), ScalarResult(), ScalarResult(snapshot)]
    )
    repository = AsyncPostgresAccountRepository(None)  # type: ignore[arg-type]

    await repository._apply_alert_mode_transition(
        session,  # type: ignore[arg-type]
        user,
        next_mode="LESSONS_AND_ALERTS",
        next_alerts_enabled=True,
    )

    sql = [str(statement) for statement in session.statements]
    assert "FROM alert_scopes" in sql[0] and "FOR UPDATE" in sql[0]
    assert sql[1].startswith("UPDATE outbox_messages SET")
    assert "outbox_messages.delivered_at IS NULL" in sql[1]
    assert "outbox_messages.dead_at IS NULL" in sql[1]
    update_values = _compiled_values(session.statements[1])
    assert "mode_generation_invalidated" in update_values
    assert ["ALERT_STARTED", "ALL_CLEAR_CONFIRMED"] in update_values
    assert "RETURN_TO_ACTIVE_LESSON" in update_values
    assert sql[2].startswith("DELETE FROM alert_episodes")
    assert "FROM alert_snapshots" in sql[3] and "FOR UPDATE" in sql[3]
    assert snapshot.state == "UNKNOWN"
    assert snapshot.last_fetch_revision is None
    assert snapshot.last_full_active_episodes is None
    assert snapshot.processed_update_ids == []
    assert snapshot.pending_clear_candidate is False

    # The toggle helper preserves identity, preferences, and the reviewed scope binding.
    assert user.mode == "LESSONS_ONLY"
    assert user.confirmed_region_id == "901"
    assert user.reminder_lead_minutes == 9
    assert scope.selected_region_id == "901"
    assert scope.applicable_region_ids == ["9", "90", "901"]
    assert scope.territory_provider == "alerts.in.ua"
    assert scope.territory_catalog_version == "reviewed-v1"


@pytest.mark.asyncio
async def test_enabled_to_disabled_invalidates_queued_safety_rows_without_clear() -> None:
    user = User(
        id=uuid4(),
        telegram_user_id=42,
        mode="LESSONS_AND_ALERTS",
        alerts_enabled=True,
        lessons_enabled=True,
    )
    scope = _scope(user)
    session = ModeTransitionSession([ScalarResult(scope), ScalarResult()])
    repository = AsyncPostgresAccountRepository(None)  # type: ignore[arg-type]

    await repository._apply_alert_mode_transition(
        session,  # type: ignore[arg-type]
        user,
        next_mode="LESSONS_ONLY",
        next_alerts_enabled=False,
    )

    sql = [str(statement) for statement in session.statements]
    assert len(sql) == 2
    assert sql[1].startswith("UPDATE outbox_messages SET")
    values = _compiled_values(session.statements[1])
    assert "mode_generation_invalidated" in values
    assert ["ALERT_STARTED", "ALL_CLEAR_CONFIRMED"] in values
    assert "RETURN_TO_ACTIVE_LESSON" in values
    assert all("DELETE FROM alert_episodes" not in statement for statement in sql)


@pytest.mark.asyncio
async def test_combined_lesson_alert_mode_change_invalidates_only_return_generation() -> None:
    user = User(
        id=uuid4(),
        telegram_user_id=42,
        mode="ALERTS_ONLY",
        alerts_enabled=True,
        lessons_enabled=False,
    )
    session = ModeTransitionSession([ScalarResult()])
    repository = AsyncPostgresAccountRepository(None)  # type: ignore[arg-type]

    await repository._apply_alert_mode_transition(
        session,  # type: ignore[arg-type]
        user,
        next_mode="LESSONS_AND_ALERTS",
        next_alerts_enabled=True,
    )

    values = _compiled_values(session.statements[0])
    assert "RETURN_TO_ACTIVE_LESSON" in values
    assert ["ALERT_STARTED", "ALL_CLEAR_CONFIRMED"] not in values


def test_unknown_reset_allows_exactly_one_start_for_next_fresh_active_poll() -> None:
    machine = AlertStateMachine(RegionScope.from_hierarchy("901", ancestors=("9", "90")))
    episode = DomainEpisode("901", "air_raid", "active-901")
    update = OfficialAlertSnapshot(
        update_id="fresh-after-enable",
        fetch_revision=18,
        kind=ProviderSnapshotKind.FULL_200,
        covered_region_ids=frozenset({"9", "90", "901"}),
        active_episodes=(episode,),
        fetched_at=datetime(2026, 9, 24, 12, tzinfo=UTC),
        is_complete=True,
        is_fresh=True,
    )

    first = machine.apply_snapshot(update)
    duplicate = machine.apply_snapshot(update)

    assert first.kind is TransitionKind.ALERT_STARTED
    assert duplicate.kind is TransitionKind.NONE
