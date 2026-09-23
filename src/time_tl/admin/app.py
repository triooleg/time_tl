"""FastAPI owner panel backed by local sanitized state only."""
# ruff: noqa: E501  # audited embedded HTML/CSP literals

from __future__ import annotations

import html
import math
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from time_tl.config import AdminBackend, Environment, Settings, get_settings

from .metrics import HostMetricsCollector
from .repository import LocalAdminRepository
from .security import (
    PasswordPolicyError,
    Session,
    hash_password,
    utcnow,
    validate_new_password,
    verify_password,
)

COOKIE = "time_tl_owner"
LOGIN_CSRF = "time_tl_login_csrf"
GENERIC_LOGIN_ERROR = "Не удалось войти. Проверьте данные или попробуйте позже."


def _form(body: bytes) -> dict[str, str]:
    return {
        key: values[-1]
        for key, values in parse_qs(body.decode("utf-8"), keep_blank_values=True).items()
    }


def _client_ip(request: Request) -> str:
    # Do not trust forwarded headers until a reviewed trusted-proxy policy exists.
    return request.client.host if request.client else "unknown"


def _duration(value: timedelta) -> str:
    seconds = max(1, math.ceil(value.total_seconds()))
    return f"{seconds // 60 + bool(seconds % 60)} мин."


def _login_html(csrf: str, message: str = "") -> str:
    notice = f'<p class="notice" role="alert">{html.escape(message)}</p>' if message else ""
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Time TL — Вход</title><link rel="stylesheet" href="/static/admin.css"></head><body class="login"><main><form method="post" action="/login" class="login-card"><h1>Вход для владельца</h1>{notice}<input type="hidden" name="csrf" value="{html.escape(csrf)}"><label>Имя пользователя<input name="username" value="admin" autocomplete="username" required></label><label>Пароль<input type="password" name="password" autocomplete="current-password" required></label><button type="submit">Увійти</button></form></main></body></html>'''


def _change_html(csrf: str, message: str = "") -> str:
    notice = f'<p class="notice" role="alert">{html.escape(message)}</p>' if message else ""
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Time TL — Новый пароль</title><link rel="stylesheet" href="/static/admin.css"></head><body class="login"><main><form method="post" action="/change-password" class="login-card"><h1>Создайте постоянный пароль</h1>{notice}<input type="hidden" name="csrf" value="{html.escape(csrf)}"><label>Новый пароль<input type="password" name="password" minlength="14" autocomplete="new-password" required></label><label>Повторите пароль<input type="password" name="confirmation" minlength="14" autocomplete="new-password" required></label><button type="submit">Сохранить пароль</button></form></main></body></html>'''


