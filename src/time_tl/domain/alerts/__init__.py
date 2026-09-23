"""Fail-closed alert state and notification decisions."""

from .decisions import (
    LessonContext,
    NotificationIntent,
    NotificationKind,
    NotificationMode,
    decide_alert_notifications,
    decide_lesson_reminder,
)
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
from .state_machine import AlertStateMachine

__all__ = [
    "ActiveEpisode",
    "AlertEpisode",
    "AlertSnapshot",
    "AlertState",
    "AlertStateMachine",
    "LessonContext",
    "NotificationIntent",
    "NotificationKind",
    "NotificationMode",
    "OfficialAlertSnapshot",
    "ProviderSnapshotKind",
    "RegionScope",
    "SourceFailure",
    "SourceFailureKind",
    "Transition",
    "TransitionKind",
    "decide_alert_notifications",
    "decide_lesson_reminder",
]
