"""FastAPI owner panel backed by local sanitized state only."""
# ruff: noqa: E501  # audited embedded HTML/CSP literals

from __future__ import annotations

import html
import math
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any
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


def _project_setup_guide_html() -> str:
    """Render operator guidance without reading or exposing secret values."""

    return '''<section id="project-setup" aria-labelledby="project-setup-title"><h2 id="project-setup-title">Налаштування проєкту</h2><p class="setup-intro">Це контрольний список для власника. Інфраструктурні секрети додавайте лише в захищені secrets сервера. Telegram Bot Token і токен alerts.in.ua вводьте у write-only поля майстра: панель показує лише факт налаштування й ніколи не повертає значення.</p><div class="setup-warning" role="note"><strong>Починайте із SIMULATION.</strong> Перехід до STAGING або PRODUCTION дозволений тільки після відповідних перевірок і окремого схвалення.</div><ol class="setup-steps"><li><strong>Telegram.</strong> Створіть окремого бота через BotFather. Введіть Telegram Bot Token у write-only поле майстра налаштування. Для STAGING вкажіть лише погоджені числові ID у <code>TEST_CHAT_ALLOWLIST</code>; сторонні чати не повинні отримувати повідомлення.</li><li><strong>Офіційні тривоги.</strong> Отримайте окремий токен alerts.in.ua та введіть його у write-only поле майстра. Не вставляйте токен у Git, логи або скриншоти.</li><li><strong>База та шифрування.</strong> Задайте довгий випадковий пароль PostgreSQL, узгоджений <code>DATABASE_URL</code> і стабільні <code>LESSON_LINK_FERNET_KEY</code>, <code>OWNER_TELEGRAM_FERNET_KEY</code> і окремий <code>OWNER_ALERTS_FERNET_KEY</code>. Fernet-ключі не змінюйте без окремого плану міграції.</li><li><strong>Захист панелі.</strong> Задайте стабільний <code>ADMIN_IP_HASH_KEY</code> щонайменше 32 байти, увімкніть secure cookie за HTTPS і внесіть тільки точні CIDR перевіреного reverse proxy. Після міграцій створіть одноразовий пароль адміністратора та одразу замініть його при першому вході.</li><li><strong>Область і onboarding.</strong> Для поточного staging використовуйте лише ручний вибір і явне підтвердження області. Геолокацію не вважайте готовою, доки resolver не пройде окремий review.</li><li><strong>Розклад.</strong> Перевірте активний профіль, уроки й перерви, пропуски, парність днів/тижнів, робочі суботи та винятки дат. Імпортований розклад застосовуйте тільки після перегляду й підтвердження учнем.</li><li><strong>Запуск і перевірка.</strong> Після міграцій перевірте provider freshness, heartbeats <code>bot</code>, <code>alert-worker</code>, <code>schedule-worker</code> і <code>sender-worker</code>, чергу outbox та метрики сервера. <code>UNKNOWN</code> або <code>STALE</code> ніколи не означає відбій.</li></ol><details><summary>Режими середовища</summary><dl class="setup-modes"><dt>SIMULATION</dt><dd>Fake provider, Telegram disabled, без реальних повідомлень.</dd><dt>STAGING</dt><dd>Окремий тестовий бот, офіційний provider і обов’язковий allowlist тестових chat ID.</dd><dt>PRODUCTION</dt><dd>Тільки після G8/G9, TLS, backup/restore, safety review і явного схвалення точної версії.</dd></dl></details><p><a href="/help/setup">Повна інструкція підключення бота</a> · <a href="/help/admin">Повна інструкція адміністратора</a></p></section>'''