def _dashboard_html(csrf: str, environment: str, poll: int, stale: int, message: str = "") -> str:
    notice = f'<div class="notice" role="status">{html.escape(message)}</div>' if message else ""
    return f'''<!doctype html><html lang="uk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Time TL — Панель</title><link rel="stylesheet" href="/static/admin.css"></head><body><a class="skip" href="#content">До вмісту</a><div class="shell"><aside><strong>Time TL</strong><nav aria-label="Розділи"><a href="#overview">Огляд</a><a href="#provider">Тривоги та провайдер</a><a href="#delivery">Доставка</a><a href="#users">Користувачі</a><a href="#server">Сервер і воркери</a><a href="#settings">Налаштування</a><a href="#help">Довідка</a></nav></aside><div><header><span>Time TL · {html.escape(environment.upper())}</span><span id="aggregate">Стан не підтверджено</span><form method="post" action="/logout"><input type="hidden" name="csrf" value="{csrf}"><button>Вийти</button></form></header><main id="content"><h1>Огляд</h1>{notice}<div class="critical" role="status">Стан тривог не підтверджено. Це не означає відбій.</div><button id="refresh">Оновити дані</button><p id="updated">Метрики ще не отримано</p><section id="overview" class="grid"><article><h2>Користувачі</h2><strong>Дані недоступні</strong><p>Агрегований PostgreSQL backend ще не підключено.</p></article><article><h2>Активні тривоги</h2><strong>Стан не підтверджено</strong></article><article><h2>Черга</h2><strong>Дані недоступні</strong></article><article><h2>Telegram</h2><strong>Дані недоступні</strong></article></section><section id="provider"><h2>Тривоги та провайдер</h2><p><b>UNKNOWN</b> · Нові відбої заблоковано</p><dl><dt>Інтервал опитування</dt><dd>{poll} с</dd><dt>Поріг застарівання</dt><dd>{stale} с</dd><dt>Ліміт</dt><dd>5 запитів/хв, burst 1</dd><dt>200 / 304 / 429 / 401-403 / 5xx</dt><dd>Дані недоступні</dd><dt>Singleton lease</dt><dd>Дані недоступні</dd></dl><p>У підтвердженому знімку немає активних тривог</p><p class="critical">Це не підтверджує відбій. Дані провайдера застаріли або недоступні.</p></section><section id="delivery"><h2>Доставка</h2><p>Outbox і Telegram: агрегати недоступні.</p><button id="resume-open" disabled>Відновити надсилання</button></section><section id="users"><h2>Користувачі</h2><p>Тільки агрегати; персональні дані та розклади не відображаються.</p></section><section id="server"><h2>Сервер і воркери</h2><div id="metrics" class="grid"><article>CPU: Метрики недоступні</article><article>RAM: Метрики недоступні</article><article>Disk: Метрики недоступні</article><article>Load: Метрики недоступні</article></div><p>Джерело: read-only host exporter</p><table><caption>Воркери</caption><thead><tr><th>Процес</th><th>Heartbeat</th><th>Перезапуски</th></tr></thead><tbody><tr><td>bot</td><td>Немає heartbeat</td><td>Дані недоступні</td></tr><tr><td>alert-worker</td><td>Немає heartbeat</td><td>Дані недоступні</td></tr><tr><td>schedule-worker</td><td>Немає heartbeat</td><td>Дані недоступні</td></tr><tr><td>sender-worker</td><td>Немає heartbeat</td><td>Дані недоступні</td></tr><tr><td>metrics collector</td><td id="metrics-heartbeat">Немає heartbeat</td><td>Дані недоступні</td></tr></tbody></table></section><section id="settings"><h2>Налаштування</h2><form method="post" action="/settings/provider" data-confirm="provider"><input type="hidden" name="csrf" value="{csrf}"><label>Poll interval, секунд<input type="number" name="poll" min="15" value="{poll}" required></label><label>Stale threshold, секунд<input type="number" name="stale" min="30" value="{stale}" required></label><button>Застосувати налаштування провайдера</button></form><h3>Секрети</h3><p>ALERTS_IN_UA_TOKEN — стан не розкривається в local backend.</p><p>Змініть секрет у захищеному середовищі сервера та перезапустіть відповідний сервіс за інструкцією.</p></section><section id="help"><h2>Довідка</h2><a href="/help/setup">Налаштування Telegram-бота</a><a href="/help/admin">Інструкція адміністратора</a><p>LOCAL, SIMULATION, STAGING і PRODUCTION — окремі рівні доказів.</p></section></main></div></div><dialog id="resume" role="alertdialog" aria-labelledby="resume-title"><h2 id="resume-title">Відновити надсилання</h2><p>У черзі: дані недоступні. Вік найстарішого: дані недоступні.</p><p>После возобновления накопленные сообщения могут быть отправлены немедленно.</p><form method="dialog"><button autofocus>Отмена</button><button disabled>Возобновить отправку</button></form></dialog><script src="/static/admin.js" defer></script></body></html>'''


