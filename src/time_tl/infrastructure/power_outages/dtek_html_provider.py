"""One bounded regional HTML fetch; live source gates are currently closed."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from types import MappingProxyType
from typing import Protocol

import httpx

from time_tl.domain.power_outages.models import PowerProvider, PowerScheduleSnapshot, SourceStatus

from .base import (
    Clock,
    PowerFetchError,
    PowerFetchErrorCode,
    PowerSourceGate,
    unavailable_snapshot,
    utc_now,
)
from .dtek_normalizer import normalize_dtek_fact
from .dtek_parser import MAX_HTML_BYTES, DtekDataError, parse_dtek_fact

DTEK_REGIONS = MappingProxyType({
    "dn": PowerProvider.DTEK,
    "k": PowerProvider.DTEK_KYIV,
    "kr": PowerProvider.DTEK_KYIV_REGION,
    "o": PowerProvider.DTEK_ODESA,
    "d": PowerProvider.DTEK_DONETSK,
})


@dataclass(frozen=True)
class HtmlResponse:
    status_code: int
    body: bytes
    retry_after_seconds: float | None = None


class HtmlTransport(Protocol):
    async def fetch(self, url: str) -> HtmlResponse: ...


def _retry_after(raw: str, clock: Clock = utc_now) -> float | None:
    if not raw:
        return None
    if len(raw) > 128:
        raise PowerFetchError(PowerFetchErrorCode.ACCESS_DENIED, permanent=True)
    if raw.isascii() and raw.isdecimal():
        retry = float(raw)
    else:
        try:
            target = parsedate_to_datetime(raw)
            if target.tzinfo is None:
                return None
            retry = max(0.0, (target - clock()).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None
    if not math.isfinite(retry) or retry > 86400:
        # Refuse rather than clamp and accidentally retry earlier than required.
        raise PowerFetchError(PowerFetchErrorCode.ACCESS_DENIED, permanent=True)
    return retry


async def read_html_response(response: httpx.Response, *, clock: Clock = utc_now) -> HtmlResponse:
    """Bounded decoding of an already supplied response; no network or approval."""
    retry = _retry_after(response.headers.get("Retry-After", ""), clock)
    if response.status_code != 200:
        return HtmlResponse(response.status_code, b"", retry)
    advertised = response.headers.get("Content-Length", "")
    if len(advertised) > 12:
        raise PowerFetchError(PowerFetchErrorCode.INVALID_RESPONSE)
    if advertised.isascii() and advertised.isdecimal() and int(advertised) > MAX_HTML_BYTES:
        raise PowerFetchError(PowerFetchErrorCode.INVALID_RESPONSE)
    body = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=65536):
        if len(body) + len(chunk) > MAX_HTML_BYTES:
            raise PowerFetchError(PowerFetchErrorCode.INVALID_RESPONSE)
        body.extend(chunk)
    return HtmlResponse(response.status_code, bytes(body), retry)


class HttpxHtmlTransport:
    """Ordinary HTTP only. Caller source admission is required before use.

    No proxy environment, UA impersonation, redirects, browser fallback, retries,
    cookie persistence or dynamic endpoint/address lookup is implemented.
    """

    def __init__(self, source_gate: PowerSourceGate) -> None:
        if type(source_gate) is not PowerSourceGate:
            raise ValueError("invalid_dtek_source_gate")
        self._source_gate = source_gate

    async def fetch(self, url: str) -> HtmlResponse:
        if (
            url not in {provider.official_url for provider in DTEK_REGIONS.values()}
            or url != self._source_gate.official_url
        ):
            raise ValueError("unapproved_dtek_url")
        if not self._source_gate.live_access_approved:
            raise PowerFetchError(PowerFetchErrorCode.SOURCE_POLICY_UNVERIFIED)
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", url) as response:
                return await read_html_response(response)


class DtekHtmlProvider:
    """Explicit region identity and an isolated SYNTHETIC transport test boundary.

    There is no public approval flag: current live metadata is immutable false.
    Only explicit offline fixture mode reaches the parser/normalizer today.
    """

    def __init__(
        self,
        provider_id: PowerProvider | None = None,
        *,
        region: str | None = None,
        clock: Clock = utc_now,
        transport: HtmlTransport | None = None,
        browser_adapter: HtmlTransport | None = None,
        offline_environment: str | None = None,
        fixture_kind: str | None = None,
        stale_after_seconds: int = 1800,
    ) -> None:
        if region is not None:
            if type(region) is not str or region not in DTEK_REGIONS:
                raise ValueError("invalid_dtek_region")
            selected = DTEK_REGIONS[region]
            if provider_id is not None and provider_id is not selected:
                raise ValueError("conflicting_dtek_region")
            provider_id = selected
        if provider_id is None:
            provider_id = PowerProvider.DTEK
        if provider_id not in DTEK_REGIONS.values() or not isinstance(provider_id, PowerProvider):
            raise ValueError("invalid_dtek_region")
        if transport is not None and browser_adapter is not None:
            raise ValueError("ambiguous_dtek_fetch_mode")
        if offline_environment is not None and (
            offline_environment not in {"local", "test", "simulation"}
            or fixture_kind != "SYNTHETIC"
            or transport is None and browser_adapter is None
        ):
            raise ValueError("invalid_dtek_synthetic_environment")
        if fixture_kind is not None and offline_environment is None:
            raise ValueError("missing_dtek_synthetic_environment")
        if type(stale_after_seconds) is not int or not 1 <= stale_after_seconds <= 86400:
            raise ValueError("invalid_dtek_stale_limit")
        self._provider_id, self._clock = provider_id, clock
        self._transport = transport or browser_adapter
        self._synthetic = offline_environment is not None
        self._stale_after = stale_after_seconds
        self._lock = asyncio.Lock()
        self._source_gate = PowerSourceGate(
            provider_id, PowerFetchErrorCode.SOURCE_POLICY_UNVERIFIED
        )

    @property
    def provider_id(self) -> PowerProvider:
        return self._provider_id

    @property
    def source_gate(self) -> PowerSourceGate:
        return self._source_gate

    async def fetch_schedules(self) -> PowerScheduleSnapshot:
        if not self._synthetic:
            # Enabling a feature or providing a transport cannot open G0.
            return await unavailable_snapshot(
                self.provider_id, self.source_gate.reason, self._clock
            )
        assert self._transport is not None
        async with self._lock:
            try:
                response = await asyncio.wait_for(
                    self._transport.fetch(self.provider_id.official_url), timeout=20
                )
                if type(response) is not HtmlResponse or type(response.status_code) is not int:
                    raise PowerFetchError(PowerFetchErrorCode.INVALID_RESPONSE)
                if response.status_code in {401, 403}:
                    raise PowerFetchError(
                        PowerFetchErrorCode.ACCESS_DENIED, status_code=response.status_code
                    )
                if response.status_code == 429:
                    raise PowerFetchError(
                        PowerFetchErrorCode.RATE_LIMITED, status_code=429,
                        retry_after_seconds=response.retry_after_seconds,
                    )
                if response.status_code != 200:
                    if 400 <= response.status_code <= 599:
                        raise PowerFetchError(
                            PowerFetchErrorCode.HTTP_ERROR, status_code=response.status_code
                        )
                    raise PowerFetchError(PowerFetchErrorCode.INVALID_RESPONSE)
                if type(response.body) is not bytes:
                    raise PowerFetchError(PowerFetchErrorCode.INVALID_RESPONSE)
                fact = parse_dtek_fact(response.body)
                return normalize_dtek_fact(
                    fact, provider_id=self.provider_id, fetched_at=self._clock(),
                    synthetic_profile=True, stale_after_seconds=self._stale_after,
                )
            except TimeoutError as exc:
                raise PowerFetchError(PowerFetchErrorCode.TIMEOUT) from exc
            except httpx.HTTPError as exc:
                raise PowerFetchError(PowerFetchErrorCode.CONNECTION_ERROR) from exc
            except DtekDataError:
                return PowerScheduleSnapshot(
                    self.provider_id, self._clock(), None, (), SourceStatus.INVALID,
                    "INVALID_DTEK_FACT",
                )


def inspect_html_fact(html: str | bytes) -> dict[str, object]:
    """Pure local diagnostic, not a fetch/approval/database/sender operation."""
    fact = parse_dtek_fact(html)
    return {
        "mapping_version": fact.mapping_version,
        "semantics_verified": False,
        "dates": [day.day.isoformat() for day in fact.days],
        "group_count": [len(day.groups) for day in fact.days],
        "hours_complete": all(len(group.hours) == 24 for day in fact.days for group in day.groups),
        "source_updated_at": fact.source_updated_at.isoformat(),
        "ready": False,
    }
