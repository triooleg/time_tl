"""Owner-only API credential management; no credential is rendered back."""

from __future__ import annotations

import asyncio
import html
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from secrets import compare_digest
from typing import Protocol
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import SecretStr

from time_tl.infrastructure.db.energy_api_credentials import (
    EnergyApiCheckStatus,
    EnergyApiConflict,
    EnergyApiCooldown,
    EnergyApiStatus,
)

from .security import Session, utcnow

HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}
PAGE = "/power/alerts-energy-api"
STATUS_LABELS = {
    EnergyApiCheckStatus.NOT_CHECKED.value: "ещё не проверен",
    EnergyApiCheckStatus.CHECKING.value: "проверяется",
    EnergyApiCheckStatus.VALID.value: "доступ подтверждён",
    EnergyApiCheckStatus.INVALID.value: "ключ не принят",
    EnergyApiCheckStatus.UNAVAILABLE.value: "проверка недоступна",
    EnergyApiCheckStatus.RATE_LIMITED.value: "лимит запросов, повторите позже",
}


class EnergyApiAdminPort(Protocol):
    async def status(self, now: datetime) -> EnergyApiStatus: ...

    async def save(
        self,
        api_key: SecretStr,
        *,
        expected_revision: int,
        actor_id: str,
        now: datetime,
    ) -> EnergyApiStatus: ...

    async def clear(
        self,
        *,
        expected_revision: int,
        actor_id: str,
        now: datetime,
    ) -> EnergyApiStatus: ...

    async def check(
        self,
        *,
        expected_revision: int,
        actor_id: str,
        now: datetime,
    ) -> EnergyApiStatus: ...


def _public(status: EnergyApiStatus, now: datetime) -> dict[str, object]:
    if (
        not isinstance(status, EnergyApiStatus)
        or type(status.configured) is not bool
        or type(status.revision) is not int
        or not 0 <= status.revision < 2**31
        or not isinstance(status.check_status, EnergyApiCheckStatus)
    ):
        raise ValueError("invalid credential metadata")
    checked = status.last_checked_at
    if checked is not None and (
        not isinstance(checked, datetime)
        or checked.tzinfo is None
        or checked.utcoffset() is None
        or checked > now
    ):
        raise ValueError("invalid credential metadata")
    return {
        "configured": status.configured,
        "revision": status.revision,
        "last_checked_at": checked.astimezone(UTC).isoformat() if checked else None,
        "check_status": status.check_status.value,
    }


def _page(csrf: str, status: dict[str, object]) -> str:
    last_checked = html.escape(str(status["last_checked_at"] or "Не выполнялась"))
    hidden = (
        f'<input type="hidden" name="csrf" value="{html.escape(csrf, quote=True)}">'
        f'<input type="hidden" name="expected_revision" value="{status["revision"]}">'
        '<label>Текущий пароль владельца<input type="password" name="current_password" '
        'autocomplete="current-password" required></label>'
    )
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<link rel="stylesheet" href="/static/admin.css"><title>Alerts Energy — API</title>'
        "</head><body><main><h1>Alerts Energy — API-ключ</h1>"
        "<p>Ключ сохраняется в зашифрованном виде. После сохранения он не показывается. "
        "Сохранение и проверка не включают сбор графиков или уведомления.</p>"
        f"<p>Ключ сохранён: {'Да' if status['configured'] else 'Нет'}. "
        f"Проверка: {STATUS_LABELS[str(status['check_status'])]}. "
        f"Последняя проверка: {last_checked}.</p>"
        '<p><a href="/power/map">Локальная карта графиков и квота API</a></p>'
        f'<form method="post" action="{PAGE}/save">{hidden}'
        '<label>Новый API-ключ<input type="password" name="api_key" autocomplete="off" '
        'maxlength="4096"></label><p>Пустое поле — сохранить текущий ключ.</p>'
        "<button>Сохранить ключ</button></form>"
        f'<form method="post" action="{PAGE}/check">{hidden}'
        "<button>Проверить доступ API</button></form>"
        f'<form method="post" action="{PAGE}/delete">{hidden}'
        '<label><input type="checkbox" name="confirm_delete" value="yes" required>'
        "Подтверждаю удаление сохранённого ключа</label>"
        "<button>Удалить сохранённый ключ</button></form>"
        '<p><a href="/">Главная панель</a></p></main></body></html>'
    )


