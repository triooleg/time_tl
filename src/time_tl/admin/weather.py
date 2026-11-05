"""Russian owner-only weather settings and aggregate coverage, never secrets."""

from __future__ import annotations

import asyncio
import html
import json
from secrets import compare_digest
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .security import utcnow

HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}
PAGE = "/settings/weather"


def _json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_configuration_key")
        value[key] = item
    return value


def render_page(status: dict[str, Any], csrf: str) -> str:
    config = status["configuration"]
    fields = html.escape(json.dumps(config, ensure_ascii=False, indent=2))
    metadata = html.escape(
        json.dumps(
            {k: v for k, v in status.items() if k != "configuration"}, ensure_ascii=False, indent=2
        )
    )
    return (
        "<!doctype html><html lang='ru'><meta charset='utf-8'><title>Погода</title>"
        "<a href='/'>Админка</a> · <a href='/settings/external-services'>Внешние сервисы</a> · "
        "<a href='/coverage'>Активные территории</a><h1>🌤 Погода</h1>"
        "<p>MET Norway: ключ не требуется. Нужны корректные User-Agent, контакт и согласие "
        "с условиями. Сбор работает только для подтверждённых активных погодных точек. "
        "Пользовательские уведомления включаются каждым пользователем отдельно.</p>"
        "<p>enabled — сбор; cycles — 4 или 6 циклов в сутки по Киеву; "
        "terms_approved — условия приняты; *_days — сроки хранения. "
        "Неизвестные поля и неверные значения отклоняются.</p>"
        f"<form method='post' action='{PAGE}/save'>"
        f"<input type='hidden' name='csrf' value='{html.escape(csrf, quote=True)}'>"
        f"<input type='hidden' name='expected_revision' value='{config['revision']}'>"
        f"<textarea name='configuration' rows='24' cols='90'>{fields}</textarea><br>"
        "<label>Текущий пароль <input type='password' name='current_password' required "
        "autocomplete='current-password'></label><button>Сохранить</button></form>"
        f"<form method='post' action='{PAGE}/check'>"
        f"<input type='hidden' name='csrf' value='{html.escape(csrf, quote=True)}'>"
        f"<input type='hidden' name='expected_revision' value='{config['revision']}'>"
        "<input type='password' name='current_password' required autocomplete='current-password'>"
        "<button>🔍 Проверить подключение — один запрос</button></form>"
        f"<form method='post' action='{PAGE}/sync'>"
        f"<input type='hidden' name='csrf' value='{html.escape(csrf, quote=True)}'>"
        f"<input type='hidden' name='expected_revision' value='{config['revision']}'>"
        "<input type='password' name='current_password' required autocomplete='current-password'>"
        "<button>🔄 Запросить синхронизацию</button>"
        "<p>Фоновая очередь соблюдает кеш и лимиты API. Эта кнопка не отправляет сообщения.</p>"
        "</form>"
        f"<pre>{metadata}</pre></html>"
    )


