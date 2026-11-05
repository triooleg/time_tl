"""Secret-safe documented API transport. No HTML fallback or implicit live sync."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import replace
from email.utils import parsedate_to_datetime

import httpx
from pydantic import SecretStr

from time_tl.domain.power_outages.models import (
    PowerProvider,
    PowerScheduleSnapshot,
    PowerSourceKind,
)

from .alerts_energy_api_normalizer import (
    MAX_API_BYTES,
    normalize_api_schedules,
    parse_api_json,
    validate_binding,
    validate_supplier_registry,
)
from .base import (
    Clock,
    PowerFetchError,
    PowerFetchErrorCode,
    unavailable_snapshot,
    utc_now,
)

_PRIVATE_QUERY: ContextVar[bool] = ContextVar("energy_private_query", default=False)


class _PrivateAddressLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if _PRIVATE_QUERY.get():
            return False
        message = record.getMessage()
        return not ("alerts.energy" in message and any(path in message for path in (
            "/address-directory/", "/atus/settlement-search",
        )))


_PRIVATE_LOG_FILTER = _PrivateAddressLogFilter()


class AlertsEnergyApiClient:
    def __init__(self, *, api_key: SecretStr, clock: Clock = utc_now,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not isinstance(api_key, SecretStr) or not api_key.get_secret_value() or any(
            ord(char) < 33 or ord(char) > 126 for char in api_key.get_secret_value()
        ) or len(api_key.get_secret_value()) > 4096:
            raise ValueError("invalid_alerts_energy_api_key")
        self._key, self._clock, self._transport = api_key, clock, transport

    def _retry_after(self, value: str | None) -> float:
        if value is None:
            return 86400
        try:
            seconds = float(int(value)) if value.isascii() and value.isdecimal() else (
                parsedate_to_datetime(value) - self._clock()
            ).total_seconds()
            if not 0 <= seconds <= 86400:
                raise ValueError
            return seconds
        except (ValueError, OverflowError, TypeError):
            # Retain the quota signal for persisted shared cooldown handling.
            # Missing/malformed quota metadata must never become a short retry.
            return 86400

    async def _get(self, path: str, *, params: Mapping[str, str] | None = None) -> object:
        private = path.startswith("address-directory/") or path == "atus/settlement-search"
        logger = logging.getLogger("httpx")
        if _PRIVATE_LOG_FILTER not in logger.filters:
            logger.addFilter(_PRIVATE_LOG_FILTER)
        token = _PRIVATE_QUERY.set(private)
        try:
            return await self._request(path, params=params)
        finally:
            _PRIVATE_QUERY.reset(token)

    async def _request(self, path: str, *, params: Mapping[str, str] | None) -> object:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(15, connect=5), follow_redirects=False, trust_env=False,
                transport=self._transport,
            ) as client, client.stream(
                "GET", "https://alerts.energy/api/v1/" + path,
                params=params,
                headers={"Authorization": "Bearer " + self._key.get_secret_value(),
                         "Accept": "application/json"},
            ) as response:
                status = response.status_code
                if status in {401, 403}:
                    raise PowerFetchError(PowerFetchErrorCode.ACCESS_DENIED, status_code=status)
                if status == 429:
                    raise PowerFetchError(PowerFetchErrorCode.RATE_LIMITED, status_code=429,
                                          retry_after_seconds=self._retry_after(
                                              response.headers.get("Retry-After")))
                if 400 <= status <= 599:
                    raise PowerFetchError(PowerFetchErrorCode.HTTP_ERROR, status_code=status)
                if status != 200 or response.headers.get("content-type", "").split(";")[0] \
                        != "application/json":
                    raise PowerFetchError(PowerFetchErrorCode.INVALID_RESPONSE)
                declared = response.headers.get("content-length")
                if declared is not None and (not declared.isascii() or not declared.isdecimal()
                                             or int(declared) > MAX_API_BYTES):
                    raise PowerFetchError(PowerFetchErrorCode.INVALID_RESPONSE)
                payload = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(payload) + len(chunk) > MAX_API_BYTES:
                        raise PowerFetchError(PowerFetchErrorCode.INVALID_RESPONSE)
                    payload.extend(chunk)
                return parse_api_json(bytes(payload))
        except httpx.TimeoutException:
            raise PowerFetchError(PowerFetchErrorCode.TIMEOUT) from None
        except httpx.HTTPError:
            raise PowerFetchError(PowerFetchErrorCode.CONNECTION_ERROR) from None
        except (ValueError, RecursionError):
            raise PowerFetchError(PowerFetchErrorCode.INVALID_RESPONSE) from None

    async def check_access(self) -> bool:
        """Explicit quota-consuming diagnostic; never implicitly called by fetch."""
        value = await self._get("check")
        if type(value) is not dict or set(value) != {"ok"} or value["ok"] is not True:
            raise PowerFetchError(PowerFetchErrorCode.INVALID_RESPONSE)
        return True


class AlertsEnergyApiProvider(AlertsEnergyApiClient):
    def __init__(self, provider: PowerProvider, *, api_key: SecretStr, area_slug: str,
                 supplier_key: str, enabled: bool = False, clock: Clock = utc_now,
                 transport: httpx.AsyncBaseTransport | None = None,
                 stale_after_seconds: int = 1800) -> None:
        validate_binding(provider, area_slug, supplier_key)
        super().__init__(api_key=api_key, clock=clock, transport=transport)
        if type(enabled) is not bool or type(stale_after_seconds) is not int \
                or stale_after_seconds < 1:
            raise ValueError("invalid_alerts_energy_api_config")
        self._provider = provider
        self._area, self._supplier = area_slug, supplier_key
        self._enabled, self._ttl = enabled, stale_after_seconds
        self._lock = asyncio.Lock()

    @property
    def provider_id(self) -> PowerProvider:
        return self._provider

    async def supplier_registry(self) -> None:
        value = await self._get(f"source-registry/areas/{self._area}/suppliers")
        try:
            validate_supplier_registry(value, area_slug=self._area, supplier_key=self._supplier)
        except (ValueError, TypeError):
            raise PowerFetchError(PowerFetchErrorCode.INVALID_RESPONSE) from None

    async def fetch_schedules(self) -> PowerScheduleSnapshot:
        if not self._enabled:
            result = await unavailable_snapshot(
                self.provider_id, PowerFetchErrorCode.SOURCE_POLICY_UNVERIFIED, self._clock,
            )
            return replace(result, source_kind=PowerSourceKind.ALERTS_ENERGY_API)
        async with self._lock:
            await self.supplier_registry()
            value = await self._get(f"source-registry/areas/{self._area}/shutdowns")
            try:
                return normalize_api_schedules(
                    value, provider=self.provider_id, area_slug=self._area,
                    supplier_key=self._supplier, fetched_at=self._clock(),
                    stale_after_seconds=self._ttl,
                )
            except (ValueError, TypeError, RecursionError):
                raise PowerFetchError(PowerFetchErrorCode.INVALID_RESPONSE) from None
