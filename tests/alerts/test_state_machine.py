import unittest
from datetime import UTC, datetime

from time_tl.domain.alerts import (
    ActiveEpisode,
    AlertEpisode,
    AlertSnapshot,
    AlertState,
    AlertStateMachine,
    OfficialAlertSnapshot,
    ProviderSnapshotKind,
    RegionScope,
    SourceFailure,
    SourceFailureKind,
    TransitionKind,
)

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
SCOPE_IDS = frozenset({"community", "district", "region", "child"})


def episode(episode_id: str, location: str = "community", kind: str = "air_raid") -> AlertEpisode:
    return AlertEpisode(location, kind, episode_id)


def full(
    revision: int,
    episodes: tuple[AlertEpisode, ...],
    *,
    complete: bool = True,
    fresh: bool = True,
    covered: frozenset[str] = SCOPE_IDS,
) -> OfficialAlertSnapshot:
    return OfficialAlertSnapshot(
        update_id=f"full-{revision}",
        fetch_revision=revision,
        kind=ProviderSnapshotKind.FULL_200,
        covered_region_ids=covered,
        active_episodes=episodes,
        fetched_at=NOW,
        is_complete=complete,
        is_fresh=fresh,
    )


def unchanged(revision: int) -> OfficialAlertSnapshot:
    return OfficialAlertSnapshot(
        update_id=f"304-{revision}",
        fetch_revision=revision,
        kind=ProviderSnapshotKind.NOT_MODIFIED_304,
        covered_region_ids=SCOPE_IDS,
        active_episodes=(),
        fetched_at=NOW,
        is_complete=True,
        is_fresh=True,
    )


class AlertStateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scope = RegionScope.from_hierarchy(
            "community", ancestors=("district", "region"), descendants=("child",)
        )
        self.machine = AlertStateMachine(self.scope)
        self.air = episode("alert-1")

    def start(self) -> None:
        transition = self.machine.apply_snapshot(full(1, (self.air,)))
        self.assertEqual(TransitionKind.ALERT_STARTED, transition.kind)
        self.assertEqual(AlertState.ACTIVE, self.machine.state)

    def test_first_disappearance_is_only_candidate_second_304_confirms(self) -> None:
        self.start()
        candidate = self.machine.apply_snapshot(full(2, ()))
        confirmed = self.machine.apply_snapshot(unchanged(3))
        self.assertEqual(TransitionKind.DEGRADED, candidate.kind)
        self.assertEqual(AlertState.STALE, candidate.state)
        self.assertEqual(TransitionKind.ALL_CLEAR_CONFIRMED, confirmed.kind)
        self.assertEqual(AlertState.CLEAR_CONFIRMED, confirmed.state)

    def test_second_full_empty_snapshot_can_confirm_clear(self) -> None:
        self.start()
        self.machine.apply_snapshot(full(2, ()))
        confirmed = self.machine.apply_snapshot(full(3, ()))
        self.assertEqual(TransitionKind.ALL_CLEAR_CONFIRMED, confirmed.kind)

    def test_multiple_ids_same_region_are_independent_episodes(self) -> None:
        artillery = episode("alert-2", kind="artillery_shelling")
        self.machine.apply_snapshot(full(1, (self.air, artillery)))
        one_disappeared = self.machine.apply_snapshot(full(2, (artillery,)))
        self.assertEqual(TransitionKind.NONE, one_disappeared.kind)
        self.assertEqual(AlertState.ACTIVE, self.machine.state)
        self.assertEqual(frozenset({artillery}), self.machine.active_episodes)

    def test_parent_episode_blocks_aggregate_clear(self) -> None:
        parent = episode("parent-alert", location="region")
        self.machine.apply_snapshot(full(1, (self.air, parent)))
        transition = self.machine.apply_snapshot(full(2, (parent,)))
        self.assertEqual(AlertState.ACTIVE, transition.state)
        self.assertEqual(frozenset({parent}), self.machine.active_episodes)

    def test_partial_or_missing_scope_never_starts_clear_candidate(self) -> None:
        self.start()
        partial = self.machine.apply_snapshot(
            full(2, (), complete=False, covered=frozenset({"community"}))
        )
        self.assertEqual(AlertState.STALE, partial.state)
        later = self.machine.apply_snapshot(full(3, ()))
        self.assertEqual(TransitionKind.DEGRADED, later.kind)
        self.assertEqual(AlertState.STALE, later.state)

    def test_failure_resets_candidate_and_requires_two_new_successes(self) -> None:
        self.start()
        self.machine.apply_snapshot(full(2, ()))
        self.machine.apply_failure(SourceFailure(SourceFailureKind.TIMEOUT))
        first_after_failure = self.machine.apply_snapshot(unchanged(3))
        second_after_failure = self.machine.apply_snapshot(unchanged(4))
        self.assertNotEqual(TransitionKind.ALL_CLEAR_CONFIRMED, first_after_failure.kind)
        self.assertEqual(TransitionKind.ALL_CLEAR_CONFIRMED, second_after_failure.kind)

    def test_equal_revision_is_fail_closed_and_resets_candidate(self) -> None:
        self.start()
        self.machine.apply_snapshot(full(2, ()))
        rejected = self.machine.apply_snapshot(unchanged(2))
        self.assertEqual(AlertState.STALE, rejected.state)
        self.assertNotEqual(TransitionKind.ALL_CLEAR_CONFIRMED, rejected.kind)
        retry = self.machine.apply_snapshot(unchanged(3))
        self.assertNotEqual(TransitionKind.ALL_CLEAR_CONFIRMED, retry.kind)
        confirmed = self.machine.apply_snapshot(unchanged(4))
        self.assertEqual(TransitionKind.ALL_CLEAR_CONFIRMED, confirmed.kind)

    def test_older_revision_cannot_clear_newer_active_snapshot(self) -> None:
        self.machine.apply_snapshot(full(10, (self.air,)))
        rejected = self.machine.apply_snapshot(full(9, ()))
        self.assertEqual(AlertState.STALE, rejected.state)
        self.assertNotEqual(TransitionKind.ALL_CLEAR_CONFIRMED, rejected.kind)
        self.assertEqual(frozenset({self.air}), self.machine.active_episodes)

    def test_304_without_validated_full_baseline_is_fail_closed(self) -> None:
        transition = self.machine.apply_snapshot(unchanged(1))
        self.assertEqual(AlertState.UNKNOWN, transition.state)
        self.assertEqual(TransitionKind.NONE, transition.kind)

    def test_initial_full_empty_recovers_without_all_clear_notification(self) -> None:
        transition = self.machine.apply_snapshot(full(1, ()))
        self.assertEqual(AlertState.CLEAR_CONFIRMED, transition.state)
        self.assertEqual(TransitionKind.RECOVERED, transition.kind)

    def test_active_snapshot_without_revision_is_rejected(self) -> None:
        invalid = AlertSnapshot(
            state=AlertState.ACTIVE,
            active_episodes=(ActiveEpisode(self.air, 1),),
            last_full_active_episodes=(self.air,),
            last_fetch_revision=None,
        )
        with self.assertRaises(ValueError):
            AlertStateMachine(self.scope, invalid)

    def test_restored_snapshot_deduplicates_poll_update(self) -> None:
        self.start()
        restored = AlertStateMachine(self.scope, self.machine.snapshot())
        duplicate = restored.apply_snapshot(full(1, (self.air,)))
        self.assertEqual(TransitionKind.NONE, duplicate.kind)
        self.assertEqual(AlertState.ACTIVE, restored.state)

    def test_restart_preserves_known_active_as_stale(self) -> None:
        self.start()
        transition = self.machine.apply_failure(SourceFailure(SourceFailureKind.PROCESS_RESTART))
        self.assertEqual(AlertState.STALE, transition.state)
        self.assertEqual(frozenset({self.air}), self.machine.active_episodes)
        restarted = self.machine.snapshot()
        self.assertIsNone(restarted.last_fetch_revision)
        self.assertIsNone(restarted.last_full_active_episodes)
        self.assertFalse(restarted.pending_clear_candidate)
        self.assertNotEqual(TransitionKind.ALL_CLEAR_CONFIRMED, transition.kind)

    def test_restart_from_confirmed_clear_becomes_unknown(self) -> None:
        self.machine.apply_snapshot(full(1, ()))
        transition = self.machine.apply_failure(SourceFailure(SourceFailureKind.PROCESS_RESTART))
        self.assertEqual(AlertState.UNKNOWN, transition.state)
        self.assertNotEqual(TransitionKind.ALL_CLEAR_CONFIRMED, transition.kind)

    def test_all_source_failure_types_never_confirm_clear(self) -> None:
        for index, failure_kind in enumerate(
            (
                SourceFailureKind.TIMEOUT,
                SourceFailureKind.HTTP_ERROR,
                SourceFailureKind.EMPTY_RESPONSE,
                SourceFailureKind.PARTIAL_RESPONSE,
                SourceFailureKind.MISSING_REGION,
                SourceFailureKind.EXPIRED_TTL,
                SourceFailureKind.STALE_RESPONSE,
                SourceFailureKind.INVALID_RESPONSE,
            ),
            start=1,
        ):
            with self.subTest(failure_kind=failure_kind):
                machine = AlertStateMachine(self.scope)
                machine.apply_snapshot(full(index * 10, (self.air,)))
                transition = machine.apply_failure(SourceFailure(failure_kind))
                self.assertEqual(AlertState.STALE, transition.state)
                self.assertNotEqual(TransitionKind.ALL_CLEAR_CONFIRMED, transition.kind)


if __name__ == "__main__":
    unittest.main()
