"""Transactional weather planning and final cache-only delivery authority.

There is no transport here. The existing singleton sender retains global rate
limits and the durable indeterminate-delivery boundary for this namespace too.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime, and_, func, literal, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from time_tl.application.weather_notifications import (
    WeatherNotificationPlanner,
    WeatherNotificationPreferences,
    daily_summary,
    event_supported,
    validate_weather_notification_payload,
)
from time_tl.domain.weather import ForecastSnapshot, WeatherThresholds
from time_tl.domain.weather import WeatherChangeEvent as DomainEvent

from .geographic_coverage import _weather_point
from .models import (
    ActiveTerritoryCatalog,
    OutboxMessage,
    RuntimeSetting,
    Territory,
    TerritoryCatalogVersion,
    User,
    UserWeatherPreference,
    WeatherChangeEvent,
    WeatherLocation,
    WeatherNotificationDelivery,
)
from .weather import _bound_previous, _point_identity
from .weather_configuration import SETTING, WeatherConfiguration

EVENT_TYPES = frozenset({"WEATHER_DAILY", "WEATHER_EVENTS"})


def semantic_delivery_history(
    deliveries: list[WeatherNotificationDelivery],
    outboxes: list[OutboxMessage],
    *,
    excluding: UUID | None = None,
    now: datetime,
) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """Unknown I/O remains a tombstone across preference/configuration epochs."""
    by_id: dict[UUID | None, OutboxMessage] = {row.id: row for row in outboxes}
    ambiguous_events: set[str] = set()
    ambiguous_dates: set[str] = set()
    delivered_dates: set[str] = set()
    for delivery in deliveries:
        if excluding is not None and delivery.outbox_id == excluding:
            continue
        row = by_id.get(delivery.outbox_id)
        ambiguous = (
            row is not None
            and delivery.delivered_at is None
            and (
                row.claimed_at is not None
                or row.dispatch_started_at is not None
                or row.indeterminate_at is not None
                or (row.attempts or 0) > 0
                or row.delivered_at is not None
            )
        )
        if row is None:
            if delivery.delivered_at is None and delivery.created_at >= now - timedelta(days=7):
                ambiguous_events.update(key[:32] for key in delivery.event_keys)
                if not delivery.event_keys:
                    ambiguous_dates.add("*")
            continue
        if not ambiguous and delivery.delivered_at is None:
            continue
        payload = validate_weather_notification_payload(row.payload)
        if row.event_type == "WEATHER_DAILY":
            target = str(payload["summary"]["forecast_date"])
            if ambiguous:
                ambiguous_dates.add(target)
            else:
                delivered_dates.add(target)
        elif ambiguous:
            ambiguous_events.update(key[:32] for key in payload["event_keys"])
    return frozenset(ambiguous_events), frozenset(ambiguous_dates), frozenset(delivered_dates)


def preferences(row: UserWeatherPreference) -> WeatherNotificationPreferences:
    return WeatherNotificationPreferences(
        **{field.name: getattr(row, field.name) for field in fields(WeatherNotificationPreferences)}
    )


def thresholds(config: WeatherConfiguration) -> WeatherThresholds:
    return WeatherThresholds(
        rain_mm=config.rain_mm,
        rain_probability=config.rain_probability,
        gust_ms=config.gust_ms,
        temperature_change_c=config.temperature_change_c,
    )


async def _config(session: AsyncSession, *, lock: bool) -> WeatherConfiguration:
    query = select(RuntimeSetting).where(RuntimeSetting.key == SETTING)
    if lock:
        query = query.with_for_update(read=True)
    row = (await session.execute(query)).scalar_one_or_none()
    return WeatherConfiguration.model_validate(row.value if row else {})


async def _context(
    session: AsyncSession, user_id: UUID, now: datetime, *, lock: bool
) -> tuple[User, UserWeatherPreference, WeatherLocation, ForecastSnapshot] | None:
    user_query = select(User).where(User.id == user_id)
    if lock:
        user_query = user_query.with_for_update()
    user = (await session.execute(user_query)).scalar_one_or_none()
    if user is None or not user.confirmed_region_id:
        return None
    pref_query = select(UserWeatherPreference).where(UserWeatherPreference.user_id == user_id)
    if lock:
        pref_query = pref_query.with_for_update(read=True)
    pref = (await session.execute(pref_query)).scalar_one_or_none()
    if pref is None or not pref.weather_enabled:
        return None
    catalog_query = select(ActiveTerritoryCatalog).where(
        ActiveTerritoryCatalog.provider == "alerts.in.ua"
    )
    if lock:
        catalog_query = catalog_query.with_for_update(read=True)
    catalog = (await session.execute(catalog_query)).scalar_one_or_none()
    if catalog is None:
        return None
    query = (
        select(WeatherLocation)
        .join(
            Territory,
            and_(
                Territory.provider == catalog.provider,
                Territory.catalog_version == catalog.catalog_version,
                Territory.region_id == user.confirmed_region_id,
                Territory.external_code == WeatherLocation.katottg_code,
                Territory.region_id == WeatherLocation.region_id,
                Territory.kind.in_(("HROMADA", "SPECIAL_STATUS_CITY")),
                Territory.is_selectable.is_(True),
            ),
        )
        .join(
            TerritoryCatalogVersion,
            and_(
                TerritoryCatalogVersion.provider == Territory.provider,
                TerritoryCatalogVersion.catalog_version == Territory.catalog_version,
                TerritoryCatalogVersion.status == "ACTIVE",
            ),
        )
        .where(WeatherLocation.enabled.is_(True), WeatherLocation.coverage_type == "BASE")
    )
    if lock:
        query = query.with_for_update(read=True, of=WeatherLocation)
    location = (await session.execute(query)).scalar_one_or_none()
    if location is None or not _weather_point(location).approved():
        return None
    snapshot = await _bound_previous(session, location.id, _point_identity(location))
    if snapshot is None or not snapshot.usable(now):
        return None
    return user, pref, location, snapshot


async def _latest_events(
    session: AsyncSession, point_id: UUID, now: datetime
) -> dict[str, DomainEvent]:
    # Rehydrate against the originating snapshot, retaining the immutable source
    # generation even if a newer unrelated forecast is now the active snapshot.
    identity = func.substr(WeatherChangeEvent.event_key, 1, 32)
    query = (
        select(WeatherChangeEvent)
        .where(
            WeatherChangeEvent.location_id == point_id,
            WeatherChangeEvent.expected_end > now,
            WeatherChangeEvent.provider == "MET Norway",
        )
        .distinct(identity)
        .order_by(identity, WeatherChangeEvent.detected_at.desc(), WeatherChangeEvent.id.desc())
    )
    result = {}
    for row in (await session.execute(query)).scalars().all():
        if row.status != ("CANCELLED" if row.kind == "CANCELLATION" else "ACTIVE"):
            continue
        event = DomainEvent(
            str(point_id),
            row.event_type,
            row.kind,
            row.expected_start,
            row.expected_end,
            row.previous_value,
            row.current_value,
            row.detected_at,
            row.provider,
            row.source_revision,
        )
        if event.event_key == row.event_key:
            result[row.event_key] = event
    return result


@dataclass(frozen=True)
class WeatherDeliveryFence:
    expires_at: datetime
    source_expires_at: datetime
    source_updated_at: datetime

    def valid_at(self, now: datetime) -> bool:
        return now < min(self.expires_at, self.source_expires_at) and timedelta(
            minutes=-5
        ) <= now - self.source_updated_at <= timedelta(hours=24)


async def weather_delivery_policy(
    session: AsyncSession,
    row: OutboxMessage,
    now: datetime,
    *,
    lock: bool = False,
    expected_recipient: Any = None,
    fences: list[WeatherDeliveryFence] | None = None,
) -> bool:
    if row.event_type not in EVENT_TYPES or row.aggregate_type != "weather":
        return False
    try:
        payload = validate_weather_notification_payload(row.payload)
        if payload["kind"] != row.event_type or payload["user_id"] != row.aggregate_id:
            return False
        expiry = datetime.fromisoformat(payload["expires_at"])
        if now >= expiry:
            return False
        config = await _config(session, lock=lock)
        if (
            not config.enabled
            or not config.terms_approved
            or (payload["configuration_revision"] != config.revision)
        ):
            return False
        context = await _context(session, UUID(row.aggregate_id), now, lock=lock)
        if context is None:
            return False
        user, pref, location, snapshot = context
        if (
            str(location.id) != payload["point_id"]
            or user.confirmed_region_id != payload["region_id"]
            or location.locality_name != payload["locality"]
            or pref.revision != payload["preference_revision"]
        ):
            return False
        if expected_recipient is not None and (
            expected_recipient.chat_id != user.telegram_user_id
            or expected_recipient.confirmed_region_id != user.confirmed_region_id
            or expected_recipient.confirmed_region_name != user.region_name
        ):
            return False
        delivery = await session.get(
            WeatherNotificationDelivery, (user.id, row.event_key.removeprefix("weather:"))
        )
        if delivery is None or delivery.outbox_id != row.id or delivery.delivered_at is not None:
            return False
        receipts = (
            (
                await session.execute(
                    select(WeatherNotificationDelivery).where(
                        WeatherNotificationDelivery.user_id == user.id
                    )
                )
            )
            .scalars()
            .all()
        )
        history_rows = (
            (
                await session.execute(
                    select(OutboxMessage).where(
                        OutboxMessage.aggregate_type == "weather",
                        OutboxMessage.aggregate_id == str(user.id),
                    )
                )
            )
            .scalars()
            .all()
        )
        ambiguous_events, ambiguous_dates, delivered_dates = semantic_delivery_history(
            list(receipts), list(history_rows), excluding=row.id, now=now
        )
        if row.event_type == "WEATHER_DAILY":
            if (
                not pref.daily_forecast_enabled
                or daily_summary(snapshot, payload["summary"]["forecast_date"])
                != payload["summary"]
                or payload["summary"]["forecast_date"] in ambiguous_dates | delivered_dates
                or "*" in ambiguous_dates
            ):
                return False
        else:
            events = await _latest_events(session, location.id, now)
            allowed = {
                "RAIN": pref.rain_alert_enabled,
                "SNOW": pref.snow_alert_enabled,
                "WIND": pref.wind_alert_enabled,
                "TEMPERATURE": pref.temperature_alert_enabled,
                "FROST": pref.frost_alert_enabled,
            }
            delivered = (
                (
                    await session.execute(
                        select(WeatherNotificationDelivery.event_keys).where(
                            WeatherNotificationDelivery.user_id == user.id,
                            WeatherNotificationDelivery.delivered_at.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            identities = {key[:32] for keys in delivered for key in keys}
            for claimed in payload["events"]:
                if claimed["event_key"][:32] in ambiguous_events:
                    return False
                event = events.get(claimed["event_key"])
                if (
                    event is None
                    or not allowed.get(event.event_type, False)
                    or not event_supported(snapshot, event, thresholds(config), now)
                ):
                    return False
                if (
                    claimed["event_type"],
                    claimed["kind"],
                    claimed["start"],
                    claimed["end"],
                    claimed["previous_value"],
                    claimed["current_value"],
                ) != (
                    event.event_type,
                    event.kind,
                    event.expected_start.isoformat(),
                    event.expected_end.isoformat(),
                    event.previous_value,
                    event.current_value,
                ):
                    return False
                was_delivered = event.identity_key in identities
                if claimed["initial"]:
                    if was_delivered or event.kind == "CANCELLATION":
                        return False
                elif not was_delivered or not pref.weather_changes_enabled:
                    return False
        if fences is not None:
            fences.append(
                WeatherDeliveryFence(expiry, snapshot.expires_at, snapshot.provider_updated_at)
            )
        return True
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


async def mark_weather_delivered(session: AsyncSession, row: OutboxMessage, now: datetime) -> None:
    delivery = await session.get(
        WeatherNotificationDelivery,
        (UUID(row.aggregate_id), row.event_key.removeprefix("weather:")),
    )
    if delivery is None or delivery.outbox_id != row.id:
        raise ValueError("weather_delivery_identity_missing")
    delivery.delivered_at = now


async def disable_weather_for_failed_recipient(
    session: AsyncSession, row: OutboxMessage, now: datetime
) -> None:
    if row.aggregate_type != "weather" or row.event_type not in EVENT_TYPES:
        return
    user_id = UUID(row.aggregate_id)
    user = await session.get(User, user_id, with_for_update=True)
    pref = await session.get(UserWeatherPreference, user_id, with_for_update=True)
    if user is not None and pref is not None and pref.weather_enabled:
        pref.weather_enabled, pref.revision, pref.updated_at = False, pref.revision + 1, now


def safely_cancelled_before_dispatch(row: OutboxMessage | None) -> bool:
    return bool(
        row is not None
        and row.delivered_at is None
        and row.dead_at is not None
        and row.claimed_at is None
        and row.dispatch_started_at is None
        and row.indeterminate_at is None
        and row.attempts == 0
        and row.last_error_code
        in {
            "delivery_policy_changed",
            "final_delivery_policy_changed",
            "final_weather_expired_or_stale",
            "final_delivery_expired",
        }
    )


def due_weather_targets(now: datetime, *, after: UUID | None = None, limit: int = 200) -> Any:
    """Select due demand in SQL; unchanged unrelated users are not replanned."""
    if now.tzinfo is None or not 1 <= limit <= 500:
        raise ValueError("invalid_weather_target_query")
    lead = UserWeatherPreference.notification_lead_minutes * text("INTERVAL '1 minute'")
    category = or_(
        and_(WeatherChangeEvent.event_type == "RAIN", UserWeatherPreference.rain_alert_enabled),
        and_(WeatherChangeEvent.event_type == "SNOW", UserWeatherPreference.snow_alert_enabled),
        and_(WeatherChangeEvent.event_type == "WIND", UserWeatherPreference.wind_alert_enabled),
        and_(
            WeatherChangeEvent.event_type == "TEMPERATURE",
            UserWeatherPreference.temperature_alert_enabled,
        ),
        and_(WeatherChangeEvent.event_type == "FROST", UserWeatherPreference.frost_alert_enabled),
    )
    event_due = (
        select(WeatherChangeEvent.id)
        .join(WeatherLocation, WeatherLocation.id == WeatherChangeEvent.location_id)
        .where(
            WeatherLocation.region_id == User.confirmed_region_id,
            WeatherLocation.enabled.is_(True),
            WeatherLocation.coverage_type == "BASE",
            WeatherChangeEvent.expected_end > now,
            category,
            or_(
                and_(
                    WeatherChangeEvent.expected_start
                    <= literal(now, type_=DateTime(timezone=True)) + lead,
                    WeatherChangeEvent.expected_start
                    > literal(now - timedelta(minutes=10), type_=DateTime(timezone=True)) + lead,
                ),
                and_(
                    WeatherChangeEvent.kind.in_(("CORRECTION", "CANCELLATION")),
                    UserWeatherPreference.weather_changes_enabled,
                ),
            ),
        )
        .correlate(User, UserWeatherPreference)
        .exists()
    )
    local = now.astimezone(ZoneInfo("Europe/Kyiv"))
    lower = local - timedelta(minutes=10)
    daily = and_(
        UserWeatherPreference.daily_forecast_enabled,
        UserWeatherPreference.daily_forecast_time <= local.time(),
    )
    if local.date() == lower.date():
        daily = and_(daily, UserWeatherPreference.daily_forecast_time > lower.time())
    query = (
        select(UserWeatherPreference.user_id)
        .join(User, User.id == UserWeatherPreference.user_id)
        .where(UserWeatherPreference.weather_enabled.is_(True), or_(daily, event_due))
    )
    if after is not None:
        query = query.where(UserWeatherPreference.user_id > after)
    return query.order_by(UserWeatherPreference.user_id).limit(limit)


class PostgresWeatherNotificationPlanner:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        enabled: bool = False,
        authority_guard: Callable[[AsyncSession, datetime], Awaitable[None]] | None = None,
    ) -> None:
        self.factory, self.enabled = factory, enabled
        self.authority_guard = authority_guard

    async def plan_user(self, user_id: UUID, now: datetime) -> int:
        if not self.enabled:
            return 0
        async with self.factory() as session, session.begin():
            if self.authority_guard is not None:
                await self.authority_guard(session, now)
            config = await _config(session, lock=True)
            if not config.enabled or not config.terms_approved:
                return 0
            context = await _context(session, user_id, now, lock=True)
            if context is None:
                return 0
            user, pref, point, snapshot = context
            events = await _latest_events(session, point.id, now)
            deliveries = (
                (
                    await session.execute(
                        select(WeatherNotificationDelivery).where(
                            WeatherNotificationDelivery.user_id == user_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            delivered = frozenset(
                key
                for item in deliveries
                if item.delivered_at is not None
                for key in item.event_keys
            )
            outboxes = (
                (
                    await session.execute(
                        select(OutboxMessage).where(
                            OutboxMessage.aggregate_type == "weather",
                            OutboxMessage.aggregate_id == str(user_id),
                        )
                    )
                )
                .scalars()
                .all()
            )
            by_id: dict[UUID | None, OutboxMessage] = {row.id: row for row in outboxes}
            receipts = {"weather:" + item.notification_key: item for item in deliveries}
            known = frozenset(
                key
                for key, item in receipts.items()
                if item.delivered_at is not None
                or not safely_cancelled_before_dispatch(by_id.get(item.outbox_id))
            )
            ambiguous_events, ambiguous_dates, delivered_dates = semantic_delivery_history(
                list(deliveries), list(outboxes), now=now
            )
            plans = WeatherNotificationPlanner(thresholds(config)).plan(
                user_id=str(user_id),
                region_id=user.confirmed_region_id or "",
                locality=point.locality_name,
                snapshot=snapshot,
                events=tuple(events.values()),
                preferences=preferences(pref),
                delivered_event_keys=delivered,
                known_notification_keys=known,
                now=now,
                configuration_revision=config.revision,
                delivered_forecast_dates=delivered_dates,
            )
            inserted = 0
            for plan in plans:
                if plan.kind == "WEATHER_DAILY":
                    summary = plan.payload["summary"]
                    assert isinstance(summary, dict)
                    if "*" in ambiguous_dates or summary["forecast_date"] in ambiguous_dates:
                        continue
                elif any(key[:32] in ambiguous_events for key in plan.event_keys):
                    continue
                key = plan.key
                # Follow immutable recovery generations. A fresh process derives
                # the same next key; claimed/sent/ambiguous rows are never rewritten.
                for _ in range(32):
                    receipt = receipts.get(key)
                    if receipt is None:
                        break
                    old = by_id.get(receipt.outbox_id)
                    if receipt.delivered_at is not None or not safely_cancelled_before_dispatch(
                        old
                    ):
                        key = ""
                        break
                    assert old is not None
                    key = (
                        "weather:"
                        + hashlib.sha256((key + ":after:" + str(old.id)).encode()).hexdigest()
                    )
                else:
                    key = ""  # Bounded recovery; operator investigation instead of a loop.
                if not key:
                    continue
                identifier = uuid4()
                result = await session.execute(
                    insert(OutboxMessage)
                    .values(
                        id=identifier,
                        event_key=key,
                        aggregate_type="weather",
                        aggregate_id=str(user_id),
                        event_type=plan.kind,
                        payload=plan.payload,
                        available_at=plan.available_at,
                    )
                    .on_conflict_do_nothing(index_elements=[OutboxMessage.event_key])
                    .returning(OutboxMessage.id)
                )
                if result.scalar_one_or_none() is not None:
                    session.add(
                        WeatherNotificationDelivery(
                            user_id=user_id,
                            notification_key=key.removeprefix("weather:"),
                            outbox_id=identifier,
                            event_keys=list(plan.event_keys),
                            created_at=now,
                        )
                    )
                    inserted += 1
            if self.authority_guard is not None:
                await self.authority_guard(session, now)
            return inserted

    async def run_once(self, now: datetime, *, page_size: int = 200) -> int:
        if not self.enabled:
            return 0
        if not 1 <= page_size <= 500:
            raise ValueError("invalid_weather_page_size")
        after: UUID | None = None
        total = 0
        while True:
            async with self.factory() as session:
                users = (
                    (await session.execute(due_weather_targets(now, after=after, limit=page_size)))
                    .scalars()
                    .all()
                )
            for user in users:
                total += await self.plan_user(user, now)
            if len(users) < page_size:
                return total
            after = users[-1]
