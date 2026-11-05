from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from time_tl.runtime.weather_bot import PostgresWeatherBotPort


def port(enabled: bool = False) -> PostgresWeatherBotPort:
    prefs = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                forecast_requested=False, weather_enabled=False, revision=0
            )
        ),
        toggle_notifications=AsyncMock(),
        request_forecast=AsyncMock(),
    )
    return PostgresWeatherBotPort(
        None, prefs, SimpleNamespace(previous=AsyncMock()), notifications_enabled=enabled
    )


async def test_preferences_read_does_not_create_user_or_forecast_interest() -> None:
    value = port()
    prefs = await value.preferences(1)
    assert prefs.revision == 0 and not prefs.forecast_requested and not prefs.weather_enabled
    value.preference_store.request_forecast.assert_not_called()


async def test_delivery_enable_requires_deployment_capability() -> None:
    value = port()
    with pytest.raises(ValueError, match="weather_delivery_unavailable"):
        await value.save_notifications(1, True, 0)
    value.preference_store.toggle_notifications.assert_not_called()


def test_capability_must_be_bool() -> None:
    with pytest.raises(ValueError):
        PostgresWeatherBotPort(None, None, None, notifications_enabled=1)  # type: ignore[arg-type]


@pytest.mark.parametrize("defect", ["sha", "url", "version", "approval"])
async def test_context_requires_full_approved_provenance(monkeypatch, defect):
    from datetime import UTC, datetime
    from uuid import UUID

    from time_tl.infrastructure.db.models import WeatherLocation
    from time_tl.infrastructure.db.weather_configuration import WeatherConfiguration

    row = WeatherLocation(
        id=UUID(int=100),
        region_id="h1",
        katottg_code="UA00000000000000003",
        locality_name="Synthetic",
        latitude=48.5,
        longitude=35,
        enabled=True,
        coverage_type="BASE",
        approved_at=datetime(2026, 11, 5, tzinfo=UTC),
        provenance_url="https://example.org/catalog",
        provenance_sha256="a" * 64,
        provenance_version="v1",
    )
    if defect == "sha":
        row.provenance_sha256 = "invalid"
    elif defect == "url":
        row.provenance_url = "http://insecure.example"
    elif defect == "version":
        row.provenance_version = ""
    else:
        row.approved_at = None

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def scalar(self, statement):
            return UUID(int=200)

        async def execute(self, statement):
            return SimpleNamespace(scalar_one_or_none=lambda: row)

    config = SimpleNamespace(load=AsyncMock(return_value=WeatherConfiguration()))
    monkeypatch.setattr(
        "time_tl.runtime.weather_bot.PostgresWeatherConfiguration", lambda _: config
    )
    value = PostgresWeatherBotPort(Session, None, None)
    context = await value.context(1)
    assert not context.has_precise_point and not context.collection_enabled
