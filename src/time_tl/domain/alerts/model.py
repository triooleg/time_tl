"""Value objects for the deterministic alerts.in.ua snapshot domain."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class AlertState(StrEnum):
    UNKNOWN = "UNKNOWN"
    ACTIVE = "ACTIVE"
    STALE = "STALE"
    CLEAR_CONFIRMED = "CLEAR_CONFIRMED"


class ProviderSnapshotKind(StrEnum):
    FULL_200 = "FULL_200"
    NOT_MODIFIED_304 = "NOT_MODIFIED_304"


class SourceFailureKind(StrEnum):
    TIMEOUT = "TIMEOUT"
    HTTP_ERROR = "HTTP_ERROR"
    EMPTY_RESPONSE = "EMPTY_RESPONSE"
    PARTIAL_RESPONSE = "PARTIAL_RESPONSE"
    MISSING_REGION = "MISSING_REGION"
    EXPIRED_TTL = "EXPIRED_TTL"
    STALE_RESPONSE = "STALE_RESPONSE"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    PROCESS_RESTART = "PROCESS_RESTART"
    NON_MONOTONIC_REVISION = "NON_MONOTONIC_REVISION"


class TransitionKind(StrEnum):
    NONE = "NONE"
    ALERT_STARTED = "ALERT_STARTED"
    ALL_CLEAR_CONFIRMED = "ALL_CLEAR_CONFIRMED"
    DEGRADED = "DEGRADED"
    RECOVERED = "RECOVERED"


@dataclass(frozen=True)
class RegionScope:
    selected_region_id: str
    applicable_region_ids: frozenset[str]

    def __post_init__(self) -> None:
        if not self.selected_region_id:
            raise ValueError("selected_region_id must not be empty")
        if self.selected_region_id not in self.applicable_region_ids:
            raise ValueError("selected region must be included in its scope")

    @classmethod
    def from_hierarchy(
        cls,
        selected_region_id: str,
        *,
        ancestors: tuple[str, ...] = (),
        descendants: tuple[str, ...] = (),
    ) -> RegionScope:
        return cls(
            selected_region_id=selected_region_id,
            applicable_region_ids=frozenset((selected_region_id, *ancestors, *descendants)),
        )


@dataclass(frozen=True, order=True)
class AlertEpisode:
    """Identity of one alerts.in.ua active-alert list item."""

    location_uid: str
    alert_type: str
    episode_id: str

    def __post_init__(self) -> None:
        if not self.location_uid or not self.alert_type or not self.episode_id:
            raise ValueError("episode location, alert type and ID must not be empty")


@dataclass(frozen=True)
class ActiveEpisode:
    episode: AlertEpisode
    active_revision: int

    def __post_init__(self) -> None:
        if self.active_revision < 0:
            raise ValueError("active_revision must be non-negative")


@dataclass(frozen=True)
class OfficialAlertSnapshot:
    """One validated poll result from the single alerts.in.ua poller.

    ``fetch_revision`` is a local, durably persisted monotonic counter. It is
    not derived from timestamps and is incremented for every poll response.
    A 304 carries no episode payload and refers to the last validated full 200.
    """

    update_id: str
    fetch_revision: int
    kind: ProviderSnapshotKind
    covered_region_ids: frozenset[str]
    active_episodes: tuple[AlertEpisode, ...]
    fetched_at: datetime
    is_complete: bool
    is_fresh: bool

    def __post_init__(self) -> None:
        if not self.update_id:
            raise ValueError("update_id must not be empty")
        if self.fetch_revision < 0:
            raise ValueError("fetch_revision must be non-negative")
        if self.fetched_at.tzinfo is None:
            raise ValueError("fetched_at must be timezone-aware")
        if len(self.active_episodes) != len(set(self.active_episodes)):
            raise ValueError("snapshot contains duplicate alert episodes")
        if self.kind is ProviderSnapshotKind.NOT_MODIFIED_304 and self.active_episodes:
            raise ValueError("304 snapshot must not carry active episodes")


@dataclass(frozen=True)
class SourceFailure:
    kind: SourceFailureKind


@dataclass(frozen=True)
class AlertSnapshot:
    """Serializable durable machine state for restart-safe restoration."""

    state: AlertState = AlertState.UNKNOWN
    active_episodes: tuple[ActiveEpisode, ...] = ()
    last_full_active_episodes: tuple[AlertEpisode, ...] | None = None
    processed_update_ids: frozenset[str] = field(default_factory=frozenset)
    last_fetch_revision: int | None = None
    pending_clear_candidate: bool = False


@dataclass(frozen=True)
class Transition:
    previous_state: AlertState
    state: AlertState
    kind: TransitionKind
    event_id: str | None = None
    reason: str | None = None

    @property
    def changed(self) -> bool:
        return self.kind is not TransitionKind.NONE
