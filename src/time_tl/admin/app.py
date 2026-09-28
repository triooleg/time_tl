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
from .settings import DEFAULT_ONBOARDING_TEXT

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
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Time TL — Вход</title><link rel="stylesheet" href="/static/admin.css"></head><body class="login"><main><form method="post" action="/login" class="login-card"><h1>Вход для владельца</h1>{notice}<input type="hidden" name="csrf" value="{html.escape(csrf)}"><label>Имя пользователя<input name="username" value="admin" autocomplete="username" required></label><label>Пароль<input type="password" name="password" autocomplete="current-password" required></label><button type="submit">Войти</button></form></main></body></html>'''


def _change_html(csrf: str, message: str = "") -> str:
    notice = f'<p class="notice" role="alert">{html.escape(message)}</p>' if message else ""
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Time TL — Новый пароль</title><link rel="stylesheet" href="/static/admin.css"></head><body class="login"><main><form method="post" action="/change-password" class="login-card"><h1>Создайте постоянный пароль</h1>{notice}<input type="hidden" name="csrf" value="{html.escape(csrf)}"><label>Новый пароль<input type="password" name="password" minlength="14" autocomplete="new-password" required></label><label>Повторите пароль<input type="password" name="confirmation" minlength="14" autocomplete="new-password" required></label><button type="submit">Сохранить пароль</button></form></main></body></html>'''


