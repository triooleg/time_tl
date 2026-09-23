"""Fail-closed alerts.in.ua full-snapshot state machine."""

from __future__ import annotations

from .model import (
    ActiveEpisode,
    AlertEpisode,
    AlertSnapshot,
    AlertState,
    OfficialAlertSnapshot,
    ProviderSnapshotKind,
    RegionScope,
    SourceFailure,
    SourceFailureKind,
    Transition,
    TransitionKind,
)


class AlertStateMachine:
    """Resolve full 200 and confirming 304 responses for one region scope."""

    def __init__(
        self,
        scope: RegionScope,
        snapshot: AlertSnapshot | None = None,
    ) -> None:
        self._scope = scope
        restored = snapshot or AlertSnapshot()
        active = {item.episode: item for item in restored.active_episodes}
        if len(active) != len(restored.active_episodes):
            raise ValueError("snapshot contains duplicate active episodes")
        if any(
            item.episode.location_uid not in scope.applicable_region_ids for item in active.values()
        ):
            raise ValueError("snapshot contains episodes outside the configured scope")
        if restored.state is AlertState.ACTIVE and not active:
            raise ValueError("ACTIVE snapshot must contain active episodes")
        if restored.state in (AlertState.UNKNOWN, AlertState.CLEAR_CONFIRMED) and active:
            raise ValueError(f"{restored.state.value} snapshot cannot contain active episodes")
        if active and restored.last_fetch_revision is None:
            raise ValueError("active snapshot requires a fetch revision")
        if restored.last_fetch_revision is not None and restored.last_fetch_revision < 0:
            raise ValueError("last fetch revision must be non-negative")
        last_fetch_revision = restored.last_fetch_revision
        if last_fetch_revision is not None and any(
            item.active_revision > last_fetch_revision for item in active.values()
        ):
            raise ValueError("active episode revision exceeds snapshot revision")
        baseline = restored.last_full_active_episodes
        if baseline is not None and len(baseline) != len(set(baseline)):
            raise ValueError("full-snapshot baseline contains duplicate episodes")
        if baseline is not None and any(
            item.location_uid not in scope.applicable_region_ids for item in baseline
        ):
            raise ValueError("full-snapshot baseline contains episodes outside scope")
        if restored.state is AlertState.ACTIVE and baseline is None:
            raise ValueError("ACTIVE snapshot requires a validated full baseline")
        if restored.pending_clear_candidate:
            if restored.state is not AlertState.STALE or not active or baseline != ():
                raise ValueError("invalid pending clear candidate snapshot")

        self._state = restored.state
        self._active_episodes = active
        self._last_full_active_episodes = baseline
        self._processed_update_ids = set(restored.processed_update_ids)
        self._last_fetch_revision = restored.last_fetch_revision
        self._pending_clear_candidate = restored.pending_clear_candidate

    @property
    def state(self) -> AlertState:
        return self._state

    @property
    def active_episodes(self) -> frozenset[AlertEpisode]:
        return frozenset(self._active_episodes)

    @property
    def active_region_ids(self) -> frozenset[str]:
        return frozenset(item.location_uid for item in self._active_episodes)

    def snapshot(self) -> AlertSnapshot:
        return AlertSnapshot(
            state=self._state,
            active_episodes=tuple(
                sorted(self._active_episodes.values(), key=lambda item: item.episode)
            ),
            last_full_active_episodes=self._last_full_active_episodes,
            processed_update_ids=frozenset(self._processed_update_ids),
            last_fetch_revision=self._last_fetch_revision,
            pending_clear_candidate=self._pending_clear_candidate,
        )

    def apply_snapshot(self, update: OfficialAlertSnapshot) -> Transition:
        previous = self._state
        if update.update_id in self._processed_update_ids:
            return self._no_change(previous, update.update_id, "duplicate update")
        self._processed_update_ids.add(update.update_id)

        if (
            self._last_fetch_revision is not None
            and update.fetch_revision <= self._last_fetch_revision
        ):
            return self._degrade(
                previous,
                update.update_id,
                SourceFailureKind.NON_MONOTONIC_REVISION.value,
            )
        self._last_fetch_revision = update.fetch_revision

        coverage_ok = self._scope.applicable_region_ids.issubset(update.covered_region_ids)
        if not update.is_complete or not update.is_fresh or not coverage_ok:
            return self._degrade(
                previous,
                update.update_id,
                "snapshot is stale, partial, or missing scope regions",
            )

        if update.kind is ProviderSnapshotKind.NOT_MODIFIED_304:
            return self._apply_not_modified(update, previous)
        return self._apply_full(update, previous)

    def apply_failure(self, failure: SourceFailure) -> Transition:
        previous = self._state
        if failure.kind is SourceFailureKind.PROCESS_RESTART:
            self._state = AlertState.UNKNOWN
            self._active_episodes.clear()
            self._last_full_active_episodes = None
            self._processed_update_ids.clear()
            self._last_fetch_revision = None
            self._pending_clear_candidate = False
            kind = (
                TransitionKind.NONE if previous is AlertState.UNKNOWN else TransitionKind.DEGRADED
            )
            return Transition(
                previous_state=previous,
                state=self._state,
                kind=kind,
                reason=failure.kind.value,
            )
        return self._degrade(previous, None, failure.kind.value)

    def _apply_full(
        self,
        update: OfficialAlertSnapshot,
        previous: AlertState,
    ) -> Transition:
        relevant = tuple(
            sorted(
                item
                for item in update.active_episodes
                if item.location_uid in self._scope.applicable_region_ids
            )
        )
        self._last_full_active_episodes = relevant
        if relevant:
            was_active = bool(self._active_episodes)
            self._active_episodes = {
                episode: self._active_episodes.get(
                    episode, ActiveEpisode(episode, update.fetch_revision)
                )
                for episode in relevant
            }
            self._pending_clear_candidate = False
            self._state = AlertState.ACTIVE
            if not was_active:
                return Transition(
                    previous_state=previous,
                    state=self._state,
                    kind=TransitionKind.ALERT_STARTED,
                    event_id=update.update_id,
                )
            kind = TransitionKind.RECOVERED if previous is AlertState.STALE else TransitionKind.NONE
            return Transition(
                previous_state=previous,
                state=self._state,
                kind=kind,
                event_id=update.update_id,
                reason="full snapshot confirms active episodes",
            )
        return self._observe_empty(update.update_id, previous)

    def _apply_not_modified(
        self,
        update: OfficialAlertSnapshot,
        previous: AlertState,
    ) -> Transition:
        if self._last_full_active_episodes is None:
            return self._degrade(
                previous,
                update.update_id,
                "304 has no validated full-snapshot baseline",
            )
        if self._last_full_active_episodes:
            baseline = set(self._last_full_active_episodes)
            self._active_episodes = {
                episode: self._active_episodes.get(
                    episode, ActiveEpisode(episode, update.fetch_revision)
                )
                for episode in baseline
            }
            self._pending_clear_candidate = False
            self._state = AlertState.ACTIVE
            kind = TransitionKind.RECOVERED if previous is AlertState.STALE else TransitionKind.NONE
            return Transition(
                previous_state=previous,
                state=self._state,
                kind=kind,
                event_id=update.update_id,
                reason="304 confirms active baseline",
            )
        return self._observe_empty(update.update_id, previous)

    def _observe_empty(self, update_id: str, previous: AlertState) -> Transition:
        if self._active_episodes:
            if self._pending_clear_candidate:
                self._active_episodes.clear()
                self._pending_clear_candidate = False
                self._state = AlertState.CLEAR_CONFIRMED
                return Transition(
                    previous_state=previous,
                    state=self._state,
                    kind=TransitionKind.ALL_CLEAR_CONFIRMED,
                    event_id=update_id,
                )
            self._pending_clear_candidate = True
            self._state = AlertState.STALE
            return Transition(
                previous_state=previous,
                state=self._state,
                kind=TransitionKind.DEGRADED,
                event_id=update_id,
                reason="candidate clear awaiting a second successful poll",
            )

        self._pending_clear_candidate = False
        self._state = AlertState.CLEAR_CONFIRMED
        kind = (
            TransitionKind.NONE
            if previous is AlertState.CLEAR_CONFIRMED
            else TransitionKind.RECOVERED
        )
        return Transition(
            previous_state=previous,
            state=self._state,
            kind=kind,
            event_id=update_id,
            reason="validated baseline contains no active episodes",
        )

    def _degrade(
        self,
        previous: AlertState,
        event_id: str | None,
        reason: str,
    ) -> Transition:
        self._pending_clear_candidate = False
        if previous in (AlertState.UNKNOWN, AlertState.STALE):
            return self._no_change(previous, event_id, reason)
        self._state = AlertState.STALE
        return Transition(
            previous_state=previous,
            state=self._state,
            kind=TransitionKind.DEGRADED,
            event_id=event_id,
            reason=reason,
        )

    def _no_change(
        self,
        previous: AlertState,
        event_id: str | None = None,
        reason: str | None = None,
    ) -> Transition:
        return Transition(
            previous_state=previous,
            state=self._state,
            kind=TransitionKind.NONE,
            event_id=event_id,
            reason=reason,
        )
