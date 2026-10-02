from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from copy import copy
from dataclasses import dataclass
from typing import Any

from aiogram.exceptions import DataNotDictLikeError
from aiogram.fsm.storage.base import StateType, StorageKey
from aiogram.fsm.storage.memory import MemoryStorage, MemoryStorageRecord


@dataclass(frozen=True)
class TelegramAllowlistGuard:
    """Policy shared by inbound staging updates and outbound delivery."""

    allowed_ids: frozenset[int]

    @classmethod
    def from_ids(cls, values: tuple[int, ...]) -> TelegramAllowlistGuard:
        return cls(frozenset(values))

    @property
    def restricted(self) -> bool:
        return bool(self.allowed_ids)

    def allows(self, telegram_id: int | None) -> bool:
        return not self.restricted or (telegram_id is not None and telegram_id in self.allowed_ids)

    def require_target(self, telegram_id: int) -> None:
        if not self.allows(telegram_id):
            raise PermissionError("Telegram target is outside TEST_CHAT_ALLOWLIST")


class AiogramAllowlistMiddleware:
    """Drop unauthorized staging updates before a handler can mutate state."""

    def __init__(self, guard: TelegramAllowlistGuard) -> None:
        self.guard = guard

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        actor = getattr(event, "from_user", None)
        actor_id = getattr(actor, "id", None)
        chat = getattr(event, "chat", None)
        if chat is None:
            chat = getattr(getattr(event, "message", None), "chat", None)
        chat_id = getattr(chat, "id", None)
        if not self.guard.allows(actor_id) or not self.guard.allows(chat_id):
            answer = getattr(event, "answer", None)
            if callable(answer) and event.__class__.__name__ == "CallbackQuery":
                await answer(
                    "Цей тестовий бот доступний лише дозволеним акаунтам.", show_alert=True
                )
            return None
        return await handler(event, data)


