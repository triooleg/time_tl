from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


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
