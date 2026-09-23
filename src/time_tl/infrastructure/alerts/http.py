"""HTTP primitives keep the provider adapter independent from aiohttp/httpx."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    timeout_seconds: float


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes = b""


class AsyncHttpTransport(Protocol):
    async def send(self, request: HttpRequest) -> HttpResponse: ...
