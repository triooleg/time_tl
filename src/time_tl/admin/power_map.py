"""Owner-only local map inspection. These routes cannot access the source API."""

from __future__ import annotations

import html
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .security import Session

HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def register_power_map(
    app: FastAPI,
    port: Any,
    *,
    session_for: Callable[[Request, bool], Awaitable[tuple[Session | None, str | None]]],
) -> None:
    @app.get("/power/map")
    @app.get("/api/power/map")
    async def overview(request: Request) -> Response:
        session, _ = await session_for(request, False)
        if session is None or session.username != "admin" or session.must_change_password:
            return JSONResponse({"detail": "unauthorized"}, status_code=401, headers=HEADERS)
        try:
            if port is None:
                raise ValueError("power_map_unconfigured")
            now = datetime.now(UTC)
            data = await port.statistics(now)
            if request.url.path.startswith("/api/"):
                return JSONResponse(data, headers=HEADERS)
            return HTMLResponse(
                '<!doctype html><html lang="ru"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<link rel="stylesheet" href="/static/admin.css"><title>Карта графиков</title>'
                "<main><h1>Локальная карта графиков</h1>"
                "<p>Сбор каждые 5 минут. Просмотр читает только базу Time TL. "
                "Сохранённый график не подтверждает наличие электричества. "
                "При аварийных отключениях план не действует.</p>"
                "<p>Время указано в UTC.</p><pre>"
                + html.escape(json.dumps(data, ensure_ascii=False, indent=2))
                + '</pre><p><a href="/power/alerts-energy-api">API-ключ</a> · '
                '<a href="/">Главная панель</a></p></main></html>',
                headers=HEADERS,
            )
        except Exception:
            return JSONResponse(
                {"detail": "power_map_unavailable"}, status_code=503, headers=HEADERS
            )

    @app.get("/api/power/map/regions/{slug}")
    async def region(request: Request, slug: str) -> JSONResponse:
        session, _ = await session_for(request, False)
        if session is None or session.username != "admin" or session.must_change_password:
            return JSONResponse({"detail": "unauthorized"}, status_code=401, headers=HEADERS)
        try:
            if port is None:
                raise ValueError("power_map_unconfigured")
            data = await port.region(slug, datetime.now(UTC))
            return JSONResponse(data, headers=HEADERS)
        except ValueError:
            return JSONResponse({"detail": "invalid_region"}, status_code=422, headers=HEADERS)
        except Exception:
            return JSONResponse(
                {"detail": "power_map_unavailable"}, status_code=503, headers=HEADERS
            )