def register_energy_api(
    app: FastAPI,
    port: EnergyApiAdminPort | None,
    *,
    session_for: Callable[[Request, bool], Awaitable[tuple[Session | None, str | None]]],
    reauthenticate: Callable[[Request, Session, str], Awaitable[tuple[bool, timedelta | None]]],
) -> None:
    @app.middleware("http")
    async def credential_no_store(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if not (
            request.url.path == PAGE
            or request.url.path.startswith(PAGE + "/")
            or request.url.path == "/api" + PAGE
        ):
            return await call_next(request)
        try:
            response = await call_next(request)
        except Exception:
            response = JSONResponse({"detail": "energy_api_unavailable"}, status_code=503)
        response.headers.update(HEADERS)
        return response

    async def owner(request: Request) -> Session | None:
        session, _ = await session_for(request, False)
        if session is None or session.username != "admin" or session.must_change_password:
            return None
        return session

    def failure(code: int, detail: str) -> Response:
        return JSONResponse({"detail": detail}, status_code=code, headers=HEADERS)

    @app.get(PAGE)
    @app.get("/api" + PAGE)
    async def get_page(request: Request) -> Response:
        session = await owner(request)
        if session is None:
            return failure(401, "unauthorized")
        try:
            if port is None:
                raise ValueError
            now = utcnow()
            async with asyncio.timeout(5):
                status = _public(await port.status(now), now)
            if request.url.path.startswith("/api/"):
                return JSONResponse(status, headers=HEADERS)
            return HTMLResponse(_page(session.csrf_token, status), headers=HEADERS)
        except Exception:
            return failure(503, "energy_api_unavailable")

    @app.post(PAGE + "/save")
    @app.post(PAGE + "/delete")
    @app.post(PAGE + "/check")
    async def change(request: Request) -> Response:
        session = await owner(request)
        if session is None:
            return failure(401, "unauthorized")
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 8192:
                return failure(413, "invalid_form")
            raw.extend(chunk)
        action = request.url.path.rsplit("/", 1)[-1]
        try:
            parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=5)
            expected_fields = {"csrf", "current_password", "expected_revision"}
            if action == "save":
                expected_fields.add("api_key")
            if action == "delete" and "confirm_delete" in parsed:
                expected_fields.add("confirm_delete")
            if set(parsed) != expected_fields or any(len(v) != 1 for v in parsed.values()):
                raise ValueError
            form = {key: values[0] for key, values in parsed.items()}
            revision = form["expected_revision"]
            if not revision.isascii() or not revision.isdecimal() or len(revision) > 10:
                raise ValueError
            expected = int(revision)
            if expected >= 2**31 - 1 or len(form["current_password"]) > 1024:
                raise ValueError
        except (ValueError, UnicodeError):
            return failure(422, "invalid_form")
        if not compare_digest(session.csrf_token.encode(), form["csrf"].encode()):
            return failure(403, "invalid_csrf")
        valid, cooldown = await reauthenticate(request, session, form["current_password"])
        if not valid:
            return failure(429 if cooldown else 401, "unauthorized")
        try:
            if port is None:
                raise ValueError
            async with asyncio.timeout(12):
                if action == "save":
                    if form["api_key"]:
                        await port.save(
                            SecretStr(form["api_key"]),
                            expected_revision=expected,
                            actor_id=session.username,
                            now=utcnow(),
                        )
                    elif (await port.status(utcnow())).revision != expected:
                        raise EnergyApiConflict
                elif action == "delete":
                    if form.get("confirm_delete") != "yes":
                        return failure(422, "confirm_key_deletion")
                    await port.clear(
                        expected_revision=expected, actor_id=session.username, now=utcnow()
                    )
                else:
                    await port.check(
                        expected_revision=expected, actor_id=session.username, now=utcnow()
                    )
        except EnergyApiConflict:
            return failure(409, "energy_api_revision_changed")
        except EnergyApiCooldown:
            return failure(429, "energy_api_check_deferred")
        except Exception:
            return failure(503, "energy_api_unavailable")
        return RedirectResponse(PAGE + "?saved=1", status_code=303, headers=HEADERS)
