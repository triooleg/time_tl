"""LOCAL owner power controls with fake repository, no source HTTP."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.admin.test_async_app import FakeAsyncRepository, FakeMetrics
from time_tl.admin.async_app import create_async_admin_app
from time_tl.admin.power import (
    PowerAdminConflict,
    power_html,
    public_source_statistics,
    public_statistics,
    register_power_admin,
)
from time_tl.admin.security import utcnow
from time_tl.config import Settings
from time_tl.domain.power_outages.models import PowerProvider, PowerSourceKind


class FakePowerAdmin:
    def __init__(self):
        self.calls = []
        self.revision = 1
        self.refresh_allowed = True

    async def stats(self, now):
        return tuple(
            {
                "provider_id": p.value,
                "enabled": False,
                "source_status": "UNAVAILABLE",
                "last_success_at": None,
                "last_attempt_at": None,
                "group_count": 0,
                "schedule_count": 0,
                "subscriber_count": 1,
                "pending_notifications": 0,
                "revision": self.revision,
                "last_error_code": "must-not-export-sensitive-error",
                "private": "private-test-marker",
            }
            for p in PowerProvider
        )

    async def set_enabled(self, provider, enabled, **kwargs):
        if kwargs["expected_revision"] != self.revision:
            raise PowerAdminConflict()
        self.calls.append(("enabled", provider, enabled, kwargs["actor_id"]))
        self.revision += 1
        return self.revision

    async def request_refresh(self, provider, **kwargs):
        self.calls.append(("refresh", provider, kwargs["actor_id"]))
        return self.refresh_allowed


@pytest.fixture
def panel():
    repo = FakeAsyncRepository(must_change=False)
    power = FakePowerAdmin()
    settings = Settings(
        DATABASE_URL="postgresql+asyncpg://u:p@db/time_tl",
        TIME_TL_ENV="local",
        ADMIN_BACKEND="postgres",
        ADMIN_COOKIE_SECURE=False,
    )
    client = TestClient(create_async_admin_app(settings, repo, FakeMetrics(), power_admin=power))
    session = asyncio.run(repo.create_session("admin", False, utcnow()))
    client.cookies.set("time_tl_owner", session.token)
    form = {
        "csrf": session.csrf_token,
        "current_password": "bootstrap-password-long",
        "provider": PowerProvider.DTEK.value,
        "expected_revision": "1",
    }
    return client, repo, power, form


def test_page_statistics_redaction_and_regional_providers(panel):
    client, _, _, _ = panel
    page = client.get("/power")
    assert page.status_code == 200 and "⚡ Графіки електроенергії" in page.text
    assert "UNAVAILABLE" in page.text and "фактическое" in page.text
    assert "Источники, группы" in client.get("/").text
    api = client.get("/api/power")
    assert len(api.json()["providers"]) == 6
    assert "must-not-export" not in api.text and "private-test-marker" not in api.text


def test_owner_auth_password_csrf_and_readonly_get(panel):
    client, repo, power, form = panel
    assert client.post("/power/refresh", data={**form, "csrf": "wrong"}).status_code == 403
    assert (
        client.post("/power/refresh", data={**form, "current_password": "bad"}).status_code == 401
    )
    assert not power.calls
    client.cookies.clear()
    assert client.get("/api/power").status_code == 401
    assert client.post("/power/refresh", data=form).status_code == 401
    session = asyncio.run(repo.create_session("admin", True, utcnow()))
    client.cookies.set("time_tl_owner", session.token)
    repo.owner.must_change_password = True
    assert client.get("/api/power").status_code == 401


def test_mutations_cas_cooldown_and_bounded_duplicate_forms(panel):
    client, _, power, form = panel
    assert (
        client.post(
            "/power/enabled", data={**form, "enabled": "true"}, follow_redirects=False
        ).status_code
        == 303
    )
    assert power.calls[-1] == ("enabled", PowerProvider.DTEK, True, "admin")
    assert client.post("/power/enabled", data={**form, "enabled": "false"}).status_code == 409
    assert client.post("/power/refresh", data=form, follow_redirects=False).status_code == 303
    power.refresh_allowed = False
    assert client.post("/power/refresh", data=form).status_code == 429
    assert client.post("/power/refresh", content="x=" + "a" * 8192).status_code == 413
    assert client.post("/power/refresh", content="csrf=a&csrf=b").status_code == 422


def test_missing_data_is_unavailable_not_synthetic_healthy_or_zero():
    now = datetime(2026, 11, 4, tzinfo=UTC)
    projected = public_statistics(
        (
            {
                "provider_id": PowerProvider.CEK.value,
                "source_status": "made-up",
                "enabled": "yes",
                "subscriber_count": True,
                "group_count": -1,
            },
        ),
        now,
    )
    assert projected[0]["source_status"] == "UNAVAILABLE"
    assert projected[0]["subscriber_count"] is None and projected[0]["group_count"] is None
    body = power_html("test-csrf", projected)
    assert "Данные недоступны" in body
    assert 'action="/power/enabled"' not in body


@pytest.mark.parametrize("last_success", [None, "not-a-date", "2026-11-05T00:00:00+00:00"])
def test_fresh_label_requires_actual_valid_nonfuture_success_time(last_success):
    result = public_statistics(
        (
            {
                "provider_id": PowerProvider.DTEK.value,
                "source_status": "FRESH",
                "last_success_at": last_success,
            },
        ),
        datetime(2026, 11, 4, tzinfo=UTC),
    )
    assert result[0]["source_status"] == "UNAVAILABLE"


def test_admin_source_freshness_uses_configured_cutoff():
    now = datetime(2026, 11, 4, tzinfo=UTC)
    row = {
        "provider_id": PowerProvider.DTEK.value,
        "source_status": "FRESH",
        "last_success_at": now - timedelta(seconds=400),
    }
    assert public_statistics((row,), now, stale_after_seconds=300)[0]["source_status"] == "STALE"
    assert public_statistics((row,), now, stale_after_seconds=600)[0]["source_status"] == "FRESH"


class FakeSourceAdmin:
    def __init__(self):
        self.calls = []
        self.revision = 1
        self.refresh_allowed = True

    async def stats(self, now):
        return (
            {
                "provider_id": "dtek_dnipro",
                "source_kind": "alerts_energy_html",
                "enabled": True,
                "priority": 0,
                "source_status": "UNAVAILABLE",
                "verification_status": "MATCH",
                "match_percent": 100,
                "compared_count": 0,
                "matched_count": 0,
                "revision": self.revision,
                "collection_allowed": True,
                "last_error_code": "SENSITIVE_RAW_HTML <script>bad</script> https://private.invalid",
                "private": "SENSITIVE_PRIVATE",
            },
        )

    async def set_source_enabled(self, provider, source, enabled, **kwargs):
        if kwargs["expected_revision"] != self.revision:
            raise PowerAdminConflict()
        self.calls.append(("enabled", provider, source, enabled, kwargs["actor_id"]))
        self.revision += 1
        return self.revision

    async def request_source_refresh(self, provider, source, **kwargs):
        self.calls.append(("refresh", provider, source, kwargs["actor_id"]))
        return self.refresh_allowed


@pytest.fixture
def source_panel():
    repo = FakeAsyncRepository(must_change=False)
    owner = asyncio.run(repo.create_session("admin", False, utcnow()))
    owner_state = {"session": owner}
    source = FakeSourceAdmin()

    async def session_for(request, _):
        return (
            owner_state["session"] if request.cookies.get("owner") == owner.token else None
        ), None

    async def reauth(request, session, password):
        return password == "test-owner-password", None

    app = FastAPI()
    register_power_admin(
        app, FakePowerAdmin(), session_for=session_for, reauthenticate=reauth, source_port=source
    )
    client = TestClient(app)
    client.cookies.set("owner", owner.token)
    form = {
        "csrf": owner.csrf_token,
        "current_password": "test-owner-password",
        "provider": "dtek_dnipro",
        "source_kind": "alerts_energy_html",
        "expected_revision": "1",
    }
    return client, source, form, owner_state


def test_source_statistics_redacted_zero_samples_and_policyclosed(source_panel):
    client, source, _, _ = source_panel
    data = client.get("/api/power")
    assert data.status_code == 200
    row = data.json()["sources"][0]
    assert row["collection_allowed"] is False
    assert row["match_percent"] is None and row["verification_status"] == "UNVERIFIED"
    assert row["last_error_code"] == "SOURCE_ERROR"
    page = client.get("/power")
    assert "Alerts Energy — HTML" in page.text
    assert "не разрешает сбор данных" in page.text
    assert "SENSITIVE" not in data.text + page.text and "private.invalid" not in page.text
    assert not source.calls


def test_source_controls_owner_csrf_password_cas_and_cooldown(source_panel):
    client, source, form, owner_state = source_panel
    owner = owner_state["session"]
    assert (
        client.post(
            "/power/source-enabled", data={**form, "enabled": "true", "csrf": "bad"}
        ).status_code
        == 403
    )
    assert (
        client.post("/power/source-refresh", data={**form, "current_password": "bad"}).status_code
        == 401
    )
    assert not source.calls
    assert (
        client.post(
            "/power/source-enabled", data={**form, "enabled": "true"}, follow_redirects=False
        ).status_code
        == 303
    )
    assert source.calls[-1] == (
        "enabled",
        PowerProvider.DTEK,
        PowerSourceKind.ALERTS_ENERGY_HTML,
        True,
        "admin",
    )
    assert (
        client.post("/power/source-enabled", data={**form, "enabled": "false"}).status_code == 409
    )
    assert (
        client.post("/power/source-refresh", data=form, follow_redirects=False).status_code == 303
    )
    source.refresh_allowed = False
    assert client.post("/power/source-refresh", data=form).status_code == 429
    client.cookies.clear()
    assert client.get("/api/power").status_code == 401
    assert client.post("/power/source-refresh", data=form).status_code == 401
    client.cookies.set("owner", owner.token)
    owner_state["session"] = replace(owner, must_change_password=True)
    assert client.post("/power/source-refresh", data=form).status_code == 401


def test_source_forms_duplicate_bounds_and_only_aggregate_kinds(source_panel):
    client, source, form, _ = source_panel
    assert (
        client.post("/power/source-refresh", content="source_kind=a&source_kind=b").status_code
        == 422
    )
    assert client.post("/power/source-refresh", content="x=" + "a" * 8192).status_code == 413
    for invalid in ("official", "https://private.invalid", "fake"):
        assert (
            client.post("/power/source-refresh", data={**form, "source_kind": invalid}).status_code
            == 422
        )
    assert not source.calls
    assert client.post("/power/source-refresh", data={**form, "csrf": "неверно"}).status_code == 403
    assert (
        client.post("/power/source-refresh", data={**form, "expected_revision": "0"}).status_code
        == 422
    )


@pytest.mark.parametrize(
    "compared,matched,expected",
    [(2, 1, 50), (1, 2, None), (True, 1, None), (0, 0, None), (-1, 0, None)],
)
def test_match_percent_computed_from_valid_samples_only(compared, matched, expected):
    rows = (
        {
            "provider_id": "cek_dnipro",
            "source_kind": "bezsvitla",
            "compared_count": compared,
            "matched_count": matched,
            "match_percent": 100,
            "verification_status": "MATCH",
        },
    )
    result = public_source_statistics(rows, utcnow())[0]
    assert result["match_percent"] == expected
    assert result["collection_allowed"] is False
