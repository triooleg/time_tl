"""Purpose-bound encrypted credential; status and audit never contain key material."""

from __future__ import annotations

import asyncio
import base64
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from time_tl.domain.power_outages.models import aware

from .models import AuditLog, RuntimeSetting

SETTING_KEY = "power_alerts_energy_api_credentials"
PURPOSE = "TimeTL/alerts-energy-api/credential/v1"
MAX_KEY_BYTES = 4096
CHECK_COOLDOWN = timedelta(seconds=60)


class EnergyApiConflict(ValueError):
    def __init__(self) -> None:
        super().__init__("energy_api_revision_changed")


class EnergyApiCooldown(ValueError):
    def __init__(self) -> None:
        super().__init__("energy_api_check_deferred")


class EnergyApiUnavailable(ValueError):
    def __init__(self) -> None:
        super().__init__("energy_api_unavailable")


class EnergyApiCheckStatus(StrEnum):
    NOT_CHECKED = "NOT_CHECKED"
    CHECKING = "CHECKING"
    VALID = "VALID"
    INVALID = "INVALID"
    UNAVAILABLE = "UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"


@dataclass(frozen=True)
class EnergyApiStatus:
    configured: bool
    revision: int
    last_checked_at: datetime | None
    check_status: EnergyApiCheckStatus


@dataclass(frozen=True)
class EnergyApiCheckResult:
    status: EnergyApiCheckStatus
    retry_after_seconds: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, EnergyApiCheckStatus) or self.status not in {
            EnergyApiCheckStatus.VALID,
            EnergyApiCheckStatus.INVALID,
            EnergyApiCheckStatus.UNAVAILABLE,
            EnergyApiCheckStatus.RATE_LIMITED,
        }:
            raise EnergyApiUnavailable
        retry = self.retry_after_seconds
        if retry is not None and (
            type(retry) not in {int, float} or not math.isfinite(retry) or not 0 <= retry <= 86400
        ):
            raise EnergyApiUnavailable


class EnergyApiChecker(Protocol):
    async def check(self, api_key: SecretStr) -> EnergyApiCheckResult: ...


def validate_api_key(value: SecretStr) -> str:
    if not isinstance(value, SecretStr):
        raise EnergyApiUnavailable
    raw = value.get_secret_value()
    if not 1 <= len(raw) <= MAX_KEY_BYTES or any(not 33 <= ord(char) <= 126 for char in raw):
        raise EnergyApiUnavailable
    return raw


class EnergyApiCredentialCodec:
    def __init__(self, seed: SecretStr | str) -> None:
        try:
            encoded = seed.get_secret_value() if isinstance(seed, SecretStr) else seed
            if type(encoded) is not str or len(encoded) != 44:
                raise ValueError
            material = base64.b64decode(encoded, altchars=b"-_", validate=True)
            if len(material) != 32:
                raise ValueError
            derived = HKDF(
                algorithm=hashes.SHA256(),
                length=32,
                salt=b"TimeTL/credential-encryption/v1",
                info=PURPOSE.encode("ascii"),
            ).derive(material)
            self._cipher = Fernet(base64.urlsafe_b64encode(derived))
        except (ValueError, TypeError):
            raise EnergyApiUnavailable from None

    def encrypt(self, api_key: SecretStr) -> str:
        value = {"purpose": PURPOSE, "version": 1, "api_key": validate_api_key(api_key)}
        return self._cipher.encrypt(json.dumps(value, separators=(",", ":")).encode()).decode()

    def decrypt(self, ciphertext: object) -> SecretStr:
        if type(ciphertext) is not str or not 1 <= len(ciphertext) <= 8192:
            raise EnergyApiUnavailable
        try:
            value = json.loads(self._cipher.decrypt(ciphertext.encode("ascii")))
            if type(value) is not dict or set(value) != {"purpose", "version", "api_key"}:
                raise ValueError
            if (
                value["purpose"] != PURPOSE
                or type(value["version"]) is not int
                or value["version"] != 1
            ):
                raise ValueError
            if type(value["api_key"]) is not str:
                raise ValueError
            key = SecretStr(value["api_key"])
            validate_api_key(key)
            return key
        except (InvalidToken, ValueError, UnicodeError, TypeError):
            raise EnergyApiUnavailable from None


def _empty() -> dict[str, object]:
    return {
        "version": 1,
        "revision": 0,
        "ciphertext": None,
        "check_status": EnergyApiCheckStatus.NOT_CHECKED.value,
        "last_checked_at": None,
        "last_attempt_at": None,
        "next_check_at": None,
        "check_id": None,
    }


def _state(row: RuntimeSetting | None) -> dict[str, object]:
    value = dict(row.value) if row is not None else _empty()
    if set(value) != set(_empty()) or type(value["version"]) is not int or value["version"] != 1:
        raise EnergyApiUnavailable
    revision = value["revision"]
    if type(revision) is not int or not 0 <= revision < 2**31 - 1:
        raise EnergyApiUnavailable
    ciphertext = value["ciphertext"]
    if ciphertext is not None and (type(ciphertext) is not str or not 1 <= len(ciphertext) <= 8192):
        raise EnergyApiUnavailable
    for key in ("last_checked_at", "last_attempt_at", "next_check_at"):
        if value[key] is not None:
            _date(value[key])
    try:
        EnergyApiCheckStatus(str(value["check_status"]))
    except ValueError:
        raise EnergyApiUnavailable from None
    return value


def _date(value: object) -> datetime:
    if type(value) is not str or len(value) > 40:
        raise EnergyApiUnavailable
    try:
        return aware(datetime.fromisoformat(value))
    except ValueError:
        raise EnergyApiUnavailable from None


