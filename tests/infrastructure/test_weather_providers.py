import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from time_tl.domain.weather import ForecastStatus, WeatherPoint
from time_tl.infrastructure.weather.met_norway import (
    MetNorwayProvider,
    MetNorwaySettings,
    WeatherProviderError,
    normalize_met,
)
from time_tl.infrastructure.weather.sources import OpenMeteoPermission, official_warnings

NOW = datetime(2026, 11, 5, 10, tzinfo=UTC)
POINT = WeatherPoint("p1", 48.123456, 34.123456)
BODY = Path("tests/fixtures/weather/met_compact.json").read_bytes()
SETTINGS = MetNorwaySettings(True, True, "TimeTL/1.0", "https://time.yoleotard.com")


def test_normalization_missing_parameters_not_zero_and_no_six_hour_interpolation() -> None:
    result = normalize_met(BODY, POINT, NOW, NOW + timedelta(hours=1))
    assert result.status == ForecastStatus.FRESH
    assert result.hours[0].precipitation_mm == 2
    assert result.hours[0].precipitation_probability is None
    assert result.hours[0].wind_gust_ms is None
    assert result.hours[0].snowfall_cm is None
    raw = json.loads(BODY)
    del raw["properties"]["timeseries"][0]["data"]["next_1_hours"]
    assert (
        normalize_met(json.dumps(raw).encode(), POINT, NOW, NOW).hours[0].precipitation_mm is None
    )


@pytest.mark.parametrize(
    "body",
    [b"{}", b"[]", b'{"properties":{},"properties":{}}', b'{"a":NaN}', b"[" * 1500 + b"]" * 1500],
)
def test_invalid_json_fail_closed(body: bytes) -> None:
    with pytest.raises(WeatherProviderError, match="INVALID_RESPONSE"):
        normalize_met(body, POINT, NOW, NOW)


def test_unknown_units_empty_partial_stale() -> None:
    raw = json.loads(BODY)
    raw["properties"]["meta"]["units"]["air_temperature"] = "fahrenheit"
    with pytest.raises(WeatherProviderError):
        normalize_met(json.dumps(raw).encode(), POINT, NOW, NOW)
    raw = json.loads(BODY)
    raw["properties"]["timeseries"] = []
    with pytest.raises(WeatherProviderError):
        normalize_met(json.dumps(raw).encode(), POINT, NOW, NOW)
    raw = json.loads(BODY)
    del raw["properties"]["timeseries"][0]["data"]["instant"]["details"]["air_temperature"]
    assert normalize_met(json.dumps(raw).encode(), POINT, NOW, NOW).status == ForecastStatus.PARTIAL
    assert normalize_met(BODY, POINT, NOW + timedelta(days=2), NOW).status == ForecastStatus.STALE


async def test_expiry_conditional_304_and_user_agent() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(
                200,
                content=BODY,
                headers={
                    "Content-Type": "application/json",
                    "Expires": "Thu, 05 Nov 2026 11:00:00 GMT",
                    "Last-Modified": "Thu, 05 Nov 2026 09:00:00 GMT",
                },
            )
        return httpx.Response(304, headers={"Expires": "Thu, 05 Nov 2026 12:00:00 GMT"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = MetNorwayProvider(client, SETTINGS)
        first = await provider.fetch(POINT, NOW)
        assert await provider.fetch(POINT, NOW, first) == first
        assert len(seen) == 1
        second = await provider.fetch(POINT, NOW + timedelta(hours=1), first)
        assert first.forecast_hash == second.forecast_hash
    assert seen[-1].headers["If-Modified-Since"] == first.last_modified
    assert "TimeTL" in seen[0].headers["User-Agent"]
    assert "https://time.yoleotard.com" in seen[0].headers["User-Agent"]
    assert len(str(seen[0].url.params["lat"]).split(".")[1]) == 4


@pytest.mark.parametrize(
    "status,code",
    [(429, "RATE_LIMITED"), (403, "AUTH_FAILED"), (503, "UNAVAILABLE"), (302, "UNAVAILABLE")],
)
async def test_http_failure_no_automatic_fallback(status: int, code: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, headers={"Retry-After": "120"})
        )
    ) as client:
        provider = MetNorwayProvider(client, SETTINGS)
        with pytest.raises(WeatherProviderError) as error:
            await provider.fetch(POINT, NOW)
        assert error.value.code == code
        if status == 429:
            with pytest.raises(WeatherProviderError) as blocked:
                await provider.fetch(POINT, NOW)
            assert blocked.value.retry_at == NOW + timedelta(seconds=120)


async def test_size_timeout_and_default_disabled() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, content=BODY, headers={"Content-Type": "application/json"}
            )
        )
    ) as client:
        with pytest.raises(WeatherProviderError, match="NOT_CONFIGURED"):
            await MetNorwayProvider(client, MetNorwaySettings()).fetch(POINT, NOW)
        with pytest.raises(WeatherProviderError, match="RESPONSE_TOO_LARGE"):
            await MetNorwayProvider(client, replace(SETTINGS, maximum_bytes=100)).fetch(POINT, NOW)

    def timeout(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("not exported")

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(WeatherProviderError, match="UNAVAILABLE"):
            await MetNorwayProvider(client, SETTINGS).fetch(POINT, NOW)


def test_other_sources_explicitly_unavailable() -> None:
    with pytest.raises(WeatherProviderError, match="NOT_CONFIGURED"):
        OpenMeteoPermission().require()
    with pytest.raises(WeatherProviderError, match="OFFICIAL_FEED_NOT_VERIFIED"):
        official_warnings()


async def test_final_request_authority_after_pacing_prevents_http_on_optout() -> None:
    import asyncio

    allowed = True
    calls = 0

    async def before(point: WeatherPoint, at: datetime) -> None:
        assert point == POINT and at == NOW + timedelta(seconds=1)
        if not allowed:
            raise WeatherProviderError("POINT_NOT_ACTIVE")

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=BODY, headers={"Content-Type": "application/json"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = MetNorwayProvider(
            client, SETTINGS, before_request=before, clock=lambda: NOW + timedelta(seconds=1)
        )
        provider._next_request = asyncio.get_running_loop().time() + 0.02
        task = asyncio.create_task(provider.fetch(POINT, NOW))
        await asyncio.sleep(0)
        allowed = False
        with pytest.raises(WeatherProviderError, match="POINT_NOT_ACTIVE"):
            await task
    assert calls == 0