def _owner_setup_html(csrf: str, summary: Any | None, runtime_environment: str) -> str:
    if summary is None:
        return """<section id="service-setup"><h2>Мастер настройки сервиса</h2><p class="critical">Защищённое хранилище ещё не подключено. Добавьте bootstrap-only ключ шифрования в Portainer и перезапустите только admin-web. Пока не вводите здесь токены.</p></section>"""

    def selected(actual: str, expected: str) -> str:
        return " selected" if actual == expected else ""

    revision = int(summary.revision)
    status_labels = {
        "DRAFT": "Черновик (DRAFT)",
        "PENDING_ACTIVATION": "Ожидает активации (PENDING_ACTIVATION)",
        "ACTIVE": "Активна (ACTIVE)",
    }
    status = html.escape(
        status_labels.get(str(summary.activation_state), str(summary.activation_state))
    )
    issues = "".join(f"<li>{html.escape(item)}</li>" for item in summary.validation_issues)
    issue_block = f'<ul class="validation-list">{issues}</ul>' if issues else ""
    if summary.activation_state == "ACTIVE":
        issue_block = (
            '<p class="setup-warning">Версия отмечена как активная (ACTIVE) в хранилище, '
            "но это не подтверждает, что все сервисы уже её применили. Проверьте heartbeat "
            "и применённую версию после контролируемого перезапуска.</p>" + issue_block
        )
    telegram_state = "Настроено" if summary.telegram_token_configured else "Не настроено"
    onboarding_text = html.escape(str(getattr(summary, "onboarding_text", DEFAULT_ONBOARDING_TEXT)))
    alerts_state = "Настроено" if summary.alerts_in_ua_token_configured else "Не настроено"
    allowlist_state = (
        f"Настроено ID: {len(summary.staging_allowlist)}"
        if summary.staging_allowlist
        else "Не настроено"
    )
    activation = ""
    same_staging = summary.environment == runtime_environment == "staging"
    if revision > 0:
        expected_ack = "АКТИВУВАТИ STAGING"
        activation = f'''<form method="post" action="/settings/service/activate" class="setup-actions" data-confirm="activation"><input type="hidden" name="csrf" value="{html.escape(csrf)}"><input type="hidden" name="expected_revision" value="{revision}"><label>Текущий пароль администратора<input type="password" name="current_password" autocomplete="current-password" required></label><label>Введите точную фразу <code>{expected_ack}</code><input name="activation_ack" autocomplete="off" required></label><small>Панель выполнит только read-only проверки Telegram getMe и полного снимка alerts.in.ua. Сообщения не отправляются, снимок не сохраняется. PRODUCTION этим маршрутом не включается.</small><button type="submit"{" disabled" if not (summary.ready_for_activation and same_staging) else ""}>Проверить и активировать STAGING</button></form>'''
        if summary.environment == "production" or runtime_environment == "production":
            activation += '<p class="critical">Переход или изменение PRODUCTION заблокированы: требуется отдельное одобрение точной версии релиза.</p>'
        elif summary.environment != runtime_environment:
            activation += '<p class="critical">Черновик относится к другой среде. Самоактивация запрещена.</p>'
    rollback_revision = getattr(summary, "rollback_revision", None)
    if rollback_revision and summary.active_revision:
        activation += f'''<form method="post" action="/settings/service/rollback" class="setup-actions"><input type="hidden" name="csrf" value="{html.escape(csrf)}"><input type="hidden" name="expected_active_revision" value="{int(summary.active_revision)}"><label>Текущий пароль администратора<input type="password" name="current_password" autocomplete="current-password" required></label><label>Введите точную фразу <code>ПОВЕРНУТИ {int(rollback_revision)}</code><input name="rollback_ack" autocomplete="off" required></label><button type="submit">Вернуть предыдущую активную версию {int(rollback_revision)}</button></form>'''
    result_labels = {
        "passed": "Проверка пройдена (passed)",
        "failed": "Проверка не пройдена (failed)",
        "rolled_back": "Выполнен откат (rolled_back)",
    }
    raw_result = str(getattr(summary, "last_activation_result", None) or "")
    activation_result = html.escape(result_labels.get(raw_result, raw_result or "ещё нет"))
    return f'''<section id="service-setup"><h2>Мастер настройки сервиса</h2><p>Текущая среда выполнения: <strong>{html.escape(runtime_environment.upper())}</strong>. Версия: {revision}. Статус: <span class="setup-status">{status}</span>.</p><p id="owner-convergence">Применение воркерами ещё не подтверждено.</p><p>Последний результат активации: <strong>{activation_result}</strong>.</p><p class="setup-warning">Сохранение создаёт только черновик. Для текущего STAGING владелец может выполнить read-only предварительную проверку и применить его без передачи пароля оператору. PRODUCTION остаётся отдельным шлюзом выпуска.</p>{issue_block}<form method="post" action="/settings/service" class="setup-grid"><input type="hidden" name="csrf" value="{html.escape(csrf)}"><input type="hidden" name="expected_revision" value="{revision}"><label>Целевая среда<select name="environment"><option value="simulation"{selected(summary.environment, "simulation")}>SIMULATION</option><option value="staging"{selected(summary.environment, "staging")}>STAGING</option><option value="production"{selected(summary.environment, "production")}>PRODUCTION</option></select></label><label>Источник тревог<select name="alert_provider"><option value="fake"{selected(summary.alert_provider, "fake")}>Fake (только SIMULATION)</option><option value="alerts_in_ua"{selected(summary.alert_provider, "alerts_in_ua")}>alerts.in.ua</option></select></label><label>Транспорт Telegram<select name="telegram_transport"><option value="disabled"{selected(summary.telegram_transport, "disabled")}>Отключён</option><option value="live"{selected(summary.telegram_transport, "live")}>Активный (live)</option></select></label><label>Интервал опроса, секунд<input type="number" name="poll_interval_seconds" min="15" max="300" value="{int(summary.poll_interval_seconds)}" required></label><label>Порог устаревания, секунд<input type="number" name="stale_after_seconds" min="30" max="3600" value="{int(summary.stale_after_seconds)}" required></label><label>STAGING allowlist (только тестовый режим) <span class="secret-state">{allowlist_state}</span><input name="staging_allowlist" inputmode="numeric" autocomplete="off" placeholder="Telegram chat ID через запятую"><small>Пусто — оставить текущий список. Для PRODUCTION список нужно явно очистить.</small></label><label class="check"><input type="checkbox" name="clear_staging_allowlist" value="yes"> Очистить allowlist</label><label>Telegram Bot Token <span class="secret-state">{telegram_state}</span><input type="password" name="telegram_token" autocomplete="new-password" spellcheck="false"><small>Пусто — не изменять.</small></label><label class="check"><input type="checkbox" name="clear_telegram_token" value="yes"> Удалить токен Telegram</label><label>alerts.in.ua Token <span class="secret-state">{alerts_state}</span><input type="password" name="alerts_in_ua_token" autocomplete="new-password" spellcheck="false"><small>Пусто — не изменять.</small></label><label class="check"><input type="checkbox" name="clear_alerts_in_ua_token" value="yes"> Удалить токен alerts.in.ua</label><label class="setup-wide">Краткая инструкция для первого сообщения<textarea name="onboarding_text" maxlength="1500" rows="10">{onboarding_text}</textarea><small>Опишите назначение бота и основные шаги. Отправляется как обычный текст без HTML. Пустое значение восстанавливает безопасный текст по умолчанию.</small></label><label>Текущий пароль администратора<input type="password" name="current_password" autocomplete="current-password" required></label><button type="submit">Сохранить черновик</button></form>{activation}<p><small>Пароль PostgreSQL, ключ шифрования, SSH и TLS настраиваются только в инфраструктуре и не принимаются этой формой.</small></p></section>'''


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
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Time TL — Панель</title><link rel="stylesheet" href="/static/admin.css"></head><body><a class="skip" href="#content">К содержимому</a><div class="shell"><aside><strong>Time TL</strong><nav aria-label="Разделы"><a href="#overview">Обзор</a><a href="#provider">Тревоги и провайдер</a><a href="#delivery">Доставка</a><a href="#users">Пользователи</a><a href="#server">Сервер и воркеры</a><a href="#settings">Настройки</a><a href="#help">Справка</a></nav></aside><div><header><span>Time TL · {html.escape(environment.upper())}</span><span id="aggregate">Состояние не подтверждено</span><form method="post" action="/logout"><input type="hidden" name="csrf" value="{html.escape(csrf)}"><button>Выйти</button></form></header><main id="content"><h1>Обзор</h1>{notice}<div class="critical" role="status">Состояние тревог не подтверждено. Это не означает отбой.</div><button id="refresh">Обновить данные</button><p id="updated">Метрики ещё не получены</p><section id="overview" class="grid"><article><h2>Пользователи</h2><strong>Данные недоступны</strong><p>Агрегированные данные PostgreSQL ещё не подключены.</p></article><article><h2>Активные тревоги</h2><strong>Состояние не подтверждено</strong></article><article><h2>Очередь</h2><strong>Данные недоступны</strong></article><article><h2>Telegram</h2><strong>Данные недоступны</strong></article></section><section id="provider"><h2>Тревоги и провайдер</h2><p><b>UNKNOWN</b> · Новые отбои заблокированы</p><dl><dt>Интервал опроса</dt><dd>{poll} с</dd><dt>Порог устаревания</dt><dd>{stale} с</dd><dt>Лимит</dt><dd>5 запросов/мин, burst 1</dd><dt>200 / 304 / 429 / 401-403 / 5xx</dt><dd>Данные недоступны</dd><dt>Единственная аренда (singleton lease)</dt><dd>Данные недоступны</dd></dl><p>В подтверждённом снимке нет активных тревог</p><p class="critical">Это не подтверждает отбой. Данные провайдера устарели или недоступны.</p></section><section id="delivery"><h2>Доставка</h2><p>Outbox и Telegram: агрегаты недоступны.</p><button id="resume-open" disabled>Возобновить отправку</button></section><section id="users"><h2>Пользователи</h2><p>Только агрегаты; персональные данные и расписания не отображаются.</p></section><section id="server"><h2>Сервер и воркеры</h2><div id="metrics" class="grid"><article>CPU: Метрики недоступны</article><article>RAM: Метрики недоступны</article><article>Disk: Метрики недоступны</article><article>Load: Метрики недоступны</article></div><p>Источник: read-only экспортёр метрик хоста</p><table><caption>Воркеры</caption><thead><tr><th>Процесс</th><th>Heartbeat</th><th>Перезапуски</th></tr></thead><tbody><tr><td>bot</td><td>Нет сигнала активности (heartbeat)</td><td>Данные недоступны</td></tr><tr><td>alert-worker</td><td>Нет сигнала активности (heartbeat)</td><td>Данные недоступны</td></tr><tr><td>schedule-worker</td><td>Нет сигнала активности (heartbeat)</td><td>Данные недоступны</td></tr><tr><td>sender-worker</td><td>Нет сигнала активности (heartbeat)</td><td>Данные недоступны</td></tr><tr><td>сборщик метрик</td><td id="metrics-heartbeat">Нет сигнала активности (heartbeat)</td><td>Данные недоступны</td></tr></tbody></table></section><section id="settings"><h2>Настройки</h2><p>Интервалы и write-only токены изменяются только в версионированном мастере выше. Старый прямой маршрут отключён.</p></section><section id="help"><h2>Справка</h2><a href="/help/setup">Настройка Telegram-бота</a><a href="/help/admin">Инструкция администратора</a><p>LOCAL, SIMULATION, STAGING и PRODUCTION — отдельные уровни доказательств.</p></section></main></div></div><dialog id="resume" role="alertdialog" aria-labelledby="resume-title"><h2 id="resume-title">Возобновить отправку</h2><p>В очереди: данные недоступны. Возраст самого старого: данные недоступны.</p><p>После возобновления накопленные сообщения могут быть отправлены немедленно.</p><form method="dialog"><button autofocus>Отмена</button><button disabled>Возобновить отправку</button></form></dialog><script src="/static/admin.js" defer></script></body></html>'''


def _project_setup_guide_html() -> str:
    return """<section id="project-setup" aria-labelledby="project-setup-title"><h2 id="project-setup-title">Настройка проекта</h2><p class="setup-intro">Контрольный список владельца. Инфраструктурные секреты добавляйте только в защищённое хранилище секретов сервера. Telegram Bot Token и токен alerts.in.ua вводите в write-only поля мастера: панель показывает только состояние настройки и никогда не возвращает значения.</p><div class="setup-warning" role="note"><strong>Начинайте с SIMULATION.</strong> Переход к STAGING или PRODUCTION разрешён только после соответствующих проверок и отдельного одобрения.</div><ol class="setup-steps"><li><strong>Telegram.</strong> Создайте отдельного бота через BotFather. Для STAGING укажите только согласованные числовые ID в <code>TEST_CHAT_ALLOWLIST</code>; посторонние чаты не должны получать сообщения.</li><li><strong>Официальные тревоги.</strong> Получите отдельный токен alerts.in.ua и введите его в write-only поле мастера. Не помещайте токен в Git, журналы или снимки экрана.</li><li><strong>База данных и шифрование.</strong> Задайте случайный пароль PostgreSQL, согласованный <code>DATABASE_URL</code> и стабильные ключи <code>LESSON_LINK_FERNET_KEY</code>, <code>OWNER_TELEGRAM_FERNET_KEY</code> и <code>OWNER_ALERTS_FERNET_KEY</code>. Не меняйте ключи Fernet без отдельного плана миграции.</li><li><strong>Защита панели.</strong> Задайте стабильный <code>ADMIN_IP_HASH_KEY</code> длиной не менее 32 байт, включите secure cookie через HTTPS и укажите только точные CIDR проверенного reverse proxy. После миграций создайте одноразовый пароль администратора и замените его при первом входе.</li><li><strong>Регион и onboarding.</strong> Для текущего STAGING используйте ручной выбор и явное подтверждение области. Не считайте геолокацию готовой, пока resolver не прошёл отдельную проверку.</li><li><strong>Расписание.</strong> Проверьте активный профиль, уроки, перерывы, пропуски, чётность дней и недель, рабочие субботы и исключения дат. Применяйте импортированное расписание только после просмотра и подтверждения учеником.</li><li><strong>Запуск и проверка.</strong> После миграций проверьте свежесть данных провайдера, heartbeat процессов <code>bot</code>, <code>alert-worker</code>, <code>schedule-worker</code> и <code>sender-worker</code>, очередь outbox и метрики сервера. <code>UNKNOWN</code> или <code>STALE</code> никогда не означают отбой.</li></ol><details><summary>Режимы среды</summary><dl class="setup-modes"><dt>SIMULATION</dt><dd>Fake provider, Telegram отключён, реальных сообщений нет.</dd><dt>STAGING</dt><dd>Отдельный тестовый бот, официальный провайдер и обязательный allowlist тестовых chat ID.</dd><dt>PRODUCTION</dt><dd>Только после G8/G9, TLS, backup/restore, независимой проверки безопасности и явного одобрения точной версии.</dd></dl></details><p><a href="/help/setup">Полная инструкция по подключению бота</a> · <a href="/help/admin">Полная инструкция администратора</a></p></section>"""


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
            "Этот маршрут отключён. Настройки провайдера нельзя изменять в обход "
            "версионированной активации.",
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
            return HTMLResponse("Инструкция ещё не создана.", 404)
        return HTMLResponse(
            f"<html lang=\"ru\"><body><main><h1>Справка</h1><pre>{html.escape(path.read_text(encoding='utf-8'))}</pre></main></body></html>"
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
