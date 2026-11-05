"""MET Locationforecast 2 compact, bounded and conditional HTTP only.

Contract: https://api.met.no/doc/TermsOfService
Schema: https://api.met.no/weatherapi/locationforecast/2.0/documentation
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from time_tl.domain.weather import ForecastHour, ForecastSnapshot, ForecastStatus, WeatherPoint

ENDPOINT = "https://api.met.no/weatherapi/locationforecast/2.0/compact"


class WeatherProviderError(Exception):
    def __init__(self, code: str, retry_at: datetime | None = None) -> None:
        self.code = code
        self.retry_at = retry_at
        super().__init__(code)


@dataclass(frozen=True)
class MetNorwaySettings:
    enabled: bool = False
    terms_approved: bool = False
    user_agent: str = ""
    contact: str = ""
    minimum_spacing_seconds: float = 1.0
    timeout_seconds: float = 15.0
    maximum_bytes: int = 2_000_000
    forecast_validity_hours: float = 8.0

    def validate(self) -> None:
        if not self.enabled or not self.terms_approved:
            raise WeatherProviderError("NOT_CONFIGURED")
        if (
            not self.user_agent.strip()
            or not self.contact.strip()
            or not ("@" in self.contact or self.contact.startswith("https://"))
            or any(c in self.user_agent + self.contact for c in "\r\n")
            or not 1 <= self.minimum_spacing_seconds <= 60
            or not 1 <= self.timeout_seconds <= 60
            or not 100 <= self.maximum_bytes <= 5_000_000
            or not 1 <= self.forecast_validity_hours <= 12
        ):
            raise WeatherProviderError("NOT_CONFIGURED")


def _coordinate(value: float) -> str:
    # MET asks for at most four decimals. Truncate, never expose extra precision.
    from decimal import ROUND_DOWN, Decimal

    return str(Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_DOWN))


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _date(raw: str) -> datetime:
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("naive_date")
    return value.astimezone(UTC)


def _header_date(raw: str | None, default: datetime) -> datetime:
    if raw is None:
        return default
    value = parsedate_to_datetime(raw)
    if value.tzinfo is None:
        raise ValueError("naive_date")
    return value.astimezone(UTC)


def normalize_met(
    raw: bytes,
    point: WeatherPoint,
    now: datetime,
    expires_at: datetime,
    last_modified: str | None = None,
    forecast_validity_hours: float = 8.0,
) -> ForecastSnapshot:
    try:
        data = json.loads(
            raw,
            object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")),
        )
        properties = data["properties"]
        meta = properties["meta"]
        units = meta["units"]
        updated = _date(meta["updated_at"])
        hours: list[ForecastHour] = []
        for row in properties["timeseries"]:
            instant = row["data"]["instant"]["details"]
            one = row["data"].get("next_1_hours", {})
            details = one.get("details", {})

            def number(key: str, source: dict[str, Any], unit: str) -> float | None:
                if key not in source:
                    return None
                if units.get(key) != unit:
                    raise ValueError("unexpected_unit")
                value = source[key]
                if isinstance(value, bool) or not isinstance(value, (float, int)):
                    raise ValueError("invalid_number")
                if not math.isfinite(value):
                    raise ValueError("invalid_number")
                return float(value)

            hour = ForecastHour(
                forecast_at=_date(row["time"]),
                temperature_c=number("air_temperature", instant, "celsius"),
                precipitation_mm=number("precipitation_amount", details, "mm"),
                precipitation_probability=number("probability_of_precipitation", details, "%"),
                wind_speed_ms=number("wind_speed", instant, "m/s"),
                wind_gust_ms=number("wind_speed_of_gust", instant, "m/s"),
                weather_code=one.get("summary", {}).get("symbol_code"),
                cloud_cover_percent=number("cloud_area_fraction", instant, "%"),
            )
            # No 6h-to-1h interpolation, snowfall conversion, or invented probability.
            hours.append(hour)
        if not hours or len(hours) > 500:
            raise ValueError("empty_or_oversized_forecast")
        if any(h.temperature_c is None for h in hours):
            status = ForecastStatus.PARTIAL
        elif updated > now + timedelta(minutes=5) or now - updated > timedelta(hours=24):
            status = ForecastStatus.STALE
        else:
            status = ForecastStatus.FRESH
        return ForecastSnapshot(
            "MET Norway",
            point.point_id,
            now,
            updated,
            min(
                now + timedelta(hours=forecast_validity_hours),
                hours[-1].forecast_at + timedelta(hours=1),
            ),
            tuple(hours),
            status,
            ENDPOINT,
            last_modified,
            expires_at,
        )
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        raise WeatherProviderError("INVALID_RESPONSE") from None


class MetNorwayProvider:
    def __init__(
        self,
        client: httpx.AsyncClient,
        settings: MetNorwaySettings,
        *,
        before_request: Callable[[WeatherPoint, datetime], Awaitable[None]] | None = None,
        after_response: Callable[[WeatherPoint, datetime | None, datetime], Awaitable[None]]
        | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.client = client
        self.settings = settings
        self.before_request = before_request
        self.after_response = after_response
        self.clock = clock
        self._lock = asyncio.Lock()
        self._next_request = 0.0
        self._retry_at: datetime | None = None

    async def fetch(
        self, point: WeatherPoint, now: datetime, previous: ForecastSnapshot | None = None
    ) -> ForecastSnapshot:
        self.settings.validate()
        if now.tzinfo is None:
            raise WeatherProviderError("INVALID_CLOCK")
        if previous is not None and (
            previous.point_id != point.point_id or previous.provider != "MET Norway"
        ):
            raise WeatherProviderError("CACHE_IDENTITY_MISMATCH")
        async with self._lock:
            if self._retry_at is not None and now < self._retry_at:
                raise WeatherProviderError("RATE_LIMITED", self._retry_at)
            if (
                previous is not None
                and previous.cache_expires_at is not None
                and previous.cache_expires_at > now
            ):
                return previous
            loop = asyncio.get_running_loop()
            await asyncio.sleep(max(0, self._next_request - loop.time()))
            self._next_request = loop.time() + self.settings.minimum_spacing_seconds
            headers = {"User-Agent": f"{self.settings.user_agent} {self.settings.contact}"}
            if previous is not None and previous.last_modified is not None:
                headers["If-Modified-Since"] = previous.last_modified
            try:
                if self.before_request is not None:
                    await self.before_request(point, self.clock())
                async with self.client.stream(
                    "GET",
                    ENDPOINT,
                    params={
                        "lat": _coordinate(point.latitude),
                        "lon": _coordinate(point.longitude),
                    },
                    headers=headers,
                    timeout=self.settings.timeout_seconds,
                    follow_redirects=False,
                ) as response:
                    if response.status_code == 429:
                        raw_retry = response.headers.get("Retry-After", "60")
                        try:
                            retry = now + timedelta(seconds=max(1, int(raw_retry)))
                        except ValueError:
                            retry = _header_date(raw_retry, now + timedelta(minutes=1))
                        self._retry_at = retry
                        raise WeatherProviderError("RATE_LIMITED", retry)
                    if response.status_code in (401, 403):
                        raise WeatherProviderError("AUTH_FAILED")
                    if response.status_code not in (200, 304):
                        raise WeatherProviderError("UNAVAILABLE")
                    expires = _header_date(response.headers.get("Expires"), now)
                    if self.after_response is not None:
                        await self.after_response(point, expires, self.clock())
                    if response.status_code == 304:
                        if previous is None or previous.last_modified is None:
                            raise WeatherProviderError("INVALID_RESPONSE")
                        return replace(
                            previous,
                            fetched_at=now,
                            cache_expires_at=expires,
                            expires_at=min(
                                now + timedelta(hours=self.settings.forecast_validity_hours),
                                previous.hours[-1].forecast_at + timedelta(hours=1),
                            ),
                        )
                    if "json" not in response.headers.get("Content-Type", ""):
                        raise WeatherProviderError("INVALID_RESPONSE")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > self.settings.maximum_bytes:
                            raise WeatherProviderError("RESPONSE_TOO_LARGE")
                    return normalize_met(
                        bytes(body),
                        point,
                        now,
                        expires,
                        response.headers.get("Last-Modified"),
                        self.settings.forecast_validity_hours,
                    )
            except httpx.HTTPError:
                raise WeatherProviderError("UNAVAILABLE") from None
            except (ValueError, OverflowError):
                raise WeatherProviderError("INVALID_RESPONSE") from None
