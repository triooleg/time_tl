import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.admin.test_async_app import FakeAsyncRepository, FakeMetrics
from time_tl.admin.async_app import create_async_admin_app
from time_tl.admin.security import utcnow
from time_tl.admin.weather import PAGE
from time_tl.config import Settings
from time_tl.infrastructure.db.weather_configuration import WeatherConfiguration
from time_tl.infrastructure.weather.met_norway import WeatherProviderError
from time_tl.runtime.weather_admin import WeatherAdminPort, WeatherDiagnosticDeferred


@pytest.fixture
def panel():
    repository = FakeAsyncRepository(must_change=False)
    port = SimpleNamespace(
        status=AsyncMock(
            return_value={
                "configuration": WeatherConfiguration().model_dump(),
                "diagnostic": {},
                "coverage": {"territories": [], "weather_point_count": 0},
                "last_run": None,
            }
        ),
        save=AsyncMock(),
        check=AsyncMock(return_value="CONNECTED"),
    )
    settings = Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://synthetic@db/test",
        TIME_TL_ENV="local",
        ADMIN_BACKEND="postgres",
        ADMIN_COOKIE_SECURE=False,
    )
    client = TestClient(
        create_async_admin_app(settings, repository, FakeMetrics(), weather_admin=port)
    )
    owner = asyncio.run(repository.create_session("admin", False, utcnow()))
    client.cookies.set("time_tl_owner", owner.token)
    form = {
        "csrf": owner.csrf_token,
        "current_password": "bootstrap-password-long",
        "expected_revision": "0",
    }
    return client, port, form


def test_weather_coverage_and_registry_are_owner_only_and_no_store(panel):
    client, port, _ = panel
    for path in (PAGE, "/api/weather", "/coverage", "/api/coverage", "/settings/external-services"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "latitude" not in response.text and "ciphertext" not in response.text
    assert not port.check.await_count and not port.save.await_count
    client.cookies.clear()
    assert client.get(PAGE).status_code == 401


def test_check_requires_csrf_reauthentication_and_explicit_single_action(panel):
    client, port, form = panel
    assert client.post(PAGE + "/check", data={**form, "csrf": "wrong"}).status_code == 403
    assert (
        client.post(PAGE + "/check", data={**form, "current_password": "wrong"}).status_code == 401
    )
    assert not port.check.await_count
    assert client.post(PAGE + "/check", data=form).json() == {"status": "CONNECTED"}
    assert port.check.await_count == 1 and not port.save.await_count


def test_duplicate_fields_or_json_keys_and_unknown_fields_are_rejected(panel):
    client, port, form = panel
    assert client.post(PAGE + "/save", content="csrf=x&csrf=y").status_code == 422
    assert (
        client.post(
            PAGE + "/save", data={**form, "configuration": '{"enabled":false,"enabled":true}'}
        ).status_code
        == 422
    )
    assert (
        client.post(
            PAGE + "/save", data={**form, "configuration": "{}", "api_key": "synthetic-never-save"}
        ).status_code
        == 422
    )
    assert not port.save.await_count


def test_blank_energy_key_keeps_existing_encrypted_store_unchanged():
    from tests.admin.test_energy_api_admin import panel as energy_panel

    # Reuse the established credential UI fixture without obtaining any real key.
    client, _, port, form = energy_panel.__wrapped__()
    assert (
        client.post(
            "/power/alerts-energy-api/save", data={**form, "api_key": ""}, follow_redirects=False
        ).status_code
        == 303
    )
    assert not port.calls


@pytest.mark.asyncio
async def test_met_diagnostic_respects_shared_http_expires_before_any_http():
    store = SimpleNamespace(
        claim_source_request=AsyncMock(side_effect=WeatherProviderError("REQUEST_DEFERRED"))
    )
    requests = []

    def client_factory(**kwargs):
        requests.append(kwargs)
        raise AssertionError("No HTTP while cache request is not due")

    port = WeatherAdminPort(
        None, client_factory=client_factory, storage_factory=lambda *a, **k: store
    )
    config = WeatherConfiguration(enabled=True, terms_approved=True)
    port.configuration = SimpleNamespace(
        load=AsyncMock(return_value=config), claim_diagnostic=AsyncMock(return_value=True)
    )
    port.coverage = SimpleNamespace(
        snapshot=AsyncMock(
            return_value=SimpleNamespace(
                weather_points=(
                    SimpleNamespace(
                        location=SimpleNamespace(
                            location_id="00000000-0000-0000-0000-000000000001",
                            latitude=48.5,
                            longitude=34.6,
                        )
                    ),
                )
            )
        )
    )
    with pytest.raises(WeatherDiagnosticDeferred):
        await port.check(revision=0, now=utcnow())
    assert not requests


@pytest.mark.asyncio
async def test_met_diagnostic_makes_one_request_and_never_syncs_or_sends():
    from datetime import UTC, datetime
    from pathlib import Path

    now = datetime(2026, 11, 5, 10, tzinfo=UTC)
    store = SimpleNamespace(
        claim_source_request=AsyncMock(),
        authorize_source_request=AsyncMock(),
        record_source_response=AsyncMock(),
        record_source_retry=AsyncMock(),
    )
    count = []

    def reply(request):
        count.append(request)
        return httpx.Response(
            200, content=Path("tests/fixtures/weather/met_compact.json").read_bytes()
        )

    def client_factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(reply), **kwargs)

    port = WeatherAdminPort(
        None,
        client_factory=client_factory,
        storage_factory=lambda *a, **k: store,
        clock=lambda: now,
    )
    port.configuration = SimpleNamespace(
        load=AsyncMock(return_value=WeatherConfiguration(enabled=True, terms_approved=True)),
        claim_diagnostic=AsyncMock(return_value=True),
        record_diagnostic=AsyncMock(),
    )
    port.coverage = SimpleNamespace(
        snapshot=AsyncMock(
            return_value=SimpleNamespace(
                weather_points=(
                    SimpleNamespace(
                        location=SimpleNamespace(
                            location_id="00000000-0000-0000-0000-000000000001",
                            latitude=48.5,
                            longitude=34.6,
                        )
                    ),
                )
            )
        )
    )
    result = await port.check(revision=0, now=now)
    assert result == "CONNECTED"
    assert len(count) == 1
    store.authorize_source_request.assert_awaited_once()
    store.record_source_response.assert_awaited_once()
    assert not store.record_source_retry.await_count
