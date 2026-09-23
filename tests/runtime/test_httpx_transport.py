from __future__ import annotations

import httpx
import pytest

from time_tl.domain.alerts import SourceFailureKind
from time_tl.infrastructure.alerts.http import HttpRequest
from time_tl.infrastructure.alerts.provider import OblastAlertsInUaAdapter
from time_tl.runtime.httpx_transport import HttpxTransport, ProviderResponseTooLargeError


def _request() -> HttpRequest:
    return HttpRequest(
        method="GET",
        url="https://api.alerts.in.ua/v1/iot/active_air_raid_alerts_by_oblast.json",
        headers={"Authorization": "Bearer test-token"},
        timeout_seconds=1.0,
    )


def _transport(response: httpx.Response, *, max_response_bytes: int = 32) -> HttpxTransport:
    async def handler(_: httpx.Request) -> httpx.Response:
        return response

    return HttpxTransport(
        max_response_bytes=max_response_bytes,
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_accepts_valid_response_within_limit() -> None:
    body = b'"NNNNNNNNNNNNNNNNNNNNNNNNNNN"'
    response = httpx.Response(
        200,
        headers={"Content-Length": str(len(body)), "Last-Modified": "marker"},
        content=body,
    )

    result = await _transport(response).send(_request())

    assert result.status == 200
    assert result.body == body
    assert result.headers["last-modified"] == "marker"


@pytest.mark.asyncio
async def test_accepts_response_without_content_length_when_actual_body_is_bounded() -> None:
    response = httpx.Response(200, content=b'"NNN"')
    del response.headers["content-length"]

    result = await _transport(response, max_response_bytes=8).send(_request())

    assert result.body == b'"NNN"'


@pytest.mark.asyncio
async def test_rejects_oversized_declared_content_length_before_reading_body() -> None:
    response = httpx.Response(200, headers={"Content-Length": "33"}, content=b"small")

    with pytest.raises(ProviderResponseTooLargeError, match="safe size limit"):
        await _transport(response, max_response_bytes=32).send(_request())


@pytest.mark.asyncio
async def test_rejects_oversized_actual_body_with_forged_small_content_length() -> None:
    response = httpx.Response(200, headers={"Content-Length": "1"}, content=b"x" * 33)

    with pytest.raises(ProviderResponseTooLargeError, match="safe size limit"):
        await _transport(response, max_response_bytes=32).send(_request())


@pytest.mark.asyncio
async def test_oversized_body_maps_to_source_failure_in_provider_adapter() -> None:
    response = httpx.Response(
        200,
        headers={"Content-Length": "33", "Last-Modified": "marker"},
        content=b"x" * 33,
    )
    adapter = OblastAlertsInUaAdapter(
        token="test-token",
        transport=_transport(response, max_response_bytes=32),
    )

    result = await adapter.fetch(fetch_revision=1, last_modified=None)

    assert result.snapshot is None
    assert result.failure is not None
    assert result.failure.kind is SourceFailureKind.TIMEOUT
