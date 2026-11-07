"""Safe delivery of already-planned outbox messages."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Protocol
from zoneinfo import ZoneInfo

from time_tl.infrastructure.telegram.transport import (
    TelegramMessage,
    TelegramSendError,
    TelegramTransport,
)

KYIV = ZoneInfo("Europe/Kyiv")


@dataclass(frozen=True)
class DeliveryRecipient:
    chat_id: int
    confirmed_region_id: str | None = None
    confirmed_region_name: str | None = None


@dataclass(frozen=True)
class ClaimedOutboxMessage:
    message_id: str
    event_type: str
    payload: dict[str, object]
    attempts: int
    recipient: DeliveryRecipient | None = None


class SenderRepository(Protocol):
    async def claim_batch_skip_locked(
        self, now: datetime, *, limit: int
    ) -> tuple[ClaimedOutboxMessage, ...]: ...
    async def deliver_if_authorized(
        self,
        message_id: str,
        delivered_at: datetime,
        deliver: Callable[[], Awaitable[None]],
        *,
        expected_recipient: DeliveryRecipient,
    ) -> bool: ...
    async def mark_retry(
        self, message_id: str, *, available_at: datetime, error_code: str
    ) -> None: ...
    async def release_unattempted(
        self,
        message_ids: tuple[str, ...],
        *,
        claimed_at: datetime,
        available_at: datetime,
        error_code: str,
    ) -> None: ...
    async def mark_dead(self, message_id: str, *, error_code: str) -> None: ...


class JoinLinkResolver(Protocol):
    async def resolve(self, opaque_reference: str) -> str | None: ...


class DeliveryFailure(RuntimeError):
    def __init__(
        self, code: str, *, retryable: bool, retry_after_seconds: float | None = None
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


class SenderWorker:
    def __init__(
        self,
        *,
        repository: SenderRepository,
        transport: TelegramTransport,
        join_links: JoinLinkResolver | None = None,
        max_attempts: int = 6,
        messages_per_second: float = 20.0,
        batch_size: int = 50,
        transport_timeout_seconds: float = 8.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if (
            max_attempts < 1
            or messages_per_second <= 0
            or batch_size < 1
            or not 0 < transport_timeout_seconds <= 10
        ):
            raise ValueError("invalid sender limits")
        self._repository = repository
        self._transport = transport
        self._join_links = join_links
        self._max_attempts = max_attempts
        self._interval = 1.0 / messages_per_second
        self._batch_size = batch_size
        self._transport_timeout_seconds = transport_timeout_seconds
        self._clock = clock

    async def run_once(self, now: datetime, *, sleep: Callable[[float], Awaitable[None]]) -> int:
        if now.tzinfo is None:
            raise ValueError("worker clock must be timezone-aware")
        claimed = await self._repository.claim_batch_skip_locked(now, limit=self._batch_size)
        delivered = 0
        for index, record in enumerate(claimed):
            if index:
                await sleep(self._interval)
            priority_check = getattr(self._repository, "higher_priority_pending", None)
            probe_at = max(now, self._clock())
            if priority_check is not None and await priority_check(record.event_type, probe_at):
                # Release unattempted lower-priority rows. The next claim sees a
                # newly arrived air alert instead of draining an old weather batch.
                await self._repository.release_unattempted(
                    tuple(item.message_id for item in claimed[index:]),
                    claimed_at=now,
                    available_at=probe_at,
                    error_code="higher_priority_pending",
                )
                break
            try:
                message = await self._render(record, now)
                authorized = await self._repository.deliver_if_authorized(
                    record.message_id,
                    now,
                    partial(self._send_with_timeout, message),
                    expected_recipient=record.recipient or _recipient_from_payload(record.payload),
                )
            except TelegramSendError as exc:
                retry_at = await self._handle_failure(
                    record,
                    now,
                    DeliveryFailure(
                        exc.code,
                        retryable=exc.retryable,
                        retry_after_seconds=exc.retry_after_seconds,
                    ),
                )
            except DeliveryFailure as exc:
                retry_at = await self._handle_failure(record, now, exc)
            except Exception:
                retry_at = await self._handle_failure(
                    record, now, DeliveryFailure("transport_error", retryable=True)
                )
            else:
                if authorized:
                    delivered += 1
                retry_at = None
            if retry_at is not None:
                remaining_ids = tuple(item.message_id for item in claimed[index + 1 :])
                if remaining_ids:
                    await self._repository.release_unattempted(
                        remaining_ids,
                        claimed_at=now,
                        available_at=retry_at,
                        error_code="telegram_429_batch_deferred",
                    )
                break
        return delivered

    async def _send_with_timeout(self, message: TelegramMessage) -> None:
        try:
            await asyncio.wait_for(
                self._transport.send(message), timeout=self._transport_timeout_seconds
            )
        except TimeoutError as exc:
            raise DeliveryFailure("telegram_timeout", retryable=True) from exc

    async def _render(self, record: ClaimedOutboxMessage, now: datetime) -> TelegramMessage:
        payload = record.payload
        if record.event_type == "RETURN_TO_ACTIVE_LESSON":
            raw_valid_until = payload.get("valid_until")
            if not isinstance(raw_valid_until, str):
                raise DeliveryFailure("missing_delivery_expiry", retryable=False)
            try:
                valid_until = datetime.fromisoformat(raw_valid_until)
            except ValueError as exc:
                raise DeliveryFailure("invalid_delivery_expiry", retryable=False) from exc
            if valid_until.tzinfo is None:
                raise DeliveryFailure("invalid_delivery_expiry", retryable=False)
            if now >= valid_until:
                raise DeliveryFailure("delivery_expired", retryable=False)
        raw_chat_id = payload["telegram_chat_id"]
        if not isinstance(raw_chat_id, (int, str)):
            raise DeliveryFailure("invalid_chat_id", retryable=False)
        try:
            chat_id = int(raw_chat_id)
        except ValueError as exc:
            raise DeliveryFailure("invalid_chat_id", retryable=False) from exc
        title = _display_text(payload.get("lesson_title"), fallback="Урок")
        if record.event_type == "LESSON_REMINDER":
            starts_at = _lesson_time(payload, "starts_at")
            text = (
                "⏰ УРОК НЕЗАБАРОМ\n\n"
                f"📚 {title}\n"
                f"🕐 Початок: {starts_at}\n\n"
                "Час підготуватися до підключення."
            )
        elif record.event_type == "CALENDAR_REMINDER":
            name = _display_text(payload.get("calendar_name"), fallback="Особистий календар")
            event = _display_text(payload.get("event_title"), fallback="Подія")
            text = (
                "📅 ПОДІЯ НЕЗАБАРОМ\n\n"
                f"🗓 {name}\n"
                f"📌 {event}\n"
                f"🕐 Початок: {_lesson_time(payload, 'starts_at')}"
            )
        elif record.event_type == "RETURN_TO_ACTIVE_LESSON":
            region_name = _confirmed_region_name(payload)
            ends_at = _lesson_time(payload, "ends_at")
            text = (
                "📚 МОЖНА ПОВЕРНУТИСЯ ДО УРОКУ\n\n"
                f"📍 Територія: {region_name}\n"
                f"📖 Поточний урок: {title}\n"
                f"🕐 Триває до: {ends_at}\n\n"
                "Якщо це не суперечить поточним офіційним вказівкам, "
                "підключайтеся до уроку."
            )
        elif record.event_type == "ALERT_STARTED":
            region_name = _confirmed_region_name(payload)
            text = (
                "🚨 ПОВІТРЯНА ТРИВОГА\n\n"
                f"📍 Територія: {region_name}"
            )
        elif record.event_type == "ALL_CLEAR_CONFIRMED":
            from time_tl.application.alert_messages import completed_alert_duration

            region_name = _confirmed_region_name(payload)
            text = (
                "✅ ВІДБІЙ ПОВІТРЯНОЇ ТРИВОГИ\n\n"
                f"📍 Територія: {region_name}\n\n"
                f"{completed_alert_duration(payload)}"
            )
        elif record.event_type == "ALERT_DAILY_SUMMARY":
            from time_tl.application.alert_messages import render_alert_report

            try:
                text = render_alert_report(payload, _confirmed_region_name(payload))
            except (KeyError, ValueError, TypeError) as exc:
                raise DeliveryFailure("invalid_alert_report_payload", retryable=False) from exc
        elif record.event_type.startswith("POWER_MAP_"):
            from time_tl.infrastructure.db.power_map_notifications import render_map_outbox

            try:
                text = render_map_outbox(record.event_type, payload, now)
            except (KeyError, ValueError, TypeError) as exc:
                raise DeliveryFailure("invalid_power_map_payload", retryable=False) from exc
        elif record.event_type.startswith("WEATHER_"):
            from time_tl.application.weather_notifications import render_weather_notification

            if record.event_type not in {"WEATHER_DAILY", "WEATHER_EVENTS"}:
                raise DeliveryFailure("invalid_weather_payload", retryable=False)
            try:
                body = {
                    key: value
                    for key, value in payload.items()
                    if key
                    not in {"telegram_chat_id", "confirmed_region_id", "confirmed_region_name"}
                }
                if body.get("kind") != record.event_type:
                    raise ValueError("weather_kind_mismatch")
                text = render_weather_notification(body)
            except (KeyError, ValueError, TypeError) as exc:
                raise DeliveryFailure("invalid_weather_payload", retryable=False) from exc
        elif record.event_type in {
            "POWER_OUTAGE_UPCOMING",
            "POWER_RESTORATION_UPCOMING",
            "POWER_SCHEDULE_CHANGED",
        }:
            from time_tl.application.power_notification_worker import render_power_message

            try:
                text = render_power_message(record.event_type, payload, now)
            except (KeyError, ValueError, TypeError) as exc:
                raise DeliveryFailure("invalid_power_payload", retryable=False) from exc
        else:
            raise DeliveryFailure("unsupported_event_type", retryable=False)
        join_url = None
        reference = payload.get("join_link_ref")
        if reference and self._join_links is not None and record.event_type != "CALENDAR_REMINDER":
            join_url = await self._join_links.resolve(str(reference))
        return TelegramMessage(
            chat_id=chat_id,
            text=text,
            join_url=join_url,
            parse_mode="HTML" if record.event_type.startswith("WEATHER_") else None,
        )

    async def _handle_failure(
        self, record: ClaimedOutboxMessage, now: datetime, failure: DeliveryFailure
    ) -> datetime | None:
        next_attempt = record.attempts + 1
        rate_limited = failure.retryable and failure.code == "telegram_429"
        if not failure.retryable or (next_attempt >= self._max_attempts and not rate_limited):
            await self._repository.mark_dead(record.message_id, error_code=failure.code)
            return None
        exponential = min(900.0, float(2 ** min(next_attempt, 9)))
        delay = max(exponential, float(failure.retry_after_seconds or 0.0))
        available_at = now + timedelta(seconds=delay)
        await self._repository.mark_retry(
            record.message_id, available_at=available_at, error_code=failure.code
        )
        return available_at if rate_limited else None


def _display_text(raw: object, *, fallback: str) -> str:
    value = " ".join(str(raw or "").split())
    return value[:160] if value else fallback


def _confirmed_region_name(payload: dict[str, object]) -> str:
    raw_region = payload.get("confirmed_region_name")
    region = _display_text(raw_region, fallback="")
    if not region:
        raise DeliveryFailure("missing_confirmed_region", retryable=False)
    return region


def _lesson_time(payload: dict[str, object], key: str) -> str:
    raw_value = payload.get(key)
    if not isinstance(raw_value, str):
        raise DeliveryFailure(f"missing_{key}", retryable=False)
    try:
        parsed = datetime.fromisoformat(raw_value)
    except ValueError as exc:
        raise DeliveryFailure(f"invalid_{key}", retryable=False) from exc
    if parsed.tzinfo is None:
        raise DeliveryFailure(f"invalid_{key}", retryable=False)
    return parsed.astimezone(KYIV).strftime("%H:%M")


def _recipient_from_payload(payload: dict[str, object]) -> DeliveryRecipient:
    raw_chat_id = payload.get("telegram_chat_id")
    if not isinstance(raw_chat_id, (int, str)):
        raise DeliveryFailure("invalid_chat_id", retryable=False)
    try:
        chat_id = int(raw_chat_id)
    except ValueError as exc:
        raise DeliveryFailure("invalid_chat_id", retryable=False) from exc
    region_id = _display_text(payload.get("confirmed_region_id"), fallback="") or None
    region_name = _display_text(payload.get("confirmed_region_name"), fallback="") or None
    return DeliveryRecipient(chat_id, region_id, region_name)


def build_sender_worker(
    repository: SenderRepository,
    transport: TelegramTransport,
    *,
    join_links: JoinLinkResolver | None = None,
    max_attempts: int = 6,
    messages_per_second: float = 20.0,
    batch_size: int = 50,
    transport_timeout_seconds: float = 8.0,
) -> SenderWorker:
    """Exact runtime factory for wire_runtime."""
    return SenderWorker(
        repository=repository,
        transport=transport,
        join_links=join_links,
        max_attempts=max_attempts,
        messages_per_second=messages_per_second,
        batch_size=batch_size,
        transport_timeout_seconds=transport_timeout_seconds,
    )
