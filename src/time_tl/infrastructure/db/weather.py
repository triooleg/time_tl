"""Local weather persistence, explicit consent and restart-safe source permissions.

No HTTP, Telegram transport, credentials or user coordinates are read here.
Collection permission is committed before HTTP; cache publication independently
rechecks current approved demand under account/preference/catalogue locks.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from time_tl.domain.weather import (
    ForecastHour,
    ForecastRun,
    ForecastSnapshot,
    ForecastStatus,
    WeatherChangeDetector,
    WeatherPoint,
    WeatherThresholds,
    aware,
)
from time_tl.infrastructure.weather.met_norway import WeatherProviderError

from .geographic_coverage import PostgresActiveGeographicCoverage, read_coverage_in_session
from .models import (
    ActiveTerritoryCatalog,
    RuntimeSetting,
    User,
    UserWeatherPreference,
    WeatherChangeEvent,
    WeatherForecastHour,
    WeatherForecastRun,
    WeatherForecastSnapshot,
    WeatherLocation,
)
from .weather_configuration import SETTING, WeatherConfiguration

PROVIDER = "MET Norway"
SOURCE_STATE_KEY = "weather_source_permission:met_norway"
FAILURE_PREFIX = "weather_failure:"
POINT_IDENTITY_PREFIX = "weather_point_identity:"
POINT_REQUEST_PREFIX = "weather_point_request:"
AuthorityGuard = Callable[[AsyncSession, datetime], Awaitable[None]]


def _clock(value: datetime) -> datetime:
    aware(value)
    return value.astimezone(UTC)


def _identifier(value: str) -> UUID:
    try:
        return UUID(value)
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("invalid_weather_point") from error


@dataclass(frozen=True)
class WeatherPreferences:
    forecast_requested: bool = False
    weather_enabled: bool = False
    daily_forecast_enabled: bool = False
    daily_forecast_time: time = time(7)
    weather_changes_enabled: bool = True
    rain_alert_enabled: bool = True
    snow_alert_enabled: bool = True
    wind_alert_enabled: bool = True
    temperature_alert_enabled: bool = True
    frost_alert_enabled: bool = True
    notification_lead_minutes: int = 120
    revision: int = 0

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name in {"daily_forecast_time", "notification_lead_minutes", "revision"}:
                continue
            if type(value) is not bool:
                raise ValueError("invalid_weather_preference")
        clock = self.daily_forecast_time
        if (
            type(clock) is not time
            or clock.tzinfo is not None
            or clock.second
            or clock.microsecond
            or type(self.notification_lead_minutes) is not int
            or not 0 <= self.notification_lead_minutes <= 1440
            or type(self.revision) is not int
            or not 0 <= self.revision < 2**31
        ):
            raise ValueError("invalid_weather_preference")


def _preferences(row: UserWeatherPreference | None) -> WeatherPreferences:
    return (
        WeatherPreferences()
        if row is None
        else WeatherPreferences(
            **{item.name: getattr(row, item.name) for item in fields(WeatherPreferences)}
        )
    )


class PostgresWeatherPreferences:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        authority_guard: AuthorityGuard | None = None,
    ) -> None:
        self._factory, self._guard = factory, authority_guard

    @staticmethod
    def _actor(actor: int) -> None:
        if type(actor) is not int or actor <= 0:
            raise ValueError("invalid_weather_actor")

    async def get(self, actor: int) -> WeatherPreferences:
        self._actor(actor)
        async with self._factory() as session, session.begin():
            if self._guard is not None:
                await self._guard(session, datetime.now(UTC))
            user = await session.scalar(select(User.id).where(User.telegram_user_id == actor))
            if user is None:
                raise ValueError("weather_account_missing")
            return _preferences(await session.get(UserWeatherPreference, user))

    async def request_forecast(self, actor: int) -> WeatherPreferences:
        return await self._write(actor, None, {"forecast_requested": True})

    async def update(
        self, actor: int, *, expected_revision: int, **changes: Any
    ) -> WeatherPreferences:
        if type(expected_revision) is not int or not 0 <= expected_revision < 2**31:
            raise ValueError("invalid_weather_revision")
        return await self._write(actor, expected_revision, changes)

    async def toggle_notifications(
        self, actor: int, enabled: bool, *, expected_revision: int
    ) -> WeatherPreferences:
        return await self.update(
            actor, expected_revision=expected_revision, weather_enabled=enabled
        )

    async def disable_weather(self, actor: int, *, expected_revision: int) -> WeatherPreferences:
        return await self.update(
            actor,
            expected_revision=expected_revision,
            forecast_requested=False,
            weather_enabled=False,
        )

    async def _write(
        self, actor: int, expected: int | None, changes: dict[str, Any]
    ) -> WeatherPreferences:
        self._actor(actor)
        allowed = {item.name for item in fields(WeatherPreferences)} - {"revision"}
        if not changes or set(changes) - allowed:
            raise ValueError("invalid_weather_preference")
        # Validate before DB mutation, then validate again against persisted defaults.
        replace(WeatherPreferences(), **changes)
        async with self._factory() as session, session.begin():
            if self._guard is not None:
                await self._guard(session, datetime.now(UTC))
            user = await session.scalar(
                select(User.id).where(User.telegram_user_id == actor).with_for_update()
            )
            if user is None:
                raise ValueError("weather_account_missing")
            row = await session.scalar(
                select(UserWeatherPreference)
                .where(UserWeatherPreference.user_id == user)
                .with_for_update()
            )
            old = _preferences(row)
            if expected is not None and expected != old.revision:
                raise ValueError("weather_revision_conflict")
            result = replace(old, **changes)
            if result == old:
                return old
            result = replace(result, revision=old.revision + 1)
            if row is None:
                row = UserWeatherPreference(user_id=user, **asdict(result))
                session.add(row)
            else:
                for key, value in asdict(result).items():
                    setattr(row, key, value)
            await session.flush()
            if self._guard is not None:
                await self._guard(session, datetime.now(UTC))
            return result


class PostgresWeatherCoverage:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._coverage = PostgresActiveGeographicCoverage(factory)
        self._clock = clock or (lambda: datetime.now(UTC))

    async def active_weather_points(self) -> tuple[WeatherPoint, ...]:
        coverage = await self._coverage.snapshot(self._clock())
        return tuple(
            WeatherPoint(item.location.location_id, item.location.latitude, item.location.longitude)
            for item in coverage.weather_points
        )


async def _lock_demand(session: AsyncSession, point_id: UUID, now: datetime) -> WeatherPoint:
    # Account -> preference -> catalogue -> point. A final subscriber's opt-out,
    # territory switch or deletion cannot commit between this read and publish.
    users = (
        (
            await session.execute(
                select(User.id)
                .join(UserWeatherPreference)
                .where(
                    or_(
                        UserWeatherPreference.weather_enabled.is_(True),
                        UserWeatherPreference.forecast_requested.is_(True),
                    )
                )
                .order_by(User.id)
                .with_for_update(read=True, of=User)
            )
        )
        .scalars()
        .all()
    )
    if users:
        await session.execute(
            select(UserWeatherPreference.user_id)
            .where(UserWeatherPreference.user_id.in_(users))
            .order_by(UserWeatherPreference.user_id)
            .with_for_update(read=True)
        )
    await session.execute(
        select(ActiveTerritoryCatalog)
        .where(ActiveTerritoryCatalog.provider == "alerts.in.ua")
        .with_for_update(read=True)
    )
    await session.execute(
        select(WeatherLocation.id).where(WeatherLocation.id == point_id).with_for_update()
    )
    coverage = await read_coverage_in_session(session, now)
    for demand in coverage.weather_points:
        point = demand.location
        if point.location_id == str(point_id):
            return WeatherPoint(point.location_id, point.latitude, point.longitude)
    raise WeatherProviderError("DEMAND_REMOVED")


async def _previous(session: AsyncSession, point_id: UUID) -> ForecastSnapshot | None:
    row = await session.scalar(
        select(WeatherForecastSnapshot)
        .where(
            WeatherForecastSnapshot.weather_location_id == point_id,
            WeatherForecastSnapshot.provider == PROVIDER,
        )
        .order_by(WeatherForecastSnapshot.fetched_at.desc(), WeatherForecastSnapshot.id.desc())
        .limit(1)
    )
    if row is None:
        return None
    hours = (
        (
            await session.execute(
                select(WeatherForecastHour)
                .where(WeatherForecastHour.snapshot_id == row.id)
                .order_by(WeatherForecastHour.forecast_at)
            )
        )
        .scalars()
        .all()
    )
    return ForecastSnapshot(
        row.provider,
        str(point_id),
        row.fetched_at,
        row.provider_updated_at,
        row.expires_at,
        tuple(
            ForecastHour(**{item.name: getattr(hour, item.name) for item in fields(ForecastHour)})
            for hour in hours
        ),
        ForecastStatus(row.status),
        row.source_url,
        row.last_modified,
        row.cache_expires_at,
    )


class PostgresWeatherStorage:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        authority_guard: AuthorityGuard | None = None,
        thresholds: WeatherThresholds | None = None,
        expected_configuration_revision: int | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._factory = factory
        self._guard = authority_guard
        self._thresholds = thresholds
        self._expected_configuration_revision = expected_configuration_revision
        self._clock = clock or (lambda: datetime.now(UTC))
        self._run_id: UUID | None = None
        self._point_evidence: dict[UUID, str] = {}
        self._request_grants: dict[UUID, datetime] = {}
        self._authorized_points: set[UUID] = set()

    async def previous(self, point_id: str) -> ForecastSnapshot | None:
        identifier = _identifier(point_id)
        async with self._factory() as session, session.begin():
            row = await session.get(WeatherLocation, identifier)
            if row is None:
                return None
            identity = _point_identity(row)
            self._point_evidence[identifier] = identity
            return await _bound_previous(session, identifier, identity)

    async def begin_run(self, run_id: str, started_at: datetime, requested_locations: int) -> None:
        identifier, clock = _identifier(run_id), _clock(started_at)
        if type(requested_locations) is not int or requested_locations < 0:
            raise ValueError("invalid_weather_run")
        if self._run_id not in {None, identifier}:
            raise ValueError("weather_run_already_active")
        async with self._factory() as session, session.begin():
            await session.execute(
                insert(WeatherForecastRun)
                .values(
                    id=identifier,
                    provider=PROVIDER,
                    started_at=clock,
                    status="RUNNING",
                    requested_locations=requested_locations,
                    successful_locations=0,
                    failed_locations=0,
                )
                .on_conflict_do_nothing(index_elements=[WeatherForecastRun.id])
            )
            row = await session.get(WeatherForecastRun, identifier)
            if (
                row is None
                or row.provider != PROVIDER
                or row.started_at != clock
                or (row.requested_locations != requested_locations or row.completed_at is not None)
            ):
                raise ValueError("weather_run_identity_mismatch")
        self._run_id = identifier

    async def finish_run(self, run: ForecastRun) -> None:
        identifier = _identifier(run.run_id)
        started, completed = _clock(run.started_at), _clock(run.completed_at)
        counts = (run.requested_locations, run.successful_locations, run.failed_locations)
        if (
            any(type(v) is not int or v < 0 for v in counts)
            or sum(counts[1:]) > counts[0]
            or completed < started
            or run.status not in {"PASS", "PARTIAL", "INTERRUPTED"}
        ):
            raise ValueError("invalid_weather_run")
        async with self._factory() as session, session.begin():
            row = await session.scalar(
                select(WeatherForecastRun)
                .where(WeatherForecastRun.id == identifier)
                .with_for_update()
            )
            if (
                row is None
                or row.provider != PROVIDER
                or row.started_at != started
                or (row.requested_locations != run.requested_locations)
            ):
                raise ValueError("weather_run_identity_mismatch")
            if row.completed_at is not None:
                if (
                    row.completed_at,
                    row.status,
                    row.successful_locations,
                    row.failed_locations,
                ) != (completed, run.status, run.successful_locations, run.failed_locations):
                    raise ValueError("weather_run_already_finished")
            else:
                row.completed_at, row.status = completed, run.status
                row.successful_locations = run.successful_locations
                row.failed_locations = run.failed_locations
        if self._run_id == identifier:
            self._run_id = None

    async def save(self, snapshot: ForecastSnapshot) -> None:
        point_id, now = _identifier(snapshot.point_id), _clock(self._clock())
        if snapshot.provider != PROVIDER or not snapshot.usable(now):
            raise WeatherProviderError("INVALID_FORECAST")
        if self._run_id is None:
            raise ValueError("weather_run_required")
        async with self._factory() as session, session.begin():
            if self._guard is not None:
                await self._guard(session, now)
            config = await self._configuration(session)
            await _lock_demand(session, point_id, now)
            location = await session.get(WeatherLocation, point_id)
            if location is None or self._point_evidence.get(point_id) != _point_identity(location):
                raise WeatherProviderError("POINT_CHANGED")
            identity = _point_identity(location)
            previous = await _bound_previous(session, point_id, identity)
            if previous is not None and (
                snapshot.fetched_at < previous.fetched_at
                or snapshot.provider_updated_at < previous.provider_updated_at
            ):
                raise WeatherProviderError("OUT_OF_ORDER_FORECAST")
            row = await session.scalar(
                select(WeatherForecastSnapshot).where(
                    WeatherForecastSnapshot.weather_location_id == point_id,
                    WeatherForecastSnapshot.provider == PROVIDER,
                    WeatherForecastSnapshot.forecast_hash == snapshot.forecast_hash,
                )
            )
            if row is None:
                row = WeatherForecastSnapshot(
                    weather_location_id=point_id,
                    provider=PROVIDER,
                    forecast_run_id=self._run_id,
                    forecast_hash=snapshot.forecast_hash,
                    fetched_at=snapshot.fetched_at,
                    provider_updated_at=snapshot.provider_updated_at,
                    expires_at=snapshot.expires_at,
                    status=snapshot.status.value,
                    cache_expires_at=snapshot.cache_expires_at,
                    source_url=snapshot.source_url,
                    last_modified=snapshot.last_modified,
                )
                session.add(row)
                await session.flush()
                for hour in snapshot.hours:
                    session.add(WeatherForecastHour(snapshot_id=row.id, **asdict(hour)))
            row.forecast_run_id = self._run_id
            for name in (
                "fetched_at",
                "provider_updated_at",
                "expires_at",
                "cache_expires_at",
                "source_url",
                "last_modified",
            ):
                setattr(row, name, getattr(snapshot, name))
            row.status = snapshot.status.value
            await session.flush()
            known = frozenset(
                (
                    await session.execute(
                        select(WeatherChangeEvent.event_key).where(
                            WeatherChangeEvent.location_id == point_id,
                            WeatherChangeEvent.provider == PROVIDER,
                        )
                    )
                )
                .scalars()
                .all()
            )
            detector = WeatherChangeDetector(
                self._thresholds
                or WeatherThresholds(
                    rain_mm=config.rain_mm,
                    rain_probability=config.rain_probability,
                    gust_ms=config.gust_ms,
                    temperature_change_c=config.temperature_change_c,
                )
            )
            for event in detector.detect(previous, snapshot, now, known):
                if event.kind == "CANCELLATION":
                    await session.execute(
                        update(WeatherChangeEvent)
                        .where(
                            WeatherChangeEvent.location_id == point_id,
                            WeatherChangeEvent.provider == PROVIDER,
                            WeatherChangeEvent.event_type == event.event_type,
                            WeatherChangeEvent.expected_start == event.expected_start,
                            WeatherChangeEvent.expected_end == event.expected_end,
                            WeatherChangeEvent.status == "ACTIVE",
                        )
                        .values(status="CANCELLED")
                    )
                values = asdict(event)
                values.pop("point_id")
                await session.execute(
                    insert(WeatherChangeEvent)
                    .values(
                        location_id=point_id,
                        snapshot_id=row.id,
                        event_key=event.event_key,
                        **values,
                        status="CANCELLED" if event.kind == "CANCELLATION" else "ACTIVE",
                    )
                    .on_conflict_do_nothing(index_elements=[WeatherChangeEvent.event_key])
                )
            await session.execute(
                insert(RuntimeSetting)
                .values(
                    key=POINT_IDENTITY_PREFIX + str(point_id),
                    value={"identity": identity},
                    updated_by="weather-worker",
                )
                .on_conflict_do_update(
                    index_elements=[RuntimeSetting.key],
                    set_={"value": {"identity": identity}, "updated_at": now},
                )
            )
            if self._guard is not None:
                await self._guard(session, now)

    async def record_failure(self, point_id: str, code: str, now: datetime) -> None:
        identifier, clock = _identifier(point_id), _clock(now)
        if (
            not isinstance(code, str)
            or not code
            or len(code) > 64
            or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for char in code)
        ):
            raise ValueError("invalid_weather_failure")
        async with self._factory() as session, session.begin():
            await session.execute(
                insert(RuntimeSetting)
                .values(
                    key=FAILURE_PREFIX + str(identifier),
                    value={"code": code, "at": clock.isoformat()},
                    updated_by="weather-worker",
                )
                .on_conflict_do_update(
                    index_elements=[RuntimeSetting.key],
                    set_={
                        "value": {"code": code, "at": clock.isoformat()},
                        "updated_at": clock,
                        "updated_by": "weather-worker",
                    },
                )
            )

    async def source_retry_at(self, provider: str) -> datetime | None:
        self._provider(provider)
        async with self._factory() as session, session.begin():
            row = await session.get(RuntimeSetting, SOURCE_STATE_KEY)
            return _source_state(row.value)[0] if row is not None else None

    async def record_source_retry(self, provider: str, retry_at: datetime) -> None:
        self._provider(provider)
        retry = _clock(retry_at)
        async with self._factory() as session, session.begin():
            row = await _source_row(session)
            old, next_at = _source_state(row.value)
            row.value = _encode_source(max(old, retry) if old is not None else retry, next_at)

    @staticmethod
    def _provider(provider: str) -> None:
        if provider != PROVIDER:
            raise ValueError("invalid_weather_provider")

    async def _configuration(self, session: AsyncSession) -> WeatherConfiguration:
        row = await session.scalar(
            select(RuntimeSetting).where(RuntimeSetting.key == SETTING).with_for_update(read=True)
        )
        try:
            config = WeatherConfiguration.model_validate(row.value if row else {})
        except ValueError as error:
            raise WeatherProviderError("INVALID_WEATHER_CONFIGURATION") from error
        if not config.enabled or not config.terms_approved:
            raise WeatherProviderError("WEATHER_DISABLED")
        if self._expected_configuration_revision is not None and (
            config.revision != self._expected_configuration_revision
        ):
            raise WeatherProviderError("WEATHER_CONFIGURATION_CHANGED")
        return config

    async def claim_source_request(self, point: WeatherPoint, now: datetime) -> None:
        clock, identifier = _clock(now), _identifier(point.point_id)
        async with self._factory() as session, session.begin():
            if self._guard is not None:
                await self._guard(session, clock)
            await self._configuration(session)
            row = await _source_row(session)
            retry, next_at = _source_state(row.value)
            fences = [value for value in (retry, next_at) if value is not None]
            current = await _lock_demand(session, identifier, clock)
            if current != point:
                raise WeatherProviderError("POINT_CHANGED")
            location = await session.get(WeatherLocation, identifier)
            if location is None:
                raise WeatherProviderError("POINT_CHANGED")
            identity = _point_identity(location)
            previous = await _bound_previous(session, identifier, identity)
            if previous is not None and previous.cache_expires_at is not None:
                fences.append(previous.cache_expires_at)
            response = await session.get(RuntimeSetting, POINT_REQUEST_PREFIX + str(identifier))
            if response is not None:
                recorded = _response_expiry(response.value, identity)
                if recorded is not None:
                    fences.append(recorded)
            fence = max(fences, default=clock)
            if fence > clock:
                raise WeatherProviderError("REQUEST_DEFERRED", fence)
            row.value = _encode_source(retry, clock + timedelta(seconds=1))
            if self._guard is not None:
                await self._guard(session, clock)
        self._point_evidence[identifier] = identity
        self._request_grants[identifier] = clock

    async def authorize_source_request(self, point: WeatherPoint, now: datetime) -> None:
        clock, identifier = _clock(now), _identifier(point.point_id)
        grant = self._request_grants.get(identifier)
        if grant is None or not grant <= clock <= grant + timedelta(seconds=60):
            raise WeatherProviderError("REQUEST_GRANT_EXPIRED")
        async with self._factory() as session, session.begin():
            if self._guard is not None:
                await self._guard(session, clock)
            await self._configuration(session)
            state = await _source_row(session)
            retry, next_at = _source_state(state.value)
            if retry is not None and retry > clock:
                raise WeatherProviderError("RATE_LIMITED", retry)
            if next_at is None or next_at < grant + timedelta(seconds=1):
                raise WeatherProviderError("INVALID_SOURCE_PERMISSION")
            current = await _lock_demand(session, identifier, clock)
            location = await session.get(WeatherLocation, identifier)
            if (
                current != point
                or location is None
                or (self._point_evidence.get(identifier) != _point_identity(location))
            ):
                raise WeatherProviderError("POINT_CHANGED")
            if self._guard is not None:
                await self._guard(session, clock)
        self._request_grants.pop(identifier, None)
        self._authorized_points.add(identifier)

    async def record_source_response(
        self, point: WeatherPoint, cache_expires_at: datetime | None, now: datetime
    ) -> None:
        identifier, clock = _identifier(point.point_id), _clock(now)
        expiry = _clock(cache_expires_at) if cache_expires_at is not None else None
        if identifier not in self._authorized_points:
            raise WeatherProviderError("REQUEST_GRANT_REQUIRED")
        if expiry is not None and expiry > clock + timedelta(days=7):
            raise WeatherProviderError("INVALID_CACHE_EXPIRY")
        async with self._factory() as session, session.begin():
            if self._guard is not None:
                await self._guard(session, clock)
            location = await session.scalar(
                select(WeatherLocation)
                .where(WeatherLocation.id == identifier)
                .with_for_update(read=True)
            )
            identity = self._point_evidence.get(identifier)
            if (
                location is None
                or identity != _point_identity(location)
                or (WeatherPoint(str(location.id), location.latitude, location.longitude) != point)
            ):
                raise WeatherProviderError("POINT_CHANGED")
            await session.execute(
                insert(RuntimeSetting)
                .values(
                    key=POINT_REQUEST_PREFIX + str(identifier),
                    value={
                        "identity": identity,
                        "expires_at": expiry.isoformat() if expiry else None,
                    },
                    updated_by="weather-worker",
                )
                .on_conflict_do_update(
                    index_elements=[RuntimeSetting.key],
                    set_={
                        "value": {
                            "identity": identity,
                            "expires_at": expiry.isoformat() if expiry else None,
                        },
                        "updated_at": clock,
                    },
                )
            )
            if self._guard is not None:
                await self._guard(session, clock)
        self._authorized_points.discard(identifier)


def _response_expiry(value: Any, identity: str) -> datetime | None:
    if type(value) is not dict or set(value) != {"identity", "expires_at"}:
        raise WeatherProviderError("INVALID_SOURCE_PERMISSION")
    if value["identity"] != identity:
        return None
    if value["expires_at"] is None:
        return None
    try:
        if type(value["expires_at"]) is not str:
            raise ValueError("invalid")
        return _clock(datetime.fromisoformat(value["expires_at"]))
    except ValueError as error:
        raise WeatherProviderError("INVALID_SOURCE_PERMISSION") from error


def _point_identity(row: WeatherLocation) -> str:
    body = (
        str(row.id),
        row.region_id,
        row.katottg_code,
        row.latitude,
        row.longitude,
        row.coverage_type,
        row.enabled,
        row.provenance_url,
        row.provenance_sha256,
        row.provenance_version,
        row.approved_at.isoformat() if row.approved_at else None,
    )
    return hashlib.sha256(json.dumps(body, separators=(",", ":")).encode()).hexdigest()


async def _bound_previous(
    session: AsyncSession, point_id: UUID, identity: str
) -> ForecastSnapshot | None:
    binding = await session.get(RuntimeSetting, POINT_IDENTITY_PREFIX + str(point_id))
    if binding is None or binding.value != {"identity": identity}:
        return None
    return await _previous(session, point_id)


def _source_state(value: Any) -> tuple[datetime | None, datetime | None]:
    if (
        type(value) is not dict
        or set(value) != {"version", "retry_at", "next_request_at"}
        or (type(value["version"]) is not int or value["version"] != 1)
    ):
        raise WeatherProviderError("INVALID_SOURCE_PERMISSION")
    result: list[datetime | None] = []
    for key in ("retry_at", "next_request_at"):
        raw = value[key]
        if raw is None:
            result.append(None)
        elif type(raw) is str:
            try:
                result.append(_clock(datetime.fromisoformat(raw)))
            except ValueError as error:
                raise WeatherProviderError("INVALID_SOURCE_PERMISSION") from error
        else:
            raise WeatherProviderError("INVALID_SOURCE_PERMISSION")
    return result[0], result[1]


def _encode_source(retry: datetime | None, next_at: datetime | None) -> dict[str, Any]:
    return {
        "version": 1,
        "retry_at": retry.isoformat() if retry else None,
        "next_request_at": next_at.isoformat() if next_at else None,
    }


async def _source_row(session: AsyncSession) -> RuntimeSetting:
    await session.execute(
        insert(RuntimeSetting)
        .values(key=SOURCE_STATE_KEY, value=_encode_source(None, None), updated_by="weather-worker")
        .on_conflict_do_nothing(index_elements=[RuntimeSetting.key])
    )
    row = await session.scalar(
        select(RuntimeSetting).where(RuntimeSetting.key == SOURCE_STATE_KEY).with_for_update()
    )
    if row is None:
        raise WeatherProviderError("INVALID_SOURCE_PERMISSION")
    return row