def _owner_setup_html(csrf: str, summary: Any | None, runtime_environment: str) -> str:
    if summary is None:
        return '''<section id="service-setup"><h2>Майстер налаштування сервісу</h2><p class="critical">Захищене сховище ще не підключено. Додайте bootstrap-only ключ шифрування в Portainer і перезапустіть лише admin-web. Токени сюди поки не вводьте.</p></section>'''

    def selected(actual: str, expected: str) -> str:
        return " selected" if actual == expected else ""

    revision = int(summary.revision)
    status = html.escape(str(summary.activation_state))
    issues = "".join(f"<li>{html.escape(item)}</li>" for item in summary.validation_issues)
    issue_block = f'<ul class="validation-list">{issues}</ul>' if issues else ""
    if summary.activation_state == "ACTIVE":
        issue_block = (
            '<p class="setup-warning">Версія позначена ACTIVE у сховищі, але це не '
            'підтверджує, що всі сервіси вже її застосували. Перевірте heartbeat і '
            'застосовану версію після контрольованого перезапуску.</p>'
            + issue_block
        )
    telegram_state = "Налаштовано" if summary.telegram_token_configured else "Не налаштовано"
    alerts_state = "Налаштовано" if summary.alerts_in_ua_token_configured else "Не налаштовано"
    allowlist_state = (
        f"Налаштовано ID: {len(summary.staging_allowlist)}"
        if summary.staging_allowlist
        else "Не налаштовано"
    )
    activation = ""
    if revision > 0:
        activation = f'''<form method="post" action="/settings/service/request-activation" class="setup-actions" data-confirm="activation"><input type="hidden" name="csrf" value="{html.escape(csrf)}"><input type="hidden" name="expected_revision" value="{revision}"><label>Поточний пароль адміністратора<input type="password" name="current_password" autocomplete="current-password" required></label><label class="check"><input type="checkbox" name="acknowledge" value="yes" required> Я розумію: запит не запускає Telegram і не підтверджує production.</label><button type="submit"{' disabled' if not summary.ready_for_activation else ''}>Запросити активацію</button></form>'''
    return f'''<section id="service-setup"><h2>Майстер налаштування сервісу</h2><p>Поточний runtime: <strong>{html.escape(runtime_environment.upper())}</strong>. Версія: {revision}. Статус: <span class="setup-status">{status}</span>.</p><p class="setup-warning">Збереження створює лише чернетку. Реальна доставка не вмикається без окремої перевірки, перезапуску та дозволу.</p>{issue_block}<form method="post" action="/settings/service" class="setup-grid"><input type="hidden" name="csrf" value="{html.escape(csrf)}"><input type="hidden" name="expected_revision" value="{revision}"><label>Цільове середовище<select name="environment"><option value="simulation"{selected(summary.environment, 'simulation')}>SIMULATION</option><option value="staging"{selected(summary.environment, 'staging')}>STAGING</option><option value="production"{selected(summary.environment, 'production')}>PRODUCTION</option></select></label><label>Джерело тривог<select name="alert_provider"><option value="fake"{selected(summary.alert_provider, 'fake')}>Fake (лише simulation)</option><option value="alerts_in_ua"{selected(summary.alert_provider, 'alerts_in_ua')}>alerts.in.ua</option></select></label><label>Telegram transport<select name="telegram_transport"><option value="disabled"{selected(summary.telegram_transport, 'disabled')}>Вимкнено</option><option value="live"{selected(summary.telegram_transport, 'live')}>Live</option></select></label><label>Інтервал опитування, секунд<input type="number" name="poll_interval_seconds" min="15" max="300" value="{int(summary.poll_interval_seconds)}" required></label><label>Поріг застарівання, секунд<input type="number" name="stale_after_seconds" min="30" max="3600" value="{int(summary.stale_after_seconds)}" required></label><label>STAGING allowlist <span class="secret-state">{allowlist_state}</span><input name="staging_allowlist" inputmode="numeric" autocomplete="off" placeholder="Telegram chat ID через кому"><small>Порожньо — залишити поточний список.</small></label><label class="check"><input type="checkbox" name="clear_staging_allowlist" value="yes"> Очистити allowlist</label><label>Telegram Bot Token <span class="secret-state">{telegram_state}</span><input type="password" name="telegram_token" autocomplete="new-password" spellcheck="false"><small>Порожньо — не змінювати.</small></label><label class="check"><input type="checkbox" name="clear_telegram_token" value="yes"> Видалити Telegram token</label><label>alerts.in.ua Token <span class="secret-state">{alerts_state}</span><input type="password" name="alerts_in_ua_token" autocomplete="new-password" spellcheck="false"><small>Порожньо — не змінювати.</small></label><label class="check"><input type="checkbox" name="clear_alerts_in_ua_token" value="yes"> Видалити alerts.in.ua token</label><label>Поточний пароль адміністратора<input type="password" name="current_password" autocomplete="current-password" required></label><button type="submit">Зберегти чернетку</button></form>{activation}<p><small>Пароль PostgreSQL, ключ шифрування, SSH і TLS налаштовуються тільки в інфраструктурі та не приймаються цією формою.</small></p></section>'''


