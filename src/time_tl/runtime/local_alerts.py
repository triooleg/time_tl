"""Durable, process-local simulation alert repository.

This adapter exists only for LOCAL/SIMULATION. It is not a substitute for the
transactional PostgreSQL repository required by staging and production.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterable
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from time_tl.domain.alerts import (
    ActiveEpisode,
    AlertEpisode,
    AlertSnapshot,
    AlertState,
    AlertStateMachine,
    OfficialAlertSnapshot,
    RegionScope,
    SourceFailure,
    SourceFailureKind,
    TransitionKind,
)
from time_tl.infrastructure.alerts.repositories import AlertOutboxItem, AlertPollState


class JsonAlertRepository:
    def __init__(self, path: str | Path, scopes: Iterable[tuple[str, RegionScope]]) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()
        self._scopes = dict(scopes)
        self._snapshots: dict[str, AlertSnapshot] = {}
        self._poll_state = AlertPollState()
        self._outbox: dict[str, AlertOutboxItem] = {}
        self._load()

    async def poll_state(self) -> AlertPollState:
        async with self._lock:
            return self._poll_state

    async def set_poll_state(self, state: AlertPollState) -> None:
        async with self._lock:
            self._poll_state = state
            self._persist()

    async def apply_snapshot_and_poll_state_atomically(
        self, update: OfficialAlertSnapshot, poll_state: AlertPollState
    ) -> tuple[AlertOutboxItem, ...]:
        created: list[AlertOutboxItem] = []
        async with self._lock:
            if poll_state.fetch_revision != update.fetch_revision:
                raise ValueError("poll state and snapshot revisions must match")
            if poll_state.fetch_revision < self._poll_state.fetch_revision:
                raise ValueError("fetch revision cannot move backwards")
            staged: dict[str, AlertSnapshot] = {}
            staged_outbox: dict[str, AlertOutboxItem] = {}
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
                staged = {}
                failure = SourceFailure(SourceFailureKind.PROCESS_RESTART)
                for scope_id, scope in self._scopes.items():
                    machine = AlertStateMachine(scope, self._snapshots.get(scope_id))
                    machine.apply_failure(failure)
                    staged[scope_id] = machine.snapshot()
                staged_outbox = {}
                created = []
            next_snapshots = {**self._snapshots, **staged}
            next_outbox = {**self._outbox, **staged_outbox}
            next_poll_state = self._poll_state if rejected else poll_state
            self._persist_values(next_poll_state, next_snapshots, next_outbox)
            self._snapshots = next_snapshots
            self._outbox = next_outbox
            self._poll_state = next_poll_state
        return tuple(created)

    async def apply_failure_atomically(self, failure: SourceFailure) -> None:
        async with self._lock:
            staged: dict[str, AlertSnapshot] = {}
            for scope_id, scope in self._scopes.items():
                machine = AlertStateMachine(scope, self._snapshots.get(scope_id))
                machine.apply_failure(failure)
                staged[scope_id] = machine.snapshot()
            self._snapshots.update(staged)
            self._persist()

    async def apply_failure_and_poll_state_atomically(
        self, failure: SourceFailure, poll_state: AlertPollState
    ) -> None:
        async with self._lock:
            if poll_state.fetch_revision != self._poll_state.fetch_revision + 1:
                raise ValueError("fetch revision must be the exact next durable revision")
            staged: dict[str, AlertSnapshot] = {}
            for scope_id, scope in self._scopes.items():
                machine = AlertStateMachine(scope, self._snapshots.get(scope_id))
                machine.apply_failure(failure)
                staged[scope_id] = machine.snapshot()
            next_snapshots = {**self._snapshots, **staged}
            self._persist_values(poll_state, next_snapshots, self._outbox)
            self._snapshots = next_snapshots
            self._poll_state = poll_state

    async def apply_process_restart_atomically(self) -> None:
        async with self._lock:
            failure = SourceFailure(SourceFailureKind.PROCESS_RESTART)
            staged: dict[str, AlertSnapshot] = {}
            for scope_id, scope in self._scopes.items():
                machine = AlertStateMachine(scope, self._snapshots.get(scope_id))
                machine.apply_failure(failure)
                staged[scope_id] = machine.snapshot()
            next_snapshots = {**self._snapshots, **staged}
            next_poll_state = replace(
                self._poll_state,
                fetch_revision=self._poll_state.fetch_revision + 1,
                last_modified=None,
            )
            self._persist_values(next_poll_state, next_snapshots, self._outbox)
            self._snapshots = next_snapshots
            self._poll_state = next_poll_state

    async def snapshot_for(self, scope_id: str) -> AlertSnapshot | None:
        async with self._lock:
            return self._snapshots.get(scope_id)

    async def outbox(self) -> tuple[AlertOutboxItem, ...]:
        async with self._lock:
            return tuple(self._outbox.values())

    async def resume_operator_circuit(self) -> None:
        async with self._lock:
            self._poll_state = replace(self._poll_state, circuit_open=False)
            self._persist()

    def _load(self) -> None:
        if not self._path.exists():
            return
        data = json.loads(self._path.read_text(encoding="utf-8"))
        poll = data.get("poll_state", {})
        self._poll_state = AlertPollState(
            fetch_revision=int(poll.get("fetch_revision", 0)),
            last_modified=poll.get("last_modified"),
            circuit_open=bool(poll.get("circuit_open", False)),
            retry_not_before_monotonic=0.0,
            consecutive_failures=int(poll.get("consecutive_failures", 0)),
        )
        for scope_id, raw in data.get("snapshots", {}).items():
            if scope_id in self._scopes:
                self._snapshots[scope_id] = _decode_snapshot(raw)
        for raw in data.get("outbox", []):
            item = AlertOutboxItem(
                raw["event_key"], raw["scope_id"], TransitionKind(raw["transition_kind"])
            )
            self._outbox[item.event_key] = item

    def _persist(self) -> None:
        self._persist_values(self._poll_state, self._snapshots, self._outbox)

    def _persist_values(
        self,
        poll_state: AlertPollState,
        snapshots: dict[str, AlertSnapshot],
        outbox: dict[str, AlertOutboxItem],
    ) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "poll_state": asdict(poll_state),
            "snapshots": {key: _encode_snapshot(value) for key, value in snapshots.items()},
            "outbox": [
                {
                    "event_key": item.event_key,
                    "scope_id": item.scope_id,
                    "transition_kind": item.transition_kind.value,
                }
                for item in outbox.values()
            ],
        }
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, self._path)


def _episode(item: AlertEpisode) -> dict[str, str]:
    return {
        "location_uid": item.location_uid,
        "alert_type": item.alert_type,
        "episode_id": item.episode_id,
    }


def _decode_episode(raw: dict[str, str]) -> AlertEpisode:
    return AlertEpisode(raw["location_uid"], raw["alert_type"], raw["episode_id"])


def _encode_snapshot(snapshot: AlertSnapshot) -> dict[str, object]:
    return {
        "state": snapshot.state.value,
        "active_episodes": [
            {"episode": _episode(item.episode), "active_revision": item.active_revision}
            for item in snapshot.active_episodes
        ],
        "last_full_active_episodes": (
            None
            if snapshot.last_full_active_episodes is None
            else [_episode(item) for item in snapshot.last_full_active_episodes]
        ),
        "processed_update_ids": sorted(snapshot.processed_update_ids),
        "last_fetch_revision": snapshot.last_fetch_revision,
        "pending_clear_candidate": snapshot.pending_clear_candidate,
    }


def _decode_snapshot(raw: dict[str, Any]) -> AlertSnapshot:
    active = tuple(
        ActiveEpisode(_decode_episode(item["episode"]), int(item["active_revision"]))
        for item in raw.get("active_episodes", [])
    )
    baseline_raw = raw.get("last_full_active_episodes")
    baseline = (
        None if baseline_raw is None else tuple(_decode_episode(item) for item in baseline_raw)
    )
    return AlertSnapshot(
        state=AlertState(str(raw.get("state", "UNKNOWN"))),
        active_episodes=active,
        last_full_active_episodes=baseline,
        processed_update_ids=frozenset(raw.get("processed_update_ids", [])),
        last_fetch_revision=raw.get("last_fetch_revision"),
        pending_clear_candidate=bool(raw.get("pending_clear_candidate", False)),
    )
