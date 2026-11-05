"""A single-account, absolute-deadline boundary for a Telegram power canary."""

from __future__ import annotations

import math
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    DeleteWebhook,
    EditMessageReplyMarkup,
    EditMessageText,
    GetMe,
    GetUpdates,
    SendChatAction,
    SendDocument,
    SendMessage,
    SendPhoto,
)
from aiogram.types import Message, Update

CHAT_METHODS = frozenset(
    {
        SendMessage,
        EditMessageText,
        EditMessageReplyMarkup,
        SendDocument,
        SendPhoto,
        DeleteMessage,
        SendChatAction,
    }
)
REDIRECT_FIELDS = (
    "business_connection_id",
    "inline_message_id",
    "message_thread_id",
    "direct_messages_topic_id",
    "receiver_user_id",
    "callback_query_id",
)


class PowerCanaryRejected(PermissionError):
    """A closed error: never includes a user, payload, target, path or API response."""

    def __init__(self) -> None:
        super().__init__("power_canary_request_rejected")


class PowerCanaryGuard:
    def __init__(
        self,
        allowed_ids: frozenset[int],
        *,
        clock: Callable[[], float] = monotonic,
        callbackcapacity: int = 256,
        callbackTTL: float = 300,
        expires_at: datetime | None = None,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if (
            type(allowed_ids) is not frozenset
            or len(allowed_ids) != 1
            or any(type(actor) is not int or not 0 < actor < 2**63 for actor in allowed_ids)
            or type(callbackcapacity) is not int
            or not 1 <= callbackcapacity <= 256
            or type(callbackTTL) not in {int, float}
            or not math.isfinite(callbackTTL)
            or not 0 < callbackTTL <= 300
            or not callable(clock)
            or not callable(wall_clock)
            or expires_at is not None
            and (
                not isinstance(expires_at, datetime)
                or expires_at.tzinfo is None
                or expires_at.utcoffset() is None
            )
        ):
            raise ValueError("invalid_power_canary_policy")
        self._allowed_ids = allowed_ids
        self._clock, self._wall_clock = clock, wall_clock
        self._capacity, self._ttl = callbackcapacity, float(callbackTTL)
        self._expires_at = expires_at.astimezone(UTC) if expires_at else None
        self._callbacks: OrderedDict[str, tuple[float, bool]] = OrderedDict()
        self._counts = {
            "updates_admitted": 0,
            "updates_dropped": 0,
            "requests_admitted": 0,
            "requests_denied": 0,
        }

    @property
    def allowed_ids(self) -> frozenset[int]:
        return self._allowed_ids

    def statistics(self) -> dict[str, int]:
        """Only bounded closed-name counters, without actor or payload labels."""
        return dict(self._counts)

    def _active(self) -> None:
        if self._expires_at is None:
            return
        stamp = self._wall_clock()
        if (
            not isinstance(stamp, datetime)
            or stamp.tzinfo is None
            or stamp.utcoffset() is None
            or stamp >= self._expires_at
        ):
            self._callbacks.clear()
            raise PowerCanaryRejected

    def _now(self) -> float:
        value = self._clock()
        if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
            self._callbacks.clear()
            raise PowerCanaryRejected
        return float(value)

    def _prune(self, now: float) -> None:
        for identifier, (expiry, _) in tuple(self._callbacks.items()):
            if now >= expiry or expiry - now > self._ttl:
                self._callbacks.pop(identifier, None)

    def _private(self, message: object, actor: int) -> bool:
        return (
            isinstance(message, Message)
            and message.chat.type == "private"
            and type(message.chat.id) is int
            and message.chat.id == actor
            and message.business_connection_id is None
            and message.sender_chat is None
        )

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        self._active()
        allowed = False
        if type(event) is Update and not event.model_extra:
            present = [
                name
                for name in Update.model_fields
                if name != "update_id" and getattr(event, name, None) is not None
            ]
            if present == ["message"]:
                message = event.message
                actor = message.from_user if isinstance(message, Message) else None
                allowed = bool(
                    actor is not None
                    and not actor.is_bot
                    and type(actor.id) is int
                    and actor.id in self.allowed_ids
                    and self._private(message, actor.id)
                )
            elif present == ["callback_query"]:
                callback = event.callback_query
                if callback is not None:
                    actor = callback.from_user
                    allowed = (
                        not actor.is_bot
                        and type(actor.id) is int
                        and actor.id in self.allowed_ids
                        and callback.inline_message_id is None
                        and self._private(callback.message, actor.id)
                        and 1 <= len(callback.id.encode("utf-8")) <= 256
                    )
                    if allowed:
                        now = self._now()
                        self._prune(now)
                        # Keep consumed tickets as bounded tombstones until expiry.
                        if callback.id in self._callbacks:
                            allowed = False
                        else:
                            while len(self._callbacks) >= self._capacity:
                                self._callbacks.popitem(last=False)
                            self._callbacks[callback.id] = now + self._ttl, False
        self._counts["updates_admitted" if allowed else "updates_dropped"] += 1
        if not allowed:
            return None
        return await handler(event, data)

    async def request(
        self, make_request: Callable[..., Awaitable[Any]], bot: Any, method: Any
    ) -> Any:
        self._active()
        permitted = False
        if not getattr(method, "model_extra", None):
            if type(method) in {GetMe, GetUpdates}:
                permitted = True
            elif type(method) is DeleteWebhook:
                permitted = method.drop_pending_updates is not True
            elif type(method) is AnswerCallbackQuery:
                now = self._now()
                self._prune(now)
                identifier = method.callback_query_id
                permitted = (
                    identifier in self._callbacks
                    and not self._callbacks[identifier][1]
                    and method.url is None
                    and method.cache_time in (None, 0)
                )
                if permitted:
                    self._callbacks[identifier] = self._callbacks[identifier][0], True
            elif type(method) in CHAT_METHODS:
                target = getattr(method, "chat_id", None)
                permitted = (
                    type(target) is int
                    and target in self.allowed_ids
                    and not any(
                        getattr(method, field, None) is not None for field in REDIRECT_FIELDS
                    )
                    and getattr(method, "allow_paid_broadcast", None) is not True
                )
                reply = getattr(method, "reply_parameters", None)
                if reply is not None and getattr(reply, "chat_id", None) is not None:
                    permitted = (
                        permitted
                        and type(reply.chat_id) is int
                        and reply.chat_id in self.allowed_ids
                    )
        self._counts["requests_admitted" if permitted else "requests_denied"] += 1
        if not permitted:
            raise PowerCanaryRejected
        return await make_request(bot, method)
