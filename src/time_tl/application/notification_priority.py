"""Transport priority only; does not change alert state or delivery authority."""

AIR_EVENTS = frozenset({"ALERT_STARTED", "ALL_CLEAR_CONFIRMED"})
LESSON_EVENTS = frozenset({"RETURN_TO_ACTIVE_LESSON", "LESSON_REMINDER"})


def notification_priority(event_type: str) -> int:
    if event_type in AIR_EVENTS:
        return 0
    if event_type in LESSON_EVENTS:
        return 1
    return 3 if event_type.startswith("WEATHER_") else 2
