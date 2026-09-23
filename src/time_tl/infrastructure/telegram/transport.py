"""Telegram boundary with lazy client creation and no token-bearing logs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from time_tl.bot.safety import TelegramAllowlistGuard


@dataclass(frozen=True)
class TelegramMessage:
    chat_id: int
    text: str
    join_url: str | None = None


class TelegramTransport(Protocol):
    async def send(self, message: TelegramMessage) -> None: ...


class TelegramClient(Protocol):
    async def send_message(self, **kwargs: Any) -> Any: ...


class TelegramSendError(RuntimeError):
    def __init__(
        self, code: str, *, retryable: bool, retry_after_seconds: float | None = None
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


class AiogramTelegramTransport:
    """Lazy adapter; constructing it never creates a live Bot or performs I/O."""

    def __init__(self, client_factory: Callable[[], TelegramClient]) -> None:
        self._client_factory = client_factory
        self._client: TelegramClient | None = None

    async def send(self, message: TelegramMessage) -> None:
        if self._client is None:
            self._client = self._client_factory()
        reply_markup = None
        if message.join_url:
            reply_markup = {
                "inline_keyboard": [[{"text": "Підключитися до уроку", "url": message.join_url}]]
            }
        try:
            await self._client.send_message(
                chat_id=message.chat_id,
                text=message.text,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
        except Exception as exc:
            # Classify without importing aiogram at module import time.  Never
            # preserve provider exception text because it may contain request data.
            name = type(exc).__name__
            retry_after = getattr(exc, "retry_after", None)
            if retry_after is not None:
                raise TelegramSendError(
                    "telegram_429",
                    retryable=True,
                    retry_after_seconds=float(retry_after),
                ) from exc
            if name in {"TelegramBadRequest", "TelegramForbiddenError", "TelegramNotFound"}:
                raise TelegramSendError("telegram_permanent", retryable=False) from exc
            raise TelegramSendError("telegram_transport", retryable=True) from exc


class AllowlistedTelegramTransport:
    """Reject a staging delivery before the wrapped transport can perform I/O."""

    def __init__(self, transport: TelegramTransport, guard: TelegramAllowlistGuard) -> None:
        if not guard.restricted:
            raise ValueError("staging outbound guard requires a non-empty allowlist")
        self._transport = transport
        self._guard = guard

    async def send(self, message: TelegramMessage) -> None:
        try:
            self._guard.require_target(message.chat_id)
        except PermissionError as exc:
            raise TelegramSendError("telegram_target_not_allowlisted", retryable=False) from exc
        await self._transport.send(message)