def register_weather(app: FastAPI, port: Any, *, session_for: Any, reauthenticate: Any) -> None:
    if port is None:
        return

    def failure(code: int, detail: str) -> Response:
        return JSONResponse({"detail": detail}, status_code=code, headers=HEADERS)

    async def owner(request: Request) -> Any:
        session, _ = await session_for(request, False)
        return (
            session
            if session and session.username == "admin" and not session.must_change_password
            else None
        )

    @app.get(PAGE)
    @app.get("/api/weather")
    @app.get("/coverage")
    @app.get("/api/coverage")
    async def view(request: Request) -> Response:
        session = await owner(request)
        if session is None:
            return failure(401, "unauthorized")
        try:
            async with asyncio.timeout(5):
                status = await port.status(utcnow())
            if request.url.path == "/api/weather":
                return JSONResponse(status, headers=HEADERS)
            if request.url.path == "/api/coverage":
                return JSONResponse(status["coverage"], headers=HEADERS)
            if request.url.path == "/coverage":
                selected = request.query_params.get("oblast")
                kind = request.query_params.get("type")
                rows = [
                    row
                    for row in status["coverage"]["territories"]
                    if (not selected or row["oblast"] == selected)
                    and (kind not in {"alerts", "power", "weather"} or row[kind] > 0)
                ]
                return HTMLResponse(
                    "<html lang='ru'><meta charset='utf-8'>"
                    "<a href='/settings/weather'>Погода</a><h1>🌍 Активные территории</h1>"
                    "<form>Область <input name='oblast'> Тип подписки <select name='type'>"
                    "<option value=''>Все</option><option value='alerts'>Тревоги</option>"
                    "<option value='power'>Электричество</option>"
                    "<option value='weather'>Погода</option>"
                    "</select><button>Показать</button></form><pre>"
                    + html.escape(
                        json.dumps(
                            {**status["coverage"], "territories": rows},
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    + "</pre></html>",
                    headers=HEADERS,
                )
            return HTMLResponse(render_page(status, session.csrf_token), headers=HEADERS)
        except Exception:
            return failure(503, "weather_admin_unavailable")

    @app.get("/settings/external-services")
    async def external(request: Request) -> Response:
        if await owner(request) is None:
            return failure(401, "unauthorized")
        # No duplicate token store, arbitrary endpoint editor or fabricated connected statuses.
        return HTMLResponse(
            "<html lang='ru'><meta charset='utf-8'><h1>Внешние сервисы</h1>"
            "<ul><li>🚨 alerts.in.ua — API Token: <a href='/'>существующая конфигурация "
            "и проверка поставщика тревог</a></li><li>⚡ Alerts Energy — "
            "<a href='/power/alerts-energy-api'>сохранённый API-ключ и проверка доступа</a> · "
            "<a href='/power/map'>мониторинг синхронизации</a></li>"
            "<li>🌤 MET Norway — без ключа: "
            "<a href='/settings/weather'>настройка и проверка</a></li>"
            "<li>Open-Meteo — отключён; бесплатный интерфейс требует допустимого режима "
            "использования, платный — подтверждённого тарифа и интерфейса авторизации.</li>"
            "<li>ДТЭК / ЦЭК / Bezsvitla — ключ не требуется для разрешённых публичных "
            "страниц; <a href='/power'>статусы существующих адаптеров</a>.</li>"
            "<li>NOAA GFS / DWD ICON / ECMWF Open Data — открытые наборы без обязательного ключа; "
            "адаптеры не подключены.</li><li>Meteoalarm / Укргидрометцентр — "
            "машиночитаемый интерфейс и авторизация пока не подтверждены; отключены.</li>"
            "</ul><a href='/coverage'>🌍 Активные территории</a></html>",
            headers=HEADERS,
        )

    @app.post(PAGE + "/save")
    @app.post(PAGE + "/check")
    @app.post(PAGE + "/sync")
    async def change(request: Request) -> Response:
        session = await owner(request)
        if session is None:
            return failure(401, "unauthorized")
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 8192:
                return failure(413, "invalid_form")
            raw.extend(chunk)
        save = request.url.path.endswith("/save")
        try:
            parsed = parse_qs(raw.decode(), keep_blank_values=True, max_num_fields=4)
            fields = {"csrf", "current_password", "expected_revision"}
            if save:
                fields.add("configuration")
            if set(parsed) != fields or any(len(value) != 1 for value in parsed.values()):
                raise ValueError
            form = {key: value[0] for key, value in parsed.items()}
            revision = form["expected_revision"]
            if not revision.isascii() or not revision.isdecimal() or len(revision) > 10:
                raise ValueError
            expected = int(revision)
            if not 0 <= expected < 2**31 - 1 or len(form["current_password"]) > 1024:
                raise ValueError
        except (ValueError, UnicodeError):
            return failure(422, "invalid_form")
        if not compare_digest(session.csrf_token.encode(), form["csrf"].encode()):
            return failure(403, "invalid_csrf")
        valid, cooldown = await reauthenticate(request, session, form["current_password"])
        if not valid:
            return failure(429 if cooldown else 401, "unauthorized")
        try:
            async with asyncio.timeout(15):
                if save:
                    payload = json.loads(
                        form["configuration"],
                        object_pairs_hook=_json_pairs,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
                    )
                    await port.save(
                        payload, expected_revision=expected, actor=session.username, now=utcnow()
                    )
                elif request.url.path.endswith("/sync"):
                    await port.request_sync(revision=expected, actor=session.username, now=utcnow())
                    return JSONResponse({"status": "QUEUED"}, headers=HEADERS)
                else:
                    result = await port.check(revision=expected, now=utcnow())
                    return JSONResponse({"status": result}, headers=HEADERS)
        except ValueError as error:
            from time_tl.runtime.weather_admin import WeatherDiagnosticDeferred

            if isinstance(error, WeatherDiagnosticDeferred):
                return failure(429, "weather_diagnostic_deferred")
            return failure(422, "invalid_weather_configuration")
        except RecursionError:
            return failure(422, "invalid_weather_configuration")
        except Exception:
            return failure(503, "weather_admin_unavailable")
        return RedirectResponse(PAGE, status_code=303, headers=HEADERS)
