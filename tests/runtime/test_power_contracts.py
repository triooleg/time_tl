"""Power feature configuration is separate from live alert authorization."""

import pytest
from pydantic import ValidationError

from time_tl.config import Environment, Settings
from time_tl.runtime.contracts import RuntimeContractError, validate_runtime_contract


def test_power_is_opt_in_without_any_aggregator_key():
    settings = Settings(
        _env_file=None, DATABASE_URL="postgresql+asyncpg://powercheck@127.0.0.1:5432/powercheck"
    )
    assert not settings.power_outages_enabled
    assert not settings.power_alerts_energy_html_enabled
    assert not settings.power_bezsvitla_enabled
    assert settings.power_alerts_energy_html_poll_interval_seconds == 300
    assert not any("api_key" in name for name in type(settings).model_fields)


def test_html_poll_configuration_survives_owner_runtime_settings_roundtrip():
    settings = Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://powercheck@127.0.0.1:5432/powercheck",
        POWER_ALERTS_ENERGY_POLL_INTERVAL_SECONDS=1200,
    )
    copied = Settings.model_validate(settings.model_dump(by_alias=True))
    assert copied.power_alerts_energy_html_poll_interval_seconds == 1200


@pytest.mark.parametrize("interval", [0, 1, 299, 86401])
def test_poll_limits_cannot_be_lowered_to_aggressive_scraping(interval):
    with pytest.raises(ValidationError, match="POWER_ALERTS_ENERGY"):
        Settings(
            _env_file=None,
            DATABASE_URL="postgresql+asyncpg://powercheck@127.0.0.1:5432/powercheck",
            POWER_ALERTS_ENERGY_HTML_POLL_INTERVAL_SECONDS=interval,
        )


@pytest.mark.parametrize("environment", [Environment.STAGING, Environment.PRODUCTION])
def test_no_power_production_authorization_follows_from_old_release(environment):
    settings = Settings.model_construct(environment=environment, power_outages_enabled=True)
    with pytest.raises(RuntimeContractError, match="separate reviewed"):
        validate_runtime_contract(settings, "power-worker")