class BoundedMemoryStorage(MemoryStorage):
    """In-process FSM storage with deterministic TTL and LRU eviction."""

    def __init__(
        self,
        *,
        max_records: int = 2_048,
        idle_ttl_seconds: float = 3_600.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_records < 1:
            raise ValueError("FSM max_records must be positive")
        if idle_ttl_seconds <= 0:
            raise ValueError("FSM idle TTL must be positive")
        super().__init__()
        self._max_records = max_records
        self._idle_ttl_seconds = idle_ttl_seconds
        self._monotonic = monotonic
        self._last_access: OrderedDict[StorageKey, float] = OrderedDict()
        self._lock = asyncio.Lock()

    @property
    def tracked_records(self) -> int:
        return len(self.storage)

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        async with self._lock:
            self._prepare(key, create=state is not None)
            await super().set_state(key, state)
            self._discard_empty(key)

    async def get_state(self, key: StorageKey) -> str | None:
        async with self._lock:
            self._prepare(key, create=False)
            if key not in self.storage:
                return None
            state = await super().get_state(key)
            self._discard_empty(key)
            return state

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        async with self._lock:
            self._prepare(key, create=bool(data))
            await super().set_data(key, data)
            self._discard_empty(key)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        async with self._lock:
            self._prepare(key, create=False)
            if key not in self.storage:
                return {}
            data = await super().get_data(key)
            self._discard_empty(key)
            return data

    async def update_data(self, key: StorageKey, data: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise DataNotDictLikeError(
                f"Data must be a dict or dict-like object, got {type(data).__name__}"
            )
        async with self._lock:
            self._prepare(key, create=bool(data))
            if key not in self.storage:
                return {}
            self.storage[key].data.update(data)
            updated = self.storage[key].data.copy()
            self._discard_empty(key)
            return updated

    async def get_value(
        self,
        storage_key: StorageKey,
        dict_key: str,
        default: Any | None = None,
    ) -> Any | None:
        data = await self.get_data(storage_key)
        return copy(data.get(dict_key, default))

    async def close(self) -> None:
        async with self._lock:
            self.storage.clear()
            self._last_access.clear()
            await super().close()

    def _prepare(self, key: StorageKey, *, create: bool) -> None:
        now = self._monotonic()
        while self._last_access:
            oldest, last_seen = next(iter(self._last_access.items()))
            if now - last_seen < self._idle_ttl_seconds:
                break
            self._last_access.popitem(last=False)
            self.storage.pop(oldest, None)
        if key in self._last_access:
            self._last_access.pop(key)
        elif key not in self.storage and create:
            while len(self.storage) >= self._max_records and self._last_access:
                oldest, _ = self._last_access.popitem(last=False)
                self.storage.pop(oldest, None)
            self.storage[key] = MemoryStorageRecord()
        elif key not in self.storage:
            return
        self._last_access[key] = now

    def _discard_empty(self, key: StorageKey) -> None:
        record = self.storage.get(key)
        if record is not None and record.state is None and not record.data:
            self.storage.pop(key, None)
            self._last_access.pop(key, None)


@dataclass
class _TokenBucket:
    tokens: float
    updated_at: float
    last_seen_at: float


class BoundedTelegramIngressGuard:
    """Bound public update load without retaining an unbounded attacker key set."""

    def __init__(
        self,
        *,
        per_user_rate: float = 1.0,
        per_user_burst: int = 6,
        global_rate: float = 20.0,
        global_burst: int = 40,
        max_users: int = 256,
        idle_ttl_seconds: float = 300.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if per_user_rate <= 0 or global_rate <= 0:
            raise ValueError("ingress rates must be positive")
        if per_user_burst < 1 or global_burst < 1 or max_users < 1:
            raise ValueError("ingress capacities must be positive")
        if idle_ttl_seconds <= 0:
            raise ValueError("ingress idle TTL must be positive")
        self._per_user_rate = per_user_rate
        self._per_user_burst = float(per_user_burst)
        self._global_rate = global_rate
        self._global_burst = float(global_burst)
        self._max_users = max_users
        self._idle_ttl_seconds = idle_ttl_seconds
        self._monotonic = monotonic
        now = monotonic()
        self._global = _TokenBucket(self._global_burst, now, now)
        self._users: OrderedDict[int, _TokenBucket] = OrderedDict()

    @property
    def tracked_users(self) -> int:
        return len(self._users)

    def allows(self, telegram_user_id: int | None) -> bool:
        if telegram_user_id is None:
            return False
        now = self._monotonic()
        self._prune(now)
        bucket = self._users.pop(telegram_user_id, None)
        if bucket is None:
            while len(self._users) >= self._max_users:
                self._users.popitem(last=False)
            bucket = _TokenBucket(self._per_user_burst, now, now)
        allowed = self._consume(
            bucket,
            now=now,
            rate=self._per_user_rate,
            capacity=self._per_user_burst,
        )
        bucket.last_seen_at = now
        self._users[telegram_user_id] = bucket
        if not allowed:
            return False
        if self._consume(
            self._global,
            now=now,
            rate=self._global_rate,
            capacity=self._global_burst,
        ):
            return True
        bucket.tokens = min(self._per_user_burst, bucket.tokens + 1.0)
        return False

    def _prune(self, now: float) -> None:
        while self._users:
            _key, bucket = next(iter(self._users.items()))
            if now - bucket.last_seen_at < self._idle_ttl_seconds:
                break
            self._users.popitem(last=False)

    @staticmethod
    def _consume(bucket: _TokenBucket, *, now: float, rate: float, capacity: float) -> bool:
        elapsed = max(0.0, now - bucket.updated_at)
        bucket.tokens = min(capacity, bucket.tokens + elapsed * rate)
        bucket.updated_at = now
        if bucket.tokens < 1.0:
            return False
        bucket.tokens -= 1.0
        return True


class AiogramIngressSafetyMiddleware:
    """Allow bounded private-chat traffic before any handler or DB mutation."""

    PRIVATE_REDIRECT = "Для безпеки відкрийте приватний чат із ботом і надішліть /start."

    def __init__(
        self,
        guard: BoundedTelegramIngressGuard,
        *,
        notice_guard: BoundedTelegramIngressGuard | None = None,
    ) -> None:
        self.guard = guard
        self.notice_guard = notice_guard or BoundedTelegramIngressGuard(
            per_user_rate=1 / 60,
            per_user_burst=1,
            global_rate=1.0,
            global_burst=3,
            max_users=256,
            idle_ttl_seconds=300,
        )

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        actor = getattr(event, "from_user", None)
        actor_id = getattr(actor, "id", None)
        chat = getattr(event, "chat", None)
        if chat is None:
            chat = getattr(getattr(event, "message", None), "chat", None)
        chat_id = getattr(chat, "id", None)
        raw_chat_type = getattr(chat, "type", None)
        chat_type = getattr(raw_chat_type, "value", raw_chat_type)
        if chat_type != "private" or actor_id != chat_id:
            await self._redirect_once(event, chat_type=chat_type, chat_id=chat_id)
            return None
        if not self.guard.allows(actor_id):
            return None
        return await handler(event, data)

    async def _redirect_once(self, event: Any, *, chat_type: Any, chat_id: int | None) -> None:
        if chat_type == "channel" or not self.notice_guard.allows(chat_id):
            return
        answer = getattr(event, "answer", None)
        if not callable(answer):
            return
        if event.__class__.__name__ == "CallbackQuery":
            await answer(self.PRIVATE_REDIRECT, show_alert=False)
            return
        await answer(self.PRIVATE_REDIRECT)
