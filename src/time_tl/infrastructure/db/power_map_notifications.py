"""Head9 map-only reminders, durable dedupe and locked final authorization.

Only cached public plans are consumed. No HTTP, Telegram or official-power
tables are accessed. The caller owns global release/allowlist authorization.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import String, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from time_tl.application.power_map_messages import (
    render_map_emergency,
    render_map_reminder,
    render_map_schedule_change,
    render_map_status,
)
from time_tl.domain.power_outages.map_reminders import (
    OUTAGE,
    RESTORATION,
    MapPowerReminder,
    map_reminder_evidence_hashes,
    plan_map_reminders,
    usable_map_day,
)
from time_tl.domain.power_outages.models import (
    PowerDaySchedule,
    PowerInterval,
    PowerProvider,
    PowerPublicationStatus,
    PowerScheduleView,
    PowerSourceKind,
    PowerState,
    PowerSubscription,
    ScheduleKind,
    SourceStatus,
    aware,
    validate_group,
)
from time_tl.domain.power_outages.normalization import KYIV, day_bounds
from time_tl.infrastructure.power_outages.alerts_energy_api_normalizer import (
    KNOWN_BINDINGS,
    normalize_cached_api_day,
)
from time_tl.runtime.power_map import PowerMapScheduleReader

from .models import OutboxMessage, RuntimeSetting, User
from .power_map import REGION_PREFIX, PostgresPowerMapRepository, PowerMapView
from .power_map_preferences import PREFIX, decode, preference_key

CHANGED = "POWER_MAP_SCHEDULE_CHANGED"
EMERGENCY_STARTED = "POWER_MAP_EMERGENCY_STARTED"
EMERGENCY_ENDED = "POWER_MAP_EMERGENCY_ENDED"
EMERGENCY_EVENTS = frozenset({EMERGENCY_STARTED, EMERGENCY_ENDED})
STATUS_CHANGED = "POWER_MAP_STATUS_CHANGED"
STATUSES = frozenset({"ScheduleApplies", "NoShutdowns", "WaitingForSchedule", "EmergencyShutdowns"})
EVENT_TYPES = frozenset({OUTAGE, RESTORATION, CHANGED, STATUS_CHANGED}) | EMERGENCY_EVENTS
EMERGENCY_STATE_PREFIX = "power_api_emergency_state:"
STATUS_STATE_PREFIX = "power_api_status_state:"
STATE_PREFIX = "power_api_reminder_state:"
_CREDENTIAL_KEY = "power_alerts_energy_api_credentials"
_COMMON = {
    "version",
    "source_kind",
    "provider",
    "group",
    "preference_revision",
    "credential_revision",
    "valid_until",
}


def reminder_state_key(user_id: str) -> str:
    return STATE_PREFIX + str(UUID(user_id))


def _hash(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _stamp(value: object) -> datetime:
    if type(value) is not str or len(value) > 40:
        raise ValueError("invalid_map_notification_time")
    return aware(datetime.fromisoformat(value))


def _expiry(event: MapPowerReminder) -> datetime:
    return aware(event.expires_at)


def _day_wire(day: PowerDaySchedule) -> dict[str, object]:
    return {
        "provider": day.provider.value,
        "group": day.group_id,
        "date": day.local_date.isoformat(),
        "content_hash": day.content_hash,
        "intervals": [
            [aware(i.starts_at).isoformat(), aware(i.ends_at).isoformat(), i.state.value]
            for i in day.intervals
        ],
    }


def _read_day(value: object) -> PowerDaySchedule:
    if (
        type(value) is not dict
        or set(value) != {"provider", "group", "date", "content_hash", "intervals"}
        or any(type(value[key]) is not str for key in ("provider", "group", "date", "content_hash"))
        or type(value["intervals"]) is not list
        or not 1 <= len(value["intervals"]) <= 192
    ):
        raise ValueError("invalid_map_notification_day")
    if any(type(item) is not list or len(item) != 3 for item in value["intervals"]):
        raise ValueError("invalid_map_notification_day")
    day = PowerDaySchedule(
        PowerProvider(value["provider"]),
        value["group"],
        date.fromisoformat(value["date"]),
        ScheduleKind.CONFIRMED_DAY,
        tuple(
            PowerInterval(value["group"], _stamp(i[0]), _stamp(i[1]), PowerState(i[2]))
            for i in value["intervals"]
        ),
    )
    if day.content_hash != value["content_hash"]:
        raise ValueError("invalid_map_notification_day_hash")
    return day


def _payload(payload: object, event_type: str) -> dict[str, Any]:
    extra = {"day"} if event_type == CHANGED else {"event"}
    if event_type in EMERGENCY_EVENTS:
        extra = {"transition_at"}
    if event_type == STATUS_CHANGED:
        extra = {"status_day", "status", "transition_at"}
    if (
        type(payload) is not dict
        or event_type not in EVENT_TYPES
        or set(payload) != _COMMON | extra
        or payload["source_kind"] != "alerts_energy_api"
        or type(payload["version"]) is not int
        or payload["version"] != 1
        or any(
            type(payload[k]) is not int or not 1 <= payload[k] < 2**31
            for k in ("preference_revision", "credential_revision")
        )
    ):
        raise ValueError("invalid_map_notification_payload")
    if type(payload["provider"]) is not str or type(payload["group"]) is not str:
        raise ValueError("invalid_map_notification_payload")
    provider = PowerProvider(payload["provider"])
    if provider not in KNOWN_BINDINGS:
        raise ValueError("unsupported_map_notification_provider")
    _stamp(payload["valid_until"])
    if event_type == STATUS_CHANGED:
        validate_group(payload["group"])
        if type(payload["status"]) is not str or payload["status"] not in STATUSES:
            raise ValueError("invalid_map_status")
        if type(payload["status_day"]) is not str or date.fromisoformat(payload["status_day"]).isoformat() != payload["status_day"]:
            raise ValueError("invalid_map_status_day")
        transition = _stamp(payload["transition_at"])
        if not transition < _stamp(payload["valid_until"]) <= transition + timedelta(minutes=10):
            raise ValueError("invalid_map_status_deadline")
        return payload
    if event_type in EMERGENCY_EVENTS:
        validate_group(payload["group"])
        transition = _stamp(payload["transition_at"])
        if not transition < _stamp(payload["valid_until"]) <= transition + timedelta(minutes=10):
            raise ValueError("invalid_map_emergency_deadline")
        return payload
    if event_type == CHANGED:
        day = _read_day(payload["day"])
        identity = (day.provider.value, day.group_id)
        if day.publication_status is not PowerPublicationStatus.PLANNED:
            raise ValueError("invalid_map_notification_day")
    else:
        event = MapPowerReminder.from_wire(payload["event"])
        if event.event_type != event_type or _stamp(payload["valid_until"]) != _expiry(event):
            raise ValueError("invalid_map_notification_event")
        identity = (event.provider.value, event.group_id)
    if identity != (payload["provider"], payload["group"]):
        raise ValueError("map_notification_identity_mismatch")
    return payload


def render_map_outbox(event_type: str, payload: dict[str, object], now: datetime) -> str:
    """Final policy must revalidate before sending; renderer independently rejects expiry."""
    # Claiming injects a recipient from the locked User. It is transport-only,
    # never part of the persisted authority or semantic payload.
    value = _payload(
        {key: value for key, value in payload.items() if key != "telegram_chat_id"}, event_type
    )
    if aware(now) >= _stamp(value["valid_until"]):
        raise ValueError("map_notification_expired")
    if event_type in EMERGENCY_EVENTS:
        return render_map_emergency(
            PowerProvider(value["provider"]), value["group"], event_type == EMERGENCY_STARTED
        )
    if event_type == STATUS_CHANGED:
        return render_map_status(PowerProvider(value["provider"]), value["group"],
                                 date.fromisoformat(value["status_day"]), value["status"])
    if event_type != CHANGED:
        return render_map_reminder(MapPowerReminder.from_wire(value["event"]), now)
    day = _read_day(value["day"])
    view = PowerScheduleView(
        day.provider,
        day.group_id,
        SourceStatus.FRESH,
        aware(now),
        aware(now),
        day,
        source_kind=PowerSourceKind.ALERTS_ENERGY_API,
    )
    return render_map_schedule_change(view, now)


@dataclass(frozen=True)
class MapDeliveryFence:
    valid_until: datetime
    freshness_deadlines: tuple[datetime, ...]
    authorization_at: datetime | None = None

    def valid_at(self, now: datetime) -> bool:
        clock = aware(now)
        return (
            clock < aware(self.valid_until)
            and (self.authorization_at is None or clock >= aware(self.authorization_at))
            and bool(self.freshness_deadlines)
            and all(clock <= aware(deadline) for deadline in self.freshness_deadlines)
        )


class _LockedCache:
    def __init__(self, value: PowerMapView, ttl: int) -> None:
        self.value, self.stale_after_seconds = value, ttl

    async def read_region(self, slug: str, now: datetime) -> PowerMapView:
        if slug != self.value.area_slug:
            raise ValueError("map_notification_region_mismatch")
        return self.value


@dataclass(frozen=True)
class _Context:
    user: User
    subscription: PowerSubscription
    views: tuple[PowerScheduleView, ...]
    map_view: PowerMapView
    credential_revision: int
    ttl: int
    emergency_view: PowerScheduleView | None = None

    def deadlines(self, dates: set[date]) -> tuple[datetime, ...]:
        data = self.map_view.data
        if data is None:
            return ()
        supplier = KNOWN_BINDINGS[self.subscription.provider][1]
        checks = [
            data.record_checked_at(supplier, self.subscription.group_id, day=day)
            for day in sorted(dates)
        ]
        if not checks or any(check is None for check in checks):
            return ()
        return tuple(
            [aware(data.fetched_at) + timedelta(seconds=self.ttl)]
            + [aware(check) + timedelta(seconds=self.ttl) for check in checks if check is not None]
        )


async def _context(
    session: AsyncSession, user_id: UUID, now: datetime, ttl: int, *, lock: bool
) -> _Context | None:
    user = await session.get(User, user_id, with_for_update=True if lock else None)
    if user is None or type(user.telegram_user_id) is not int or user.telegram_user_id <= 0:
        return None
    pref = await session.get(
        RuntimeSetting, preference_key(str(user_id)), with_for_update=True if lock else None
    )
    if pref is None:
        return None
    subscription = decode(pref.value, str(user_id))
    if not subscription.enabled:
        return None
    # Follow cache publisher order: credential before region. No encrypted value
    # is loaded or included in diagnostics/payloads.
    query = select(RuntimeSetting.value["revision"], RuntimeSetting.value["check_status"]).where(
        RuntimeSetting.key == _CREDENTIAL_KEY
    )
    if lock:
        query = query.with_for_update()
    credential = (await session.execute(query)).one_or_none()
    if (
        credential is None
        or type(credential[0]) is not int
        or not 1 <= credential[0] < 2**31
        or credential[1] != "VALID"
    ):
        return None
    slug = KNOWN_BINDINGS[subscription.provider][0]
    row = await session.get(
        RuntimeSetting, REGION_PREFIX + slug, with_for_update=True if lock else None
    )
    repository = PostgresPowerMapRepository(None, stale_after_seconds=ttl)  # type: ignore[arg-type]
    view = repository._view(slug, row, aware(now))
    reader = PowerMapScheduleReader(_LockedCache(view, ttl), stale_after_seconds=ttl)
    today = aware(now).astimezone(KYIV).date()
    views = tuple(
        [
            await reader.read_schedule(
                subscription.provider,
                subscription.group_id,
                today + timedelta(days=offset),
                schedule_type=ScheduleKind.CONFIRMED_DAY,
                now=now,
            )
            for offset in (-1, 0, 1)
        ]
    )
    emergency_view = await reader.read_schedule(
        subscription.provider,
        subscription.group_id,
        today,
        schedule_type=ScheduleKind.CONFIRMED_DAY,
        now=now,
        display_only=True,
    )
    return _Context(user, subscription, views, view, credential[0], ttl, emergency_view)


@dataclass(frozen=True)
class _Cursor:
    days: dict[str, str | None]
    generation: int
    pending_changes: dict[str, str]


def _cursor(value: object, subscription: PowerSubscription) -> _Cursor | None:
    if value is None:
        return None
    if (
        type(value) is not dict
        or set(value)
        != {"version", "preference_revision", "provider", "group", "days"}
        | ({"generation", "pending_changes"} if value.get("version") == 2 else set())
        or type(value["version"]) is not int
        or value["version"] not in {1, 2}
        or type(value["preference_revision"]) is not int
        or type(value["days"]) is not dict
        or len(value["days"]) > 2
    ):
        raise ValueError("invalid_map_notification_cursor")
    if (value["preference_revision"], value["provider"], value["group"]) != (
        subscription.revision,
        subscription.provider.value,
        subscription.group_id,
    ):
        return None
    for day, fingerprint in value["days"].items():
        if (
            type(day) is not str
            or date.fromisoformat(day).isoformat() != day
            or fingerprint is not None
            and (type(fingerprint) is not str or re.fullmatch(r"[a-f0-9]{64}", fingerprint) is None)
        ):
            raise ValueError("invalid_map_notification_cursor")
    generation = value.get("generation", 0)
    pending = value.get("pending_changes", {})
    if (
        type(generation) is not int
        or not 0 <= generation < 2**63 - 2
        or type(pending) is not dict
        or not set(pending) <= set(value["days"])
        or any(
            type(key) is not str or re.fullmatch(r"power-map:[a-f0-9]{64}", key) is None
            for key in pending.values()
        )
    ):
        raise ValueError("invalid_map_notification_cursor")
    return _Cursor(dict(value["days"]), generation, dict(pending))


def _identity_payload(context: _Context, valid_until: datetime) -> dict[str, Any]:
    sub = context.subscription
    return {
        "version": 1,
        "source_kind": "alerts_energy_api",
        "provider": sub.provider.value,
        "group": sub.group_id,
        "preference_revision": sub.revision,
        "credential_revision": context.credential_revision,
        "valid_until": aware(valid_until).isoformat(),
    }


def _event_allowed(sub: PowerSubscription, event_type: str) -> bool:
    return sub.enabled and (
        event_type in {CHANGED, STATUS_CHANGED}
        or event_type in {OUTAGE, EMERGENCY_STARTED}
        and sub.outage_notifications_enabled
        or event_type in {RESTORATION, EMERGENCY_ENDED}
        and sub.restoration_notifications_enabled
    )


def _emergency_observation(context: _Context, now: datetime) -> tuple[bool, datetime] | None:
    day = aware(now).astimezone(KYIV).date()
    data = context.map_view.data
    if data is None:
        return None
    view = context.emergency_view
    if view is None or view.source_status is not SourceStatus.FRESH:
        return None
    schedule = view.schedule
    if (
        schedule is None
        or schedule.local_date != day
        or schedule.publication_status
        not in {PowerPublicationStatus.EMERGENCY, PowerPublicationStatus.PLANNED}
    ):
        return None
    supplier = KNOWN_BINDINGS[context.subscription.provider][1]
    checked = data.record_checked_at(supplier, context.subscription.group_id, day=day)
    if checked is None or any(
        not 0 <= (aware(now) - aware(stamp)).total_seconds() <= context.ttl
        for stamp in (data.fetched_at, checked)
    ):
        return None
    return schedule.publication_status is PowerPublicationStatus.EMERGENCY, aware(checked)


def _emergency_identity(context: _Context) -> list[str]:
    sub = context.subscription
    return [sub.provider.value, sub.group_id]


def _status_observations(context: _Context, now: datetime) -> dict[str, tuple[str, datetime]]:
    data = context.map_view.data
    if (data is None or not getattr(context.map_view, "last_fetch_succeeded", False)
            or context.map_view.source_status in {SourceStatus.INVALID, SourceStatus.UNAVAILABLE}
            or context.map_view.error_code not in {None, "MAP_UNVERIFIED_OR_STALE"}
            or not 0 <= (aware(now) - aware(data.fetched_at)).total_seconds() <= context.ttl):
        return {}
    slug, supplier = KNOWN_BINDINGS[context.subscription.provider]
    row = next((item for item in data.to_wire()["rows"]
                if item["initiator"] == supplier and item["queue"] == context.subscription.group_id), None)
    if row is None:
        return {}
    today = aware(now).astimezone(KYIV).date()
    result = {}
    for prefix in ("today", "tomorrow"):
        day = date.fromisoformat(row[prefix + "_date"])
        checked = data.record_checked_at(supplier, context.subscription.group_id, day=day)
        if (day not in {today, today + timedelta(days=1)} or checked is None
                or not 0 <= (aware(now) - aware(checked)).total_seconds() <= context.ttl):
            continue
        projected = normalize_cached_api_day(
            row, provider=context.subscription.provider, area_slug=slug, day=day,
            fetched_at=data.fetched_at, stale_after_seconds=context.ttl,
        )
        if projected.publication_status is PowerPublicationStatus.UNKNOWN:
            continue
        status = row[prefix + "_status"]
        if status in STATUSES:
            result[day.isoformat()] = status, aware(checked)
    return result


def _read_status_state(row: RuntimeSetting | None, context: _Context) -> dict[str, Any]:
    if row is None:
        return {}
    if type(row.value) is not dict:
        raise ValueError("invalid_map_status_state")
    if row.value.get("identity") != _emergency_identity(context):
        return {}
    days = row.value["days"]
    if type(days) is not dict or len(days) > 2:
        raise ValueError("invalid_map_status_state")
    for day, value in days.items():
        if (date.fromisoformat(day).isoformat() != day or type(value) is not dict
                or set(value) not in ({"status", "checked_at", "transition_at"},
                                      {"status", "checked_at", "transition_at", "fetched_at"})
                or value["status"] not in STATUSES):
            raise ValueError("invalid_map_status_state")
        _stamp(value["checked_at"])
        _stamp(value["transition_at"])
        if "fetched_at" in value:
            _stamp(value["fetched_at"])
    return dict(days)


async def _plan_status_changes(session: AsyncSession, context: _Context, now: datetime) -> list[tuple[str, str, dict[str, Any]]]:
    observed = _status_observations(context, now)
    if not observed:
        return []
    key = STATUS_STATE_PREFIX + str(context.user.id)
    row = await session.get(RuntimeSetting, key, with_for_update=True)
    previous = _read_status_state(row, context)
    today = aware(now).astimezone(KYIV).date()
    days = {day: value for day, value in previous.items()
            if date.fromisoformat(day) in {today, today + timedelta(days=1)}}
    events = []
    fetched = aware(context.map_view.data.fetched_at)
    for day, (status, checked) in observed.items():
        old = previous.get(day)
        if old is not None:
            old_checked = _stamp(old["checked_at"])
            old_fetched = _stamp(old.get("fetched_at", old["checked_at"]))
            if checked < old_checked or fetched <= old_fetched:
                continue
        changed = old is not None and status != old["status"]
        transition = now if changed or old is None else _stamp(old["transition_at"])
        days[day] = {"status": status, "checked_at": checked.isoformat(), "fetched_at": fetched.isoformat(),
                     "transition_at": transition.isoformat()}
        if changed and _event_allowed(context.subscription, STATUS_CHANGED):
            deadline = min(now + timedelta(minutes=10), day_bounds(date.fromisoformat(day))[1])
            if deadline <= now:
                continue
            payload = _identity_payload(context, deadline)
            payload.update(status_day=day, status=status, transition_at=transition.isoformat())
            events.append((STATUS_CHANGED, _hash([*_emergency_identity(context), day,
                                                  status, transition.isoformat()]), payload))
    value = {"identity": _emergency_identity(context), "days": days}
    if row is None:
        session.add(RuntimeSetting(key=key, value=value, updated_by="power-map-reminders"))
    else:
        row.value, row.updated_at = value, now
    return events


def _read_emergency_state(
    row: RuntimeSetting | None, context: _Context
) -> tuple[bool, datetime, datetime] | None:
    if row is None:
        return None
    value = row.value
    if type(value) is not dict or value.get("identity") != _emergency_identity(context):
        return None
    if type(value.get("active")) is not bool:
        raise ValueError("invalid_map_emergency_state")
    return value["active"], _stamp(value["checked_at"]), _stamp(value["transition_at"])


async def _plan_emergency(
    session: AsyncSession, context: _Context, now: datetime
) -> tuple[str, str, dict[str, Any]] | None:
    observed = _emergency_observation(context, now)
    if observed is None:
        return None
    active, checked = observed
    key = EMERGENCY_STATE_PREFIX + str(context.user.id)
    row = await session.get(RuntimeSetting, key, with_for_update=True)
    previous = _read_emergency_state(row, context)
    if previous is not None and checked <= previous[1]:
        return None
    changed = active if previous is None else active != previous[0]
    transition = now if changed or previous is None else previous[2]
    value = {
        "identity": _emergency_identity(context),
        "active": active,
        "checked_at": checked.isoformat(),
        "transition_at": transition.isoformat(),
    }
    if row is None:
        session.add(RuntimeSetting(key=key, value=value, updated_by="power-map-reminders"))
    else:
        row.value, row.updated_at = value, now
    kind = EMERGENCY_STARTED if active else EMERGENCY_ENDED
    deadline = min(now + timedelta(minutes=10), day_bounds(now.astimezone(KYIV).date())[1])
    if not changed or deadline <= now or not _event_allowed(context.subscription, kind):
        return None
    payload = _identity_payload(context, deadline)
    payload["transition_at"] = transition.isoformat()
    return (
        kind,
        _hash(
            [
                context.subscription.provider.value,
                context.subscription.group_id,
                transition.isoformat(),
            ]
        ),
        payload,
    )


def _same_claims(stored: MapPowerReminder, current: MapPowerReminder) -> bool:
    """Permit newly available future detail without rewriting an admitted message.

    The queued message can remain conservative. Every time/confirmation it does
    claim must still agree with the freshly normalized event. Unknown next-cut
    information is not a promise that no next cut exists.
    """
    fields = (
        "provider",
        "group_id",
        "event_type",
        "transition_at",
        "planned_send_at",
        "outage_starts_at",
        "outage_ends_at",
        "outage_start_confirmed",
        "restoration_confirmed",
        "transition_key",
    )
    if any(getattr(stored, field) != getattr(current, field) for field in fields):
        return False
    if (
        stored.next_outage_starts_at is not None
        and stored.next_outage_starts_at != current.next_outage_starts_at
    ):
        return False
    if (
        stored.next_outage_ends_at is not None
        and stored.next_outage_ends_at != current.next_outage_ends_at
    ):
        return False
    return True


async def map_delivery_policy(
    session: AsyncSession,
    row: OutboxMessage,
    now: datetime,
    *,
    stale_after_seconds: int = 1800,
    lock: bool = False,
    expected_recipient: Any = None,
    fences: list[MapDeliveryFence] | None = None,
) -> str:
    """Fail closed unless an exact user-bound cached event is still valid.

    Caller must hold the outbox transaction through bounded I/O when lock=True.
    Global owner release/allowlist authorization stays with the existing sender.
    """
    try:
        clock = aware(now)
        if row.event_type not in EVENT_TYPES or row.aggregate_type != "power_map":
            return "REJECTED"
        value = _payload(row.payload, row.event_type)
        if clock >= _stamp(value["valid_until"]):
            return "REJECTED"
        identifier = UUID(row.aggregate_id)
        if str(identifier) != row.aggregate_id:
            return "REJECTED"
        context = await _context(session, identifier, clock, stale_after_seconds, lock=lock)
        if context is None:
            return "REJECTED"
        sub = context.subscription
        if not _event_allowed(sub, row.event_type) or (
            sub.revision,
            sub.provider.value,
            sub.group_id,
            context.credential_revision,
        ) != (
            value["preference_revision"],
            value["provider"],
            value["group"],
            value["credential_revision"],
        ):
            return "REJECTED"
        if expected_recipient is not None and (
            type(getattr(expected_recipient, "chat_id", None)) is not int
            or expected_recipient.chat_id != context.user.telegram_user_id
        ):
            return "REJECTED"
        if lock and expected_recipient is None:
            return "REJECTED"
        if row.event_type == STATUS_CHANGED:
            day = date.fromisoformat(value["status_day"])
            observed = _status_observations(context, clock).get(value["status_day"])
            state = _read_status_state(
                await session.get(RuntimeSetting, STATUS_STATE_PREFIX + str(identifier)), context
            ).get(value["status_day"])
            transition = _stamp(value["transition_at"])
            if (observed is None or state is None or clock < transition
                    or observed[0] != value["status"] or state["status"] != value["status"]
                    or _stamp(state["transition_at"]) != transition
                    or observed[1] < _stamp(state["checked_at"])
                    or aware(context.map_view.data.fetched_at)
                    < _stamp(state.get("fetched_at", state["checked_at"]))):
                return "REJECTED"
            dates = {day}
        elif row.event_type in EMERGENCY_EVENTS:
            current = _emergency_observation(context, clock)
            state = _read_emergency_state(
                await session.get(RuntimeSetting, EMERGENCY_STATE_PREFIX + str(identifier)), context
            )
            active = row.event_type == EMERGENCY_STARTED
            transition = _stamp(value["transition_at"])
            if (
                current is None
                or state is None
                or current[0] != active
                or state[0] != active
                or state[2] != transition
                or clock < transition
                or current[1] < state[1]
            ):
                return "REJECTED"
            dates = {clock.astimezone(KYIV).date()}
        elif row.event_type == CHANGED:
            day = _read_day(value["day"])
            today = clock.astimezone(KYIV).date()
            if day.local_date not in {today, today + timedelta(days=1)}:
                return "REJECTED"
            matching = next(
                (
                    view
                    for view in context.views
                    if view.schedule is not None and view.schedule.local_date == day.local_date
                ),
                None,
            )
            if (
                matching is None
                or usable_map_day(matching, clock, stale_after_seconds=stale_after_seconds) is None
                or matching.schedule is None
                or matching.schedule.content_hash != day.content_hash
            ):
                return "REJECTED"
            dates = {day.local_date}
        else:
            event = MapPowerReminder.from_wire(value["event"])
            current = plan_map_reminders(
                context.views,
                clock,
                outage_lead_minutes=sub.outage_lead_minutes,
                restoration_lead_minutes=sub.restoration_lead_minutes,
                stale_after_seconds=stale_after_seconds,
            )
            if not any(_same_claims(event, item) for item in current):
                return "REJECTED"
            # Recompute exactly the stored claims' evidence. Candidate evidence
            # may contain newly available next-day detail which this queued
            # message never claimed; acquisition is never authority by itself.
            if (
                map_reminder_evidence_hashes(
                    event,
                    context.views,
                    clock,
                    stale_after_seconds=stale_after_seconds,
                )
                != event.schedule_hashes
            ):
                return "REJECTED"
            dates = {date.fromisoformat(day) for day, _ in event.schedule_hashes}
        fence = MapDeliveryFence(_stamp(value["valid_until"]), context.deadlines(dates), clock)
        if not fence.valid_at(clock):
            return "REJECTED"
        if fences is not None:
            fences.append(fence)
        return "AUTHORIZED"
    except (KeyError, ValueError, TypeError, RecursionError, OverflowError):
        return "REJECTED"


async def disable_map_for_failed_recipient(
    session: AsyncSession, row: OutboxMessage, now: datetime
) -> None:
    """Stop only this unchanged map opt-in after a permanent transport failure."""
    try:
        if row.aggregate_type != "power_map" or row.event_type not in EVENT_TYPES:
            return
        value = _payload(row.payload, row.event_type)
        identifier = UUID(row.aggregate_id)
        user = await session.get(User, identifier, with_for_update=True)
        if user is None:
            return
        pref = await session.get(
            RuntimeSetting, preference_key(str(identifier)), with_for_update=True
        )
        if pref is None:
            return
        sub = decode(pref.value, str(identifier))
        if sub.revision != value["preference_revision"] or not sub.enabled:
            return
        if sub.revision >= 2**31 - 1 or pref.value["version"] != 2:
            return
        pref.value = {**pref.value, "enabled": False, "revision": sub.revision + 1}
        pref.updated_at = aware(now)
    except (KeyError, ValueError, TypeError, RecursionError):
        return


class PostgresPowerMapNotificationRepository:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        stale_after_seconds: int = 1800,
        allowed_chat_ids: tuple[int, ...] | None = None,
        authority_guard: Callable[[AsyncSession, datetime], Awaitable[None]] | None = None,
    ) -> None:
        if (
            type(stale_after_seconds) is not int
            or not 1 <= stale_after_seconds <= 86400
            or allowed_chat_ids is not None
            and (
                type(allowed_chat_ids) is not tuple
                or not allowed_chat_ids
                or any(type(item) is not int or item <= 0 for item in allowed_chat_ids)
                or len(set(allowed_chat_ids)) != len(allowed_chat_ids)
            )
        ):
            raise ValueError("invalid_map_notification_configuration")
        self._factory, self._ttl, self._allowlist = factory, stale_after_seconds, allowed_chat_ids
        self._authority_guard = authority_guard

    async def plan_notifications(self, now: datetime, *, limit: int = 100) -> int:
        clock = aware(now)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid_map_notification_limit")
        total, cursor = 0, None
        while True:
            query = (
                select(User.id)
                .join(RuntimeSetting, RuntimeSetting.key == PREFIX + User.id.cast(String))
                .order_by(User.id)
                .limit(limit)
            )
            if cursor is not None:
                query = query.where(User.id > cursor)
            if self._allowlist is not None:
                query = query.where(User.telegram_user_id.in_(self._allowlist))
            async with self._factory() as session:
                users = (await session.execute(query)).scalars().all()
            if not users:
                return total
            for identifier in users:
                total += await self._plan_user(identifier, clock)
            cursor = users[-1]

    async def _plan_user(self, identifier: UUID, now: datetime) -> int:
        async with self._factory() as session, session.begin():
            # Owner authority locks precede User/cache locks, matching sender order.
            if self._authority_guard is not None:
                await self._authority_guard(session, now)
            try:
                context = await _context(session, identifier, now, self._ttl, lock=True)
            except (ValueError, TypeError, KeyError, RecursionError):
                return 0
            if (
                context is None
                or self._allowlist is not None
                and context.user.telegram_user_id not in self._allowlist
            ):
                return 0
            sub = context.subscription
            # Skip rows currently held by a sender. Its own final policy will
            # reject stale content. Never block on outbox while holding User.
            pending = (
                (
                    await session.execute(
                        select(OutboxMessage)
                        .where(
                            OutboxMessage.aggregate_type == "power_map",
                            OutboxMessage.aggregate_id == str(identifier),
                            OutboxMessage.delivered_at.is_(None),
                            OutboxMessage.dead_at.is_(None),
                            OutboxMessage.dispatch_started_at.is_(None),
                        )
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for row in pending:
                if (
                    await map_delivery_policy(session, row, now, stale_after_seconds=self._ttl)
                    != "AUTHORIZED"
                ):
                    row.dead_at, row.claimed_at = now, None
                    row.last_error_code = "map_schedule_or_preferences_changed"
            state_key = reminder_state_key(str(identifier))
            state = await session.get(RuntimeSetting, state_key, with_for_update=True)
            try:
                previous_cursor = _cursor(state.value if state else None, sub)
            except (ValueError, TypeError, KeyError, RecursionError):
                return 0
            previous = previous_cursor.days if previous_cursor else None
            generation = previous_cursor.generation if previous_cursor else 0
            pending_changes = dict(previous_cursor.pending_changes) if previous_cursor else {}
            retry_changes: set[str] = set()
            if pending_changes:
                prior_messages = (
                    (
                        await session.execute(
                            select(OutboxMessage).where(
                                OutboxMessage.aggregate_type == "power_map",
                                OutboxMessage.aggregate_id == str(identifier),
                                OutboxMessage.event_key.in_(tuple(pending_changes.values())),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                by_key = {message.event_key: message for message in prior_messages}
                for pending_day, event_key in tuple(pending_changes.items()):
                    prior = by_key.get(event_key)
                    if prior is None:
                        # Missing history is not proof that a message was unsent.
                        pending_changes.pop(pending_day)
                    elif (
                        prior.delivered_at is not None
                        or prior.indeterminate_at is not None
                        or prior.dispatch_started_at is not None
                    ):
                        pending_changes.pop(pending_day)
                    elif prior.dead_at is not None:
                        # A never-dispatched terminal row is not resurrected.
                        # Fresh recovery queues one new generation with latest data.
                        retry_changes.add(pending_day)
            days: dict[str, str | None] = {}
            valid_views: dict[str, PowerScheduleView] = {}
            for offset in (0, 1):
                day = now.astimezone(KYIV).date() + timedelta(days=offset)
                key = day.isoformat()
                current = next(
                    (
                        view
                        for view in context.views
                        if view.schedule is not None and view.schedule.local_date == day
                    ),
                    None,
                )
                published = (
                    usable_map_day(current, now, stale_after_seconds=self._ttl)
                    if current is not None
                    else None
                )
                days[key] = published.content_hash if published else (previous or {}).get(key)
                if published is not None and current is not None:
                    valid_views[key] = current
            events = list(
                plan_map_reminders(
                    context.views,
                    now,
                    outage_lead_minutes=sub.outage_lead_minutes,
                    restoration_lead_minutes=sub.restoration_lead_minutes,
                    stale_after_seconds=self._ttl,
                )
            )
            candidates: list[tuple[str, str, dict[str, Any]]] = []
            for event in events:
                if _event_allowed(sub, event.event_type):
                    payload = _identity_payload(context, _expiry(event))
                    payload["event"] = event.to_wire()
                    # Stable across cache refreshes, preference edits and restarts.
                    candidates.append((event.event_type, event.transition_key, payload))
            for changed_day, view in valid_views.items():
                if (
                    previous is None
                    or changed_day not in previous
                    or previous[changed_day] == days[changed_day]
                    and changed_day not in retry_changes
                ):
                    continue
                assert view.schedule is not None
                deadline = min(
                    now + timedelta(minutes=10), day_bounds(date.fromisoformat(changed_day))[1]
                )
                payload = _identity_payload(context, deadline)
                payload["day"] = _day_wire(view.schedule)
                generation += 1
                candidates.append(
                    (
                        CHANGED,
                        _hash([sub.revision, changed_day, generation, days[changed_day]]),
                        payload,
                    )
                )
            try:
                statuses = await _plan_status_changes(session, context, now)
            except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
                statuses = []
            candidates.extend(statuses)
            try:
                emergency = await _plan_emergency(session, context, now)
            except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
                emergency = None
            if emergency is not None and not any(
                payload["status_day"] == now.astimezone(KYIV).date().isoformat()
                for _, _, payload in statuses
            ):
                candidates.append(emergency)
            inserted = 0
            for kind, key, payload in candidates:
                # Validate before persisting so malformed data cannot enter a
                # sender's permissive fallback branch.
                _payload(payload, kind)
                event_key = "power-map:" + _hash([str(identifier), kind, key])
                # User serialization already excludes another planner for this
                # actor. Read committed existing identities without a row lock
                # before ON CONFLICT: a sender may hold that old row while
                # waiting for our User lock. Never wait on it just to dedupe.
                existing = (
                    (
                        await session.execute(
                            select(OutboxMessage).where(
                                OutboxMessage.event_key == event_key,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if existing:
                    continue
                statement = (
                    insert(OutboxMessage)
                    .values(
                        id=uuid4(),
                        event_key=event_key,
                        aggregate_type="power_map",
                        aggregate_id=str(identifier),
                        event_type=kind,
                        payload=payload,
                        available_at=now,
                    )
                    .on_conflict_do_nothing(index_elements=[OutboxMessage.event_key])
                    .returning(OutboxMessage.id)
                )
                inserted += int((await session.execute(statement)).scalar_one_or_none() is not None)
                if kind == CHANGED:
                    pending_changes[str(payload["day"]["date"])] = event_key
            value = {
                "version": 2,
                "preference_revision": sub.revision,
                "provider": sub.provider.value,
                "group": sub.group_id,
                "days": days,
                "generation": generation,
                "pending_changes": {
                    day: key for day, key in pending_changes.items() if day in days
                },
            }
            if state is None:
                session.add(
                    RuntimeSetting(key=state_key, value=value, updated_by="power-map-reminders")
                )
            else:
                state.value, state.updated_at = value, now
            if self._authority_guard is not None:
                await self._authority_guard(session, now)
            return inserted