def create_admin_app(settings: Settings, repo: LocalAdminRepository | None = None) -> FastAPI:
    if settings.admin_backend is not AdminBackend.LOCAL_SQLITE:
        raise RuntimeError("admin-web requires ADMIN_BACKEND=local_sqlite")
    if settings.environment not in {Environment.LOCAL, Environment.SIMULATION}:
        raise RuntimeError("local admin backend is forbidden outside local/simulation")
    repository = repo or LocalAdminRepository(settings.admin_state_dir)
    collector = HostMetricsCollector(settings.admin_state_dir)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.update(
            {
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
            }
        )
        return response

    def session_for(request: Request, touch: bool = True) -> tuple[Session | None, str | None]:
        token = request.cookies.get(COOKIE)
        return (repository.get_session(token, utcnow(), touch) if token else None), token

    def redirect_login(expired: bool = False) -> RedirectResponse:
        target = "/login?expired=1" if expired else "/login"
        response = RedirectResponse(target, 303)
        response.delete_cookie(COOKIE)
        return response

    def require_csrf(session: Session | None, supplied: str) -> bool:
        from secrets import compare_digest

        return bool(session and supplied and compare_digest(session.csrf_token, supplied))

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> HTMLResponse:
        import secrets

        csrf = secrets.token_urlsafe(24)
        message = (
            "Сеанс завершён. Войдите снова."
            if request.query_params.get("expired")
            else (
                "Пароль изменён. Панель готова к работе."
                if request.query_params.get("changed")
                else ""
            )
        )
        response = HTMLResponse(_login_html(csrf, message))
        response.set_cookie(
            LOGIN_CSRF,
            csrf,
            httponly=False,
            secure=settings.admin_cookie_secure,
            samesite="strict",
            max_age=900,
        )
        return response

    @app.post("/login")
    async def login(request: Request) -> Response:
        data = _form(await request.body())
        cookie_csrf = request.cookies.get(LOGIN_CSRF, "")
        from secrets import compare_digest

        if not cookie_csrf or not compare_digest(cookie_csrf, data.get("csrf", "")):
            return Response(status_code=403)
        username, source_ip, now = data.get("username", ""), _client_ip(request), utcnow()
        remaining = repository.lockout_remaining(username, source_ip, now)
        if remaining:
            return HTMLResponse(
                _login_html(
                    cookie_csrf,
                    f"Слишком много попыток входа. Повторите через {_duration(remaining)}",
                ),
                status_code=429,
            )
        account = repository.account(username)
        valid = bool(
            account
            and repository.bootstrap_is_valid(account, now)
            and verify_password(account.password_hash, data.get("password", ""))
        )
        if not valid:
            repository.record_failure(username, source_ip, now)
            remaining = repository.lockout_remaining(username, source_ip, now)
            if remaining:
                message = f"Слишком много попыток входа. Повторите через {_duration(remaining)}"
                return HTMLResponse(_login_html(cookie_csrf, message), status_code=429)
            return HTMLResponse(_login_html(cookie_csrf, GENERIC_LOGIN_ERROR), status_code=401)
        assert account is not None
        repository.reset_failures(username, source_ip)
        session = repository.create_session(username, account.must_change_password, now)
        target = "/change-password" if account.must_change_password else "/"
        response = RedirectResponse(target, 303)
        response.set_cookie(
            COOKIE,
            session.token,
            httponly=True,
            secure=settings.admin_cookie_secure,
            samesite="strict",
            max_age=12 * 3600,
        )
        response.delete_cookie(LOGIN_CSRF)
        return response

    @app.get("/change-password", response_class=HTMLResponse)
    async def change_page(request: Request) -> Response:
        session, _ = session_for(request)
        if not session:
            return redirect_login(True)
        return HTMLResponse(_change_html(session.csrf_token))

    @app.post("/change-password")
    async def change_password(request: Request) -> Response:
        session, token = session_for(request, False)
        if not session:
            return redirect_login(True)
        data = _form(await request.body())
        if not require_csrf(session, data.get("csrf", "")):
            return Response(status_code=403)
        try:
            validate_new_password(data.get("password", ""), data.get("confirmation", ""))
        except PasswordPolicyError as exc:
            return HTMLResponse(_change_html(session.csrf_token, str(exc)), status_code=422)
        repository.change_password(session.username, hash_password(data["password"]))
        response = RedirectResponse("/login?changed=1", 303)
        response.delete_cookie(COOKIE)
        return response

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        session, token = session_for(request, False)
        data = _form(await request.body())
        if not require_csrf(session, data.get("csrf", "")):
            return Response(status_code=403)
        if token:
            repository.delete_session(token)
        response = RedirectResponse("/login", 303)
        response.delete_cookie(COOKIE)
        return response

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> Response:
        session, _ = session_for(request)
        if not session:
            return redirect_login(True)
        if session.must_change_password:
            return RedirectResponse("/change-password", 303)
        poll = repository.get_setting("poll", settings.alert_poll_interval_seconds)
        stale = repository.get_setting("stale", settings.alert_stale_after_seconds)
        message = (
            "Пароль изменён. Панель готова к работе." if request.query_params.get("ready") else ""
        )
        return HTMLResponse(
            _dashboard_html(session.csrf_token, settings.environment.value, poll, stale, message)
        )

    @app.get("/api/dashboard")
    async def dashboard_api(request: Request) -> JSONResponse:
        session, _ = session_for(request)
        if not session or session.must_change_password:
            return JSONResponse({"detail": "unauthorized"}, 401)
        return JSONResponse(
            {
                "generated_at": utcnow().isoformat(),
                "host": collector.collect(),
                "provider": {
                    "state": "UNKNOWN",
                    "poll_interval_seconds": repository.get_setting(
                        "poll", settings.alert_poll_interval_seconds
                    ),
                    "stale_after_seconds": repository.get_setting(
                        "stale", settings.alert_stale_after_seconds
                    ),
                    "http_counts": None,
                    "rate_budget": None,
                    "singleton_lease": None,
                },
                "aggregates": None,
                "workers": None,
            }
        )

    @app.post("/settings/provider")
    async def save_provider(request: Request) -> Response:
        session, _ = session_for(request, False)
        if not session or session.must_change_password:
            return redirect_login(True)
        data = _form(await request.body())
        if not require_csrf(session, data.get("csrf", "")):
            return Response(status_code=403)
        try:
            poll, stale = int(data["poll"]), int(data["stale"])
        except (KeyError, ValueError):
            return HTMLResponse("Невірне значення", 422)
        if poll < 15 or stale < 2 * poll:
            return HTMLResponse(
                "Poll interval має бути не менше 15 с; stale threshold — не менше 2 × poll.", 422
            )
        repository.set_settings(
            {"poll": poll, "stale": stale}, session.username, "provider_settings"
        )
        return RedirectResponse("/?saved=1#settings", 303)

    @app.get("/help/{document}", response_class=HTMLResponse)
    async def help_document(document: str, request: Request) -> Response:
        session, _ = session_for(request)
        if not session or session.must_change_password:
            return redirect_login(True)
        allowed = {"setup": Path("docs/SETUP_BOT.md"), "admin": Path("docs/ADMIN_PANEL.md")}
        path = allowed.get(document)
        if not path or not path.exists():
            return HTMLResponse("Інструкція ще не створена.", 404)
        return HTMLResponse(
            f"<html><body><main><h1>Довідка</h1><pre>{html.escape(path.read_text(encoding='utf-8'))}</pre></main></body></html>"
        )

    @app.get("/static/admin.css")
    async def css() -> Response:
        return Response(_CSS, media_type="text/css")

    @app.get("/static/admin.js")
    async def js() -> Response:
        return Response(_JS, media_type="application/javascript")

    return app


def run_admin_web(settings: Settings | None = None) -> None:
    active = settings or get_settings()
    uvicorn.run(
        create_admin_app(active),
        host=active.admin_host,
        port=active.admin_port,
        log_config=None,
        access_log=False,
    )


_CSS = Path(__file__).with_name("static").joinpath("admin.css").read_text(encoding="utf-8")
_JS = Path(__file__).with_name("static").joinpath("admin.js").read_text(encoding="utf-8")