def _dashboard_html(
    csrf: str,
    environment: str,
    poll: int,
    stale: int,
    message: str = "",
    owner_setup: Any | None = None,
) -> str:
    notice = f'<div class="notice" role="status">{html.escape(message)}</div>' if message else ""
    notice += _owner_setup_html(csrf, owner_setup, environment)
    return f'''<!doctype html><html lang="uk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Time TL — Панель</title><link rel="stylesheet" href="/static/admin.css"></head><body><a class="skip" href="#content">До вмісту</a><div class="shell"><aside><strong>Time TL</strong><nav aria-label="Розділи"><a href="#overview">Огляд</a><a href="#provider">Тривоги та провайдер</a><a href="#delivery">Доставка</a><a href="#users">Користувачі</a><a href="#server">Сервер і воркери</a><a href="#settings">Налаштування</a><a href="#help">Довідка</a></nav></aside><div><header><span>Time TL · {html.escape(environment.upper())}</span><span id="aggregate">Стан не підтверджено</span><form method="post" action="/logout"><input type="hidden" name="csrf" value="{csrf}"><button>Вийти</button></form></header><main id="content"><h1>Огляд</h1>{notice}<div class="critical" role="status">Стан тривог не підтверджено. Це не означає відбій.</div><button id="refresh">Оновити дані</button><p id="updated">Метрики ще не отримано</p><section id="overview" class="grid"><article><h2>Користувачі</h2><strong>Дані недоступні</strong><p>Агрегований PostgreSQL backend ще не підключено.</p></article><article><h2>Активні тривоги</h2><strong>Стан не підтверджено</strong></article><article><h2>Черга</h2><strong>Дані недоступні</strong></article><article><h2>Telegram</h2><strong>Дані недоступні</strong></article></section><section id="provider"><h2>Тривоги та провайдер</h2><p><b>UNKNOWN</b> · Нові відбої заблоковано</p><dl><dt>Інтервал опитування</dt><dd>{poll} с</dd><dt>Поріг застарівання</dt><dd>{stale} с</dd><dt>Ліміт</dt><dd>5 запитів/хв, burst 1</dd><dt>200 / 304 / 429 / 401-403 / 5xx</dt><dd>Дані недоступні</dd><dt>Singleton lease</dt><dd>Дані недоступні</dd></dl><p>У підтвердженому знімку немає активних тривог</p><p class="critical">Це не підтверджує відбій. Дані провайдера застаріли або недоступні.</p></section><section id="delivery"><h2>Доставка</h2><p>Outbox і Telegram: агрегати недоступні.</p><button id="resume-open" disabled>Відновити надсилання</button></section><section id="users"><h2>Користувачі</h2><p>Тільки агрегати; персональні дані та розклади не відображаються.</p></section><section id="server"><h2>Сервер і воркери</h2><div id="metrics" class="grid"><article>CPU: Метрики недоступні</article><article>RAM: Метрики недоступні</article><article>Disk: Метрики недоступні</article><article>Load: Метрики недоступні</article></div><p>Джерело: read-only host exporter</p><table><caption>Воркери</caption><thead><tr><th>Процес</th><th>Heartbeat</th><th>Перезапуски</th></tr></thead><tbody><tr><td>bot</td><td>Немає heartbeat</td><td>Дані недоступні</td></tr><tr><td>alert-worker</td><td>Немає heartbeat</td><td>Дані недоступні</td></tr><tr><td>schedule-worker</td><td>Немає heartbeat</td><td>Дані недоступні</td></tr><tr><td>sender-worker</td><td>Немає heartbeat</td><td>Дані недоступні</td></tr><tr><td>metrics collector</td><td id="metrics-heartbeat">Немає heartbeat</td><td>Дані недоступні</td></tr></tbody></table></section><section id="settings"><h2>Налаштування</h2><p>Інтервали та write-only токени змінюються тільки у версійованому майстрі вище. Старий прямий маршрут вимкнено.</p></section><section id="help"><h2>Довідка</h2><a href="/help/setup">Налаштування Telegram-бота</a><a href="/help/admin">Інструкція адміністратора</a><p>LOCAL, SIMULATION, STAGING і PRODUCTION — окремі рівні доказів.</p></section></main></div></div><dialog id="resume" role="alertdialog" aria-labelledby="resume-title"><h2 id="resume-title">Відновити надсилання</h2><p>У черзі: дані недоступні. Вік найстарішого: дані недоступні.</p><p>После возобновления накопленные сообщения могут быть отправлены немедленно.</p><form method="dialog"><button autofocus>Отмена</button><button disabled>Возобновить отправку</button></form></dialog><script src="/static/admin.js" defer></script></body></html>'''


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
        return HTMLResponse(
            "Цей маршрут вимкнено. Налаштування провайдера не можна змінювати в обхід "
            "версійованої активації.",
            410,
        )

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