def _status(value: dict[str, object]) -> EnergyApiStatus:
    return EnergyApiStatus(
        bool(value["ciphertext"]),
        int(str(value["revision"])),
        _date(value["last_checked_at"]) if value["last_checked_at"] else None,
        EnergyApiCheckStatus(str(value["check_status"])),
    )


class PostgresEnergyApiCredentials:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        seed: SecretStr | str,
        checker: EnergyApiChecker | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._factory, self._codec, self._checker = factory, EnergyApiCredentialCodec(seed), checker
        self._clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    async def _locked(session: AsyncSession) -> RuntimeSetting:
        await session.execute(
            insert(RuntimeSetting)
            .values(
                key=SETTING_KEY,
                value=_empty(),
                updated_by="system",
            )
            .on_conflict_do_nothing(index_elements=[RuntimeSetting.key])
        )
        return (
            await session.execute(
                select(RuntimeSetting)
                .where(
                    RuntimeSetting.key == SETTING_KEY,
                )
                .with_for_update()
            )
        ).scalar_one()

    @staticmethod
    def _audit(session: AsyncSession, actor: str, action: str, revision: int) -> None:
        if type(actor) is not str or not 1 <= len(actor) <= 80 or any(ord(c) < 32 for c in actor):
            raise EnergyApiUnavailable
        session.add(
            AuditLog(
                actor=actor,
                action=action,
                result="RECORDED",
                target_type="energy_api_credentials",
                target_id="alerts_energy",
                redacted_details={"revision": revision},
            )
        )

    async def status(self, now: datetime) -> EnergyApiStatus:
        aware(now)
        async with self._factory() as session:
            return _status(_state(await session.get(RuntimeSetting, SETTING_KEY)))

    async def load(self) -> SecretStr | None:
        async with self._factory() as session:
            value = _state(await session.get(RuntimeSetting, SETTING_KEY))
        return self._codec.decrypt(value["ciphertext"]) if value["ciphertext"] else None

    async def save(
        self,
        api_key: SecretStr,
        *,
        expected_revision: int,
        actor_id: str,
        now: datetime,
    ) -> EnergyApiStatus:
        ciphertext = self._codec.encrypt(api_key)
        return await self._change(ciphertext, expected_revision, actor_id, now)

    async def clear(
        self,
        *,
        expected_revision: int,
        actor_id: str,
        now: datetime,
    ) -> EnergyApiStatus:
        return await self._change(None, expected_revision, actor_id, now)

    async def _change(
        self,
        ciphertext: str | None,
        expected: int,
        actor: str,
        now: datetime,
    ) -> EnergyApiStatus:
        aware(now)
        async with self._factory() as session, session.begin():
            row = await self._locked(session)
            current = _state(row)
            if type(expected) is not int or current["revision"] != expected:
                raise EnergyApiConflict
            value = _empty()
            value.update(revision=expected + 1, ciphertext=ciphertext)
            for key in ("last_attempt_at", "next_check_at", "check_id"):
                value[key] = current[key]
            row.value, row.updated_by = value, actor
            self._audit(
                session,
                actor,
                "energy_api.saved" if ciphertext else "energy_api.cleared",
                expected + 1,
            )
            return _status(value)

    async def check(
        self,
        *,
        expected_revision: int,
        actor_id: str,
        now: datetime,
    ) -> EnergyApiStatus:
        checked_at = aware(now)
        if self._checker is None:
            raise EnergyApiUnavailable
        attempt_id = uuid4().hex
        async with self._factory() as session, session.begin():
            row = await self._locked(session)
            value = _state(row)
            if type(expected_revision) is not int or value["revision"] != expected_revision:
                raise EnergyApiConflict
            if not value["ciphertext"]:
                raise EnergyApiUnavailable
            attempted = value["last_attempt_at"]
            if attempted is not None and checked_at - _date(attempted) < CHECK_COOLDOWN:
                raise EnergyApiCooldown
            if value["next_check_at"] is not None and checked_at < _date(value["next_check_at"]):
                raise EnergyApiCooldown
            key = self._codec.decrypt(value["ciphertext"])
            value.update(
                check_id=attempt_id,
                last_attempt_at=checked_at.isoformat(),
                next_check_at=(checked_at + CHECK_COOLDOWN).isoformat(),
                check_status=EnergyApiCheckStatus.CHECKING.value,
            )
            row.value, row.updated_by = value, actor_id
            self._audit(session, actor_id, "energy_api.check_requested", expected_revision)
        try:
            result = await asyncio.wait_for(self._checker.check(key), timeout=8)
            if not isinstance(result, EnergyApiCheckResult):
                result = EnergyApiCheckResult(EnergyApiCheckStatus.UNAVAILABLE)
        except Exception:
            result = EnergyApiCheckResult(EnergyApiCheckStatus.UNAVAILABLE)
        completed = max(checked_at, aware(self._clock()))
        conflict = False
        async with self._factory() as session, session.begin():
            row = await self._locked(session)
            value = _state(row)
            conflict = value["revision"] != expected_revision or value["check_id"] != attempt_id
            previous_next = _date(value["next_check_at"]) if value["next_check_at"] else completed
            value["next_check_at"] = max(
                previous_next,
                completed + timedelta(seconds=max(60, result.retry_after_seconds or 0)),
            ).isoformat()
            if not conflict:
                value.update(
                    check_id=None,
                    last_checked_at=completed.isoformat(),
                    check_status=result.status.value,
                )
            elif value["check_id"] == attempt_id:
                value["check_id"] = None
            row.value, row.updated_by = value, actor_id
            self._audit(session, actor_id, "energy_api.checked", expected_revision)
            final = _status(value)
        if conflict:
            raise EnergyApiConflict
        return final
