"""Credential isolation and explicit diagnostic wiring; no network/database calls."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import SecretStr

from time_tl.admin.repository import LocalAdminRepository
from time_tl.infrastructure.db.admin import AsyncPostgresAdminRepository
from time_tl.infrastructure.db.energy_api_credentials import SETTING_KEY, EnergyApiCheckStatus
from time_tl.infrastructure.power_outages import alerts_energy_api
from time_tl.infrastructure.power_outages.base import PowerFetchError, PowerFetchErrorCode
from time_tl.runtime.power import AlertsEnergyAccessChecker, PostgresPowerBotPort


@pytest.mark.asyncio
async def test_generic_postgres_settings_cannot_read_or_overwrite_encrypted_credential():
    factory = Mock(side_effect=AssertionError("database must not be reached"))
    repository = AsyncPostgresAdminRepository(factory, ip_hash_key=b"x" * 32)
    with pytest.raises(ValueError, match="sensitive values"):
        await repository.get_setting(SETTING_KEY)
    with pytest.raises(ValueError, match="sensitive values"):
        await repository.set_settings({SETTING_KEY: {"ciphertext": "synthetic"}}, "admin", "save")
    factory.assert_not_called()


def test_generic_sqlite_settings_cannot_access_credential_record():
    repository = object.__new__(LocalAdminRepository)
    with pytest.raises(ValueError, match="sensitive values"):
        repository.get_setting(SETTING_KEY)
    with pytest.raises(ValueError, match="sensitive values"):
        repository.set_settings({SETTING_KEY: {}}, "admin", "save")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "http_status,expected",
    [
        (401, EnergyApiCheckStatus.INVALID),
        (403, EnergyApiCheckStatus.UNAVAILABLE),
        (429, EnergyApiCheckStatus.RATE_LIMITED),
    ],
)
async def test_diagnostic_maps_closed_status_and_preserves_retry_after(
    monkeypatch, http_status, expected
):
    error = PowerFetchError(
        PowerFetchErrorCode.RATE_LIMITED
        if http_status == 429
        else PowerFetchErrorCode.ACCESS_DENIED,
        status_code=http_status,
        retry_after_seconds=3600 if http_status == 429 else None,
    )
    client = Mock(check_access=AsyncMock(side_effect=error))
    monkeypatch.setattr(alerts_energy_api, "AlertsEnergyApiClient", Mock(return_value=client))
    result = await AlertsEnergyAccessChecker().check(SecretStr("synthetic-key"))
    assert result.status is expected
    assert result.retry_after_seconds == (3600 if http_status == 429 else None)
    client.check_access.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_diagnostic_uses_standalone_check_without_operator_or_polling(monkeypatch):
    client = Mock(check_access=AsyncMock(return_value=True))
    constructor = Mock(return_value=client)
    monkeypatch.setattr(alerts_energy_api, "AlertsEnergyApiClient", constructor)
    secret = SecretStr("synthetic-key")
    result = await AlertsEnergyAccessChecker().check(secret)
    constructor.assert_called_once_with(api_key=secret)
    assert result.status is EnergyApiCheckStatus.VALID
    client.check_access.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_power_location_reuses_actor_catalogue_katottg_without_updating_alerts(monkeypatch):
    from time_tl.infrastructure.db import territories

    session = AsyncMock()
    session.scalar.return_value = "5272"
    context = Mock(__aenter__=AsyncMock(return_value=session), __aexit__=AsyncMock())
    factory = Mock(return_value=context)
    catalog = Mock(
        get_path=AsyncMock(
            return_value=(
                SimpleNamespace(
                    region_id="5272",
                    external_code="UA12000000000090473",
                    kind="HROMADA",
                    name="Синтетична громада",
                    catalog_version="test-v1",
                ),
            )
        )
    )
    monkeypatch.setattr(
        territories, "PostgresTerritoryCatalogRepository", Mock(return_value=catalog)
    )
    seed = await PostgresPowerBotPort(factory, Mock()).location_seed(42)
    assert seed.katottg == "UA12000000000090473" and seed.needs_settlement
    statement = session.scalar.call_args.args[0]
    assert statement.compile().params == {"telegram_user_id_1": 42}
    catalog.get_path.assert_awaited_once_with(provider="alerts_in_ua", region_id="5272")
    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()
    catalog.get_path.side_effect = LookupError("retired catalogue selection")
    assert await PostgresPowerBotPort(factory, Mock()).location_seed(42) is None
    catalog.get_path.side_effect = RuntimeError("corrupt active catalogue")
    with pytest.raises(RuntimeError, match="corrupt active catalogue"):
        await PostgresPowerBotPort(factory, Mock()).location_seed(42)
