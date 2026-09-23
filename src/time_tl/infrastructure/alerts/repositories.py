"""Atomic snapshot/outbox repository ports and deterministic in-memory adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Protocol

from time_tl.domain.alerts import (
    AlertSnapshot,
    AlertStateMachine,
    OfficialAlertSnapshot,
    RegionScope,
    SourceFailure,
    SourceFailureKind,
    TransitionKind,
)


@dataclass(frozen=True)
class AlertPollState:
    fetch_revision: int = 0
    last_modified: str | None = None
    circuit_open: bool = False
    retry_not_before_monotonic: float = 0.0
    consecutive_failures: int = 0


@dataclass(frozen=True)
class AlertOutboxItem:
    event_key: str
    scope_id: str
    transition_kind: TransitionKind


class AlertRepository(Protocol):
    async def poll_state(self) -> AlertPollState: ...
    async def set_poll_state(self, state: AlertPollState) -> None: ...
    async def apply_snapshot_and_poll_state_atomically(
        self, update: OfficialAlertSnapshot, poll_state: AlertPollState
    ) -> tuple[AlertOutboxItem, ...]: ...
    async def apply_failure_atomically(self, failure: SourceFailure) -> None: ...


class InMemoryAlertRepository:
    """Shared instance models durable state across worker object restarts."""

    def __init__(self, scopes: Iterable[tuple[str, RegionScope]]) -> None:
        self._lock = asyncio.Lock()
        self._scopes = dict(scopes)
        self._snapshots: dict[str, AlertSnapshot] = {}
        self._poll_state = AlertPollState()
        self._outbox: dict[str, AlertOutboxItem] = {}

    async def poll_state(self) -> AlertPollState:
        async with self._lock:
            return self._poll_state

    async def set_poll_state(self, state: AlertPollState) -> None:
        async with self._lock:
            self._poll_state = state

    async def apply_snapshot_and_poll_state_atomically(
        self, update: OfficialAlertSnapshot, poll_state: AlertPollState
    ) -> tuple[AlertOutboxItem, ...]:
        created: list[AlertOutboxItem] = []
        async with self._lock:
            self._validate_success_poll_state(update, poll_state)
            staged, staged_outbox = {}, {}
            rejected = False
            for scope_id, scope in self._scopes.items():
                machine = AlertStateMachine(scope, self._snapshots.get(scope_id))
                transition = machine.apply_snapshot(update)
                staged[scope_id] = machine.snapshot()
                rejected = rejected or (
                    transition.reason == SourceFailureKind.NON_MONOTONIC_REVISION.value
                )
                if transition.kind in (
                    TransitionKind.ALERT_STARTED,
                    TransitionKind.ALL_CLEAR_CONFIRMED,
                ):
                    key = f"{scope_id}:{transition.kind.value}:{transition.event_id}"
                    if key not in self._outbox and key not in staged_outbox:
                        item = AlertOutboxItem(key, scope_id, transition.kind)
                        staged_outbox[key] = item
                        created.append(item)
            if rejected:
                staged = self._restart_safe_snapshots()
                staged_outbox = {}
                created = []
            self._snapshots.update(staged)
            self._outbox.update(staged_outbox)
            if not rejected:
                self._poll_state = poll_state
        return tuple(created)

    def _restart_safe_snapshots(self) -> dict[str, AlertSnapshot]:
        staged: dict[str, AlertSnapshot] = {}
        failure = SourceFailure(SourceFailureKind.PROCESS_RESTART)
        for scope_id, scope in self._scopes.items():
            machine = AlertStateMachine(scope, self._snapshots.get(scope_id))
            machine.apply_failure(failure)
            staged[scope_id] = machine.snapshot()
        return staged

    def _validate_success_poll_state(
        self, update: OfficialAlertSnapshot, poll_state: AlertPollState
    ) -> None:
        if poll_state.fetch_revision != update.fetch_revision:
            raise ValueError("poll state and snapshot revisions must match")
        if poll_state.fetch_revision < self._poll_state.fetch_revision:
            raise ValueError("fetch revision cannot move backwards")

    async def apply_failure_atomically(self, failure: SourceFailure) -> None:
        async with self._lock:
            staged = {}
            for scope_id, scope in self._scopes.items():
                machine = AlertStateMachine(scope, self._snapshots.get(scope_id))
                machine.apply_failure(failure)
                staged[scope_id] = machine.snapshot()
            self._snapshots.update(staged)

    async def snapshot_for(self, scope_id: str) -> AlertSnapshot | None:
        async with self._lock:
            return self._snapshots.get(scope_id)

    async def outbox(self) -> tuple[AlertOutboxItem, ...]:
        async with self._lock:
            return tuple(self._outbox.values())

    async def resume_operator_circuit(self) -> None:
        async with self._lock:
            self._poll_state = replace(self._poll_state, circuit_open=False)
