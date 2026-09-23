"""Concrete async HTTP transport for the provider adapter."""

from __future__ import annotations

import httpx

from time_tl.infrastructure.alerts.http import HttpRequest, HttpResponse


class ProviderResponseTooLargeError(OSError):
    """The provider response exceeded the transport's safe body limit."""


class HttpxTransport:
    """One bounded request per call; response bodies are never logged here."""

    DEFAULT_MAX_RESPONSE_BYTES = 4_096

    def __init__(
        self,
        *,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        self._max_response_bytes = max_response_bytes
        self._transport = transport

    async def send(self, request: HttpRequest) -> HttpResponse:
        try:
            async with httpx.AsyncClient(
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            ) as client:
                async with client.stream(
                    request.method,
                    request.url,
                    headers=dict(request.headers),
                    timeout=httpx.Timeout(request.timeout_seconds),
                ) as response:
                    self._reject_oversized_declared_body(response.headers)
                    body = await self._read_bounded_body(response)
        except httpx.TimeoutException as exc:
            raise TimeoutError("provider request timed out") from exc
        except httpx.RequestError as exc:
            raise OSError("provider transport failed") from exc
        return HttpResponse(
            status=response.status_code,
            headers=dict(response.headers),
            body=body,
        )

    def _reject_oversized_declared_body(self, headers: httpx.Headers) -> None:
        value = headers.get("content-length")
        if value is None:
            return
        try:
            declared_size = int(value)
        except ValueError as exc:
            raise OSError("provider returned an invalid content length") from exc
        if declared_size < 0:
            raise OSError("provider returned an invalid content length")
        if declared_size > self._max_response_bytes:
            raise ProviderResponseTooLargeError("provider response exceeded the safe size limit")

    async def _read_bounded_body(self, response: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        received = 0
        async for chunk in response.aiter_bytes():
            received += len(chunk)
            if received > self._max_response_bytes:
                raise ProviderResponseTooLargeError(
                    "provider response exceeded the safe size limit"
                )
            chunks.append(chunk)
        return b"".join(chunks)
