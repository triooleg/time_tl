"""Durable observed alert history and consent-bound daily report outbox.

This module never decides whether an all-clear exists. It records only the
already validated aggregate state and cannot replace the alert state machine.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import inspect, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from time_tl.application.sender_worker import DeliveryRecipient
from time_tl.domain.alert_statistics import (
    AlertStatistics,
    ObservedPeriod,
    ObservedSegment,
    aware,
    report_window,
    summarize,
    validate_report_payload,
)
from time_tl.domain.alerts import AlertSnapshot, AlertState, Transition, TransitionKind

from .models import (
    AlertHistoryCursor,
    AlertHistoryPeriod,
    AlertHistorySegment,
    AlertScope,
    OutboxMessage,
    User,
)

MODES = {"ALERTS_ONLY", "LESSONS_AND_ALERTS"}
MAX_OBSERVATION_GAP = timedelta(seconds=120)
LOG = logging.getLogger(__name__)


async def assert_alert_history_ready(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        head = (
            (await session.execute(text("SELECT version_num FROM alembic_version"))).scalars().all()
        )
        if head != ["0015_alert_history"]:
            raise RuntimeError("alert_history_schema_not_ready")
        connection = await session.connection()

        def validate(sync: Connection) -> None:
            inspector = inspect(sync)
            for model in (AlertHistoryPeriod, AlertHistorySegment, AlertHistoryCursor):
                columns = {
                    item["name"]: item for item in inspector.get_columns(model.__tablename__)
                }
                if set(columns) != set(model.__table__.columns.keys()):
                    raise RuntimeError("alert_history_schema_not_ready")
                for expected in model.__table__.columns:
                    actual = columns[expected.name]
                    if (
                        actual["type"]._type_affinity != expected.type._type_affinity
                        or actual["nullable"] != expected.nullable
                        or getattr(actual["type"], "timezone", None)
                        != getattr(expected.type, "timezone", None)
                    ):
                        raise RuntimeError("alert_history_schema_not_ready")

        await connection.run_sync(validate)


def scope_binding(scope: AlertScope) -> dict[str, object] | None:
    if not scope.territory_provider or not scope.territory_catalog_version:
        return None
    return {
        "provider": scope.territory_provider,
        "catalog_version": scope.territory_catalog_version,
        "selected_region_id": scope.selected_region_id,
        "applicable_region_ids": sorted(set(scope.applicable_region_ids)),
    }


def binding_key(binding: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def unknown_duration(reason: str = "history_unavailable") -> dict[str, object]:
    return {
        "duration_seconds": None,
        "duration_is_approximate": True,
        "duration_unknown_reason": reason,
    }


async def _window_already_admitted(
    session: AsyncSession, scope_id: UUID, end: datetime, *, excluding: UUID | None = None
) -> bool:
    """Consent/catalog epochs never reset an admitted user/window identity.

    Claimed work is intentionally conservative: we cannot infer that it never
    reached transport. A new generation can replace only provably unadmitted
    rows, whose old territory policy is no longer authorized.
    """
    query = select(OutboxMessage).where(
        OutboxMessage.aggregate_type == "alert_scope",
        OutboxMessage.aggregate_id == str(scope_id),
        OutboxMessage.event_type == "ALERT_DAILY_SUMMARY",
        OutboxMessage.payload["period_end"].astext == end.isoformat(),
    )
    if excluding is not None:
        query = query.where(OutboxMessage.id != excluding)
    rows = (await session.scalars(query.execution_options(populate_existing=True))).all()
    return any(
        row.delivered_at is not None
        or row.claimed_at is not None
        or row.dispatch_started_at is not None
        or row.indeterminate_at is not None
        or type(row.attempts) is not int
        or row.attempts != 0
        for row in rows
    )


async def record_source_gap(session: AsyncSession, scope_id: UUID) -> None:
    cursor = await session.get(AlertHistoryCursor, scope_id)
    if cursor is None:
        return
    cursor.state, cursor.segment_id = "UNKNOWN", None
    if cursor.period_id is not None:
        period = await session.get(AlertHistoryPeriod, cursor.period_id)
        if period is not None:
            period.has_gaps = True


async def record_snapshot(
    session: AsyncSession,
    scope: AlertScope,
    original: AlertSnapshot | None,
    snapshot: AlertSnapshot,
    transition: Transition,
    at: datetime,
    *,
    max_observation_gap: timedelta = MAX_OBSERVATION_GAP,
) -> dict[str, object]:
    at = aware(at)
    binding = scope_binding(scope)
    if binding is None:
        return unknown_duration()
    key = binding_key(binding)
    cursor = await session.get(AlertHistoryCursor, scope.id)
    if cursor is None:
        cursor = AlertHistoryCursor(scope_id=scope.id, binding_key=key, state="UNKNOWN")
        session.add(cursor)
    elif cursor.binding_key != key:
        # Territory changes cannot carry another territory's active episode.
        if cursor.period_id is not None:
            abandoned = await session.get(AlertHistoryPeriod, cursor.period_id)
            if abandoned is not None and abandoned.ended_at is None:
                abandoned.tracking_ended_at = cursor.last_observed_at
                abandoned.has_gaps = True
        cursor.binding_key, cursor.state = key, "UNKNOWN"
        cursor.period_id = cursor.segment_id = cursor.last_observed_at = None
    pending_clear = bool(snapshot.pending_clear_candidate and snapshot.active_episodes)
    if snapshot.state not in {AlertState.ACTIVE, AlertState.CLEAR_CONFIRMED} and not pending_clear:
        await record_source_gap(session, scope.id)
        return unknown_duration("source_gap")
    last = aware(cursor.last_observed_at) if cursor.last_observed_at is not None else None
    contiguous = (
        last is not None
        and timedelta(0) < at - last <= max_observation_gap
        and cursor.state in {"ACTIVE", "CLEAR_CONFIRMED"}
    )
    if last is not None and at <= last:
        # A duplicate or backward observation cannot extend a duration.
        return unknown_duration("non_monotonic_observation")
    period = await session.get(AlertHistoryPeriod, cursor.period_id) if cursor.period_id else None
    if not contiguous and period is not None:
        period.has_gaps = True
    if contiguous and cursor.segment_id is not None:
        segment = await session.get(AlertHistorySegment, cursor.segment_id)
        if segment is not None:
            segment.ended_at = at
    # A validated empty candidate is still the same effective open alert until
    # the state machine confirms the second observation. It is not source loss.
    target = "ACTIVE" if pending_clear else snapshot.state.value
    changed = cursor.state != target or not contiguous
    timing = unknown_duration()
    if target == "ACTIVE":
        if period is None:
            period = AlertHistoryPeriod(
                id=uuid4(),
                scope_id=scope.id,
                binding_key=key,
                started_at=at,
                start_confirmed=bool(
                    contiguous
                    and cursor.state == "CLEAR_CONFIRMED"
                    and transition.kind is TransitionKind.ALERT_STARTED
                ),
                has_gaps=False,
            )
            session.add(period)
            await session.flush((period,))
            cursor.period_id = period.id
        if changed:
            segment = AlertHistorySegment(
                id=uuid4(),
                scope_id=scope.id,
                binding_key=key,
                period_id=period.id,
                started_at=at,
                ended_at=at,
                active=True,
            )
            session.add(segment)
            await session.flush((segment,))
            cursor.segment_id = segment.id
    else:
        if transition.kind is TransitionKind.ALL_CLEAR_CONFIRMED and period is not None:
            period.ended_at = at
            if not period.start_confirmed:
                timing = unknown_duration("start_unobserved")
            elif period.has_gaps:
                timing = unknown_duration("source_gap")
            else:
                timing = {
                    "duration_seconds": int((at - aware(period.started_at)).total_seconds()),
                    "duration_is_approximate": True,
                    "duration_unknown_reason": None,
                }
            cursor.period_id = None
        elif period is not None:
            # A clear baseline without the required transition is not an end.
            period.has_gaps = True
            cursor.period_id = None
        if changed:
            segment = AlertHistorySegment(
                id=uuid4(),
                scope_id=scope.id,
                binding_key=key,
                period_id=None,
                started_at=at,
                ended_at=at,
                active=False,
            )
            session.add(segment)
            await session.flush((segment,))
            cursor.segment_id = segment.id
    cursor.state, cursor.last_observed_at = target, at
    return timing


async def _statistics(
    session: AsyncSession, scope: AlertScope, start: datetime, end: datetime
) -> AlertStatistics:
    binding = scope_binding(scope)
    if binding is None:
        raise ValueError("unbound alert history scope")
    key = binding_key(binding)
    segments = (
        await session.scalars(
            select(AlertHistorySegment).where(
                AlertHistorySegment.scope_id == scope.id,
                AlertHistorySegment.binding_key == key,
                AlertHistorySegment.started_at < end,
                AlertHistorySegment.ended_at > start,
            )
        )
    ).all()
    periods = (
        await session.scalars(
            select(AlertHistoryPeriod).where(
                AlertHistoryPeriod.scope_id == scope.id,
                AlertHistoryPeriod.binding_key == key,
                AlertHistoryPeriod.started_at < end,
                (AlertHistoryPeriod.ended_at.is_(None) | (AlertHistoryPeriod.ended_at > start)),
                (
                    AlertHistoryPeriod.tracking_ended_at.is_(None)
                    | (AlertHistoryPeriod.tracking_ended_at > start)
                ),
            )
        )
    ).all()
    return summarize(
        start,
        end,
        tuple(
            ObservedSegment(
                row.started_at,
                row.ended_at,
                row.active,
                str(row.period_id) if row.period_id else None,
            )
            for row in segments
        ),
        tuple(
            ObservedPeriod(
                str(row.id),
                row.started_at,
                row.start_confirmed,
                row.ended_at,
                row.has_gaps,
                row.tracking_ended_at,
            )
            for row in periods
        ),
    )


class PostgresAlertReportPlanner:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory

    async def plan(self, now: datetime) -> int:
        now = aware(now)
        start, end = report_window(now)
        if not end <= now < end + timedelta(minutes=10):
            return 0
        count = 0
        async with self.factory() as session, session.begin():
            scopes = (await session.scalars(select(AlertScope).order_by(AlertScope.id))).all()
            for candidate in scopes:
                try:
                    async with session.begin_nested():
                        count += await self._plan_scope(session, candidate, now, start, end)
                except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                    # Invalid derived history skips one target, never lessons or
                    # other recipients. Do not include personal/raw data in logs.
                    LOG.warning("alert_report_history_invalid")

        return count

    async def _plan_scope(
        self, session: AsyncSession, candidate: AlertScope,
        now: datetime, start: datetime, end: datetime,
    ) -> int:
        # Planning is derived work and must not introduce a User/Scope lock
        # inversion with alert ingestion. Final delivery locks consent rows.
        user = await session.get(User, candidate.user_id)
        scope = await session.get(AlertScope, candidate.id)
        if scope is None:
            return 0
        binding = scope_binding(scope)
        if (
            user is None
            or user.mode not in MODES
            or binding is None
            or user.confirmed_region_id != scope.selected_region_id
        ):
            return 0
        if await _window_already_admitted(session, scope.id, end):
            return 0
        cursor = await session.get(AlertHistoryCursor, scope.id)
        if (
            now < end + timedelta(minutes=1)
            and cursor is not None
            and cursor.binding_key == binding_key(binding)
            and (cursor.last_observed_at is None or aware(cursor.last_observed_at) < end)
        ):
            return 0
        stats = await _statistics(session, scope, start, end)
        key = f"alert-daily:{scope.id}:{binding_key(binding)}:{end.isoformat()}"
        payload = {
            "scope_id": str(scope.id),
            "telegram_chat_id": user.telegram_user_id,
            "scope_binding": binding,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "valid_until": (end + timedelta(minutes=10)).isoformat(),
            "alert_count": stats.alert_count,
            "duration_seconds": stats.total_duration_seconds,
            "carried_over_count": stats.carried_over_count,
            "ongoing_count": stats.ongoing_count,
            "unknown_start_count": stats.unknown_start_count,
            "is_partial": not stats.history_complete,
            "duration_is_approximate": True,
        }
        statement = (
            insert(OutboxMessage)
            .values(
                event_key=key,
                aggregate_type="alert_scope",
                aggregate_id=str(scope.id),
                event_type="ALERT_DAILY_SUMMARY",
                payload=payload,
                available_at=now,
            )
            .on_conflict_do_nothing(index_elements=[OutboxMessage.event_key])
            .returning(OutboxMessage.id)
        )
        return int((await session.execute(statement)).scalar_one_or_none() is not None)


async def daily_report_deliverable(
    session: AsyncSession, row: OutboxMessage, now: datetime, *, lock: bool = False,
    expected_recipient: DeliveryRecipient | None = None,
) -> bool:
    """Final consent, territory binding and immutable 22:00 slot fence."""
    try:
        now = aware(now)
        payload = row.payload
        validate_report_payload(payload)
        identifier = UUID(row.aggregate_id)
        if row.event_type != "ALERT_DAILY_SUMMARY" or row.aggregate_type != "alert_scope":
            return False
        scope_query = select(AlertScope).where(AlertScope.id == identifier)
        scope = (await session.scalars(scope_query)).one_or_none()
        if scope is None:
            return False
        user = await session.get(User, scope.user_id, with_for_update=lock)
        scope = await session.get(AlertScope, identifier, with_for_update=lock)
        binding = scope_binding(scope)
        start, end = report_window(now)
        expiry = end + timedelta(minutes=10)
        if (
            not end <= now < expiry
            or user is None
            or user.mode not in MODES
            or binding is None
            or user.confirmed_region_id != scope.selected_region_id
            or payload.get("scope_binding") != binding
            or payload.get("scope_id") != str(scope.id)
            or payload.get("telegram_chat_id") != user.telegram_user_id
            or aware(datetime.fromisoformat(payload["period_start"])) != start
            or aware(datetime.fromisoformat(payload["period_end"])) != end
            or aware(datetime.fromisoformat(payload["valid_until"])) != expiry
            or row.event_key != f"alert-daily:{scope.id}:{binding_key(binding)}:{end.isoformat()}"
        ):
            return False
        if expected_recipient is not None and (
            expected_recipient.chat_id != user.telegram_user_id
            or expected_recipient.confirmed_region_id != user.confirmed_region_id
            or expected_recipient.confirmed_region_name != user.region_name
        ):
            return False
        # When lock=True the user consent row is held through transport by the
        # sender. Another generation must re-read this admission fence after
        # acquiring that lock, so two bindings cannot both send the same slot.
        if await _window_already_admitted(session, scope.id, end, excluding=row.id):
            return False
        return True
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return False
