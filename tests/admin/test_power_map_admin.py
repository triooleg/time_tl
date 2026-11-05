import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from tests.admin.test_async_app import FakeAsyncRepository, FakeMetrics
from time_tl.admin.async_app import create_async_admin_app
from time_tl.admin.security import utcnow
from time_tl.config import Settings


def panel():
    repository = FakeAsyncRepository(must_change=False)
    port = SimpleNamespace(
        statistics=AsyncMock(return_value={"poll_interval_seconds": 300}),
        region=AsyncMock(return_value={"source_status": "UNAVAILABLE"}),
    )
    app = create_async_admin_app(
        Settings(
            _env_file=None,
            DATABASE_URL="postgresql+asyncpg://synthetic:synthetic@db/test",
            TIME_TL_ENV="local",
            ADMIN_BACKEND="postgres",
            ADMIN_COOKIE_SECURE=False,
        ),
        repository,
        FakeMetrics(),
        power_map=port,
    )
    return TestClient(app), repository, port


def test_map_routes_are_owner_only_and_cache_only():
    client, repository, port = panel()
    routes = ["/power/map", "/api/power/map", "/api/power/map/regions/kyiv"]
    for path in routes:
        response = client.get(path)
        assert response.status_code == 401 and response.headers["Cache-Control"] == "no-store"
    port.statistics.assert_not_awaited()
    port.region.assert_not_awaited()
    owner = asyncio.run(repository.create_session("admin", False, utcnow()))
    client.cookies.set("time_tl_owner", owner.token)
    for path in routes:
        response = client.get(path)
        assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    assert port.statistics.await_count == 2 and port.region.await_count == 1
    assert client.post("/power/map/refresh").status_code in {404, 405}


def test_map_error_is_closed_and_never_echoes_exception():
    client, repository, port = panel()
    owner = asyncio.run(repository.create_session("admin", False, utcnow()))
    client.cookies.set("time_tl_owner", owner.token)
    port.statistics.side_effect = ValueError("secret-do-not-echo")
    response = client.get("/api/power/map")
    assert response.status_code == 503 and "secret-do-not-echo" not in response.text
