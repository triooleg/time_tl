"""No API key leaves the authenticated credential input flow."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from tests.admin.test_async_app import FakeAsyncRepository, FakeMetrics
from time_tl.admin.async_app import create_async_admin_app
from time_tl.admin.energy_api import PAGE
from time_tl.admin.security import utcnow
from time_tl.config import Settings
from time_tl.infrastructure.db.energy_api_credentials import (
    EnergyApiCheckStatus,
    EnergyApiConflict,
    EnergyApiCooldown,
    EnergyApiStatus,
)

MARKER = "synthetic-secret-must-never-be-echoed"


class FakeEnergyApi:
    def __init__(self):
        self.revision = 0
        self.key = None
        self.calls = []
        self.cooldown = False
        self.fail = False

    async def status(self, now):
        if self.fail:
            raise ValueError(MARKER)
        return EnergyApiStatus(
            self.key is not None, self.revision, None, EnergyApiCheckStatus.NOT_CHECKED
        )

    async def save(self, api_key, *, expected_revision, actor_id, now):
        if self.revision != expected_revision:
            raise EnergyApiConflict()
        assert isinstance(api_key, SecretStr)
        self.key = api_key
        self.revision += 1
        self.calls.append(("save", actor_id))
        return await self.status(now)

    async def clear(self, *, expected_revision, actor_id, now):
        if self.revision != expected_revision:
            raise EnergyApiConflict()
        self.key = None
        self.revision += 1
        self.calls.append(("clear", actor_id))
        return await self.status(now)

    async def check(self, *, expected_revision, actor_id, now):
        if self.cooldown:
            raise EnergyApiCooldown()
        if self.revision != expected_revision:
            raise EnergyApiConflict()
        self.calls.append(("check", actor_id))
        return await self.status(now)


@pytest.fixture
def panel():
    repository, port = FakeAsyncRepository(must_change=False), FakeEnergyApi()
    settings = Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://synthetic:synthetic@db/test",
        TIME_TL_ENV="local",
        ADMIN_BACKEND="postgres",
        ADMIN_COOKIE_SECURE=False,
    )
    client = TestClient(
        create_async_admin_app(settings, repository, FakeMetrics(), energy_api=port)
    )
    owner = asyncio.run(repository.create_session("admin", False, utcnow()))
    client.cookies.set("time_tl_owner", owner.token)
    form = {
        "csrf": owner.csrf_token,
        "current_password": "bootstrap-password-long",
        "expected_revision": "0",
    }
    return client, repository, port, form


def test_default_disabled_power_still_allows_credential_page_and_never_echoes_key(panel):
    client, _, port, form = panel
    assert client.get(PAGE).status_code == 200
    assert "Alerts Energy" in client.get("/").text
    assert (
        client.post(
            PAGE + "/save", data={**form, "api_key": MARKER}, follow_redirects=False
        ).status_code
        == 303
    )
    assert port.calls == [("save", "admin")]
    for path in (PAGE, "/api" + PAGE):
        response = client.get(path)
        assert response.headers["cache-control"] == "no-store"
        assert MARKER not in response.text and "ciphertext" not in response.text
    page = client.get(PAGE).text
    assert 'name="api_key" autocomplete="off"' in page
    assert f'value="{MARKER}"' not in page
    assert set(client.get("/api" + PAGE).json()) == {
        "configured",
        "revision",
        "last_checked_at",
        "check_status",
    }


def test_owner_csrf_reauth_and_unknown_forms_cannot_trigger_check(panel):
    client, repository, port, form = panel
    for suffix in ("save", "delete", "check"):
        body = {**form, "api_key": MARKER} if suffix == "save" else form
        response = client.post(PAGE + "/" + suffix, data={**body, "csrf": "bad"})
        assert response.status_code == 403 and response.headers["cache-control"] == "no-store"
    assert client.post(PAGE + "/check", data={**form, "current_password": "bad"}).status_code == 401
    assert client.post(PAGE + "/save", content="csrf=a&csrf=b").status_code == 422
    assert (
        client.post(PAGE + "/save", data={**form, "api_key": MARKER, "unknown": "x"}).status_code
        == 422
    )
    assert client.post(PAGE + "/save", content="x=" + "a" * 8192).status_code == 413
    assert not port.calls
    client.cookies.clear()
    for path in (PAGE, "/api" + PAGE):
        response = client.get(path)
        assert response.status_code == 401 and response.headers["cache-control"] == "no-store"
    assert client.post(PAGE + "/check", data=form).status_code == 401
    owner = asyncio.run(repository.create_session("admin", True, utcnow()))
    client.cookies.set("time_tl_owner", owner.token)
    repository.owner.must_change_password = True
    assert client.get(PAGE).status_code == 401
    assert not port.calls


def test_cas_check_cooldown_clear_and_safe_unavailable_errors(panel):
    client, _, port, form = panel
    assert (
        client.post(
            PAGE + "/save", data={**form, "api_key": MARKER}, follow_redirects=False
        ).status_code
        == 303
    )
    assert client.post(PAGE + "/delete", data={**form, "confirm_delete": "yes"}).status_code == 409
    latest = {**form, "expected_revision": "1"}
    assert client.post(PAGE + "/check", data=latest, follow_redirects=False).status_code == 303
    port.cooldown = True
    assert client.post(PAGE + "/check", data=latest).status_code == 429
    assert client.post(PAGE + "/delete", data=latest).status_code == 422
    assert (
        client.post(
            PAGE + "/delete", data={**latest, "confirm_delete": "yes"}, follow_redirects=False
        ).status_code
        == 303
    )
    port.fail = True
    response = client.get(PAGE)
    assert response.status_code == 503 and MARKER not in response.text
    assert response.headers["cache-control"] == "no-store"
