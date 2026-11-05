"""Owner-only redacted power monitoring; manual refresh only queues a request."""

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

from time_tl.domain.power_outages.models import PowerProvider, PowerSourceKind, SourceStatus
from time_tl.domain.power_outages.sources import ComparisonStatus

from .security import Session, utcnow


class PowerAdminConflict(ValueError):
    """The provider control revision changed; reload before another mutation."""


class PowerAdminPort(Protocol):
    async def stats(self, now: datetime) -> tuple[dict[str, object], ...]: ...

    async def set_enabled(
        self,
        provider: PowerProvider,
        enabled: bool,
        *,
        expected_revision: int,
        actor_id: str,
        now: datetime,
    ) -> int: ...

    async def request_refresh(
        self,
        provider: PowerProvider,
        *,
        actor_id: str,
        now: datetime,
    ) -> bool: ...


class PowerSourceAdminPort(Protocol):
    async def stats(self, now: datetime) -> tuple[dict[str, object], ...]: ...

    async def set_source_enabled(
        self,
        provider: PowerProvider,
        source_kind: PowerSourceKind,
        enabled: bool,
        *,
        expected_revision: int,
        actor_id: str,
        now: datetime,
    ) -> int: ...

    async def request_source_refresh(
        self,
        provider: PowerProvider,
        source_kind: PowerSourceKind,
        *,
        actor_id: str,
        now: datetime,
    ) -> bool: ...


AGGREGATE_SOURCES = frozenset(
    {
        PowerSourceKind.BEZSVITLA,
        PowerSourceKind.ALERTS_ENERGY_HTML,
        PowerSourceKind.ALERTS_ENERGY_API,
    }
)
SOURCE_NAMES = {
    PowerSourceKind.OFFICIAL: "Официальный оператор",
    PowerSourceKind.BEZSVITLA: "Bezsvitla — дополнительный источник",
    PowerSourceKind.ALERTS_ENERGY_HTML: "Alerts Energy — HTML",
    PowerSourceKind.ALERTS_ENERGY_API: "Alerts Energy — API (основной источник)",
}
PUBLIC_ERROR_CODES = frozenset(
    {
        "SOURCE_POLICY_UNVERIFIED",
        "SOURCE_DATE_CONTRACT_UNVERIFIED",
        "SOURCE_GATE_BLOCKED",
        "BLOCKED_POLICY",
        "REFERENCE_UNVERIFIED",
        "FETCH_UNAVAILABLE",
        "INVALID_RESPONSE",
        "ACCESS_DENIED",
        "RATE_LIMITED",
        "TIMEOUT",
        "CONNECTION_ERROR",
        "HTTP_ERROR",
    }
)


def public_source_statistics(
    rows: tuple[dict[str, object], ...],
    now: datetime,
    *,
    stale_after_seconds: int = 1800,
) -> list[dict[str, object]]:
    if type(rows) is not tuple or len(rows) > len(PowerProvider) * len(PowerSourceKind):
        raise ValueError("invalid source statistics")
    output: list[dict[str, object]] = []
    seen: set[tuple[PowerProvider, PowerSourceKind]] = set()
    for row in rows:
        provider = PowerProvider(str(row["provider_id"]))
        source = PowerSourceKind(str(row["source_kind"]))
        if (provider, source) in seen:
            raise ValueError("duplicate source statistics")
        seen.add((provider, source))
        base = public_statistics((row,), now, stale_after_seconds=stale_after_seconds)[0]
        item = {
            key: base[key]
            for key in (
                "provider_id",
                "enabled",
                "source_status",
                "last_success_at",
                "last_attempt_at",
                "revision",
            )
        }
        item.update({"source_kind": source.value, "collection_allowed": False})
        for key in ("priority", "compared_count", "matched_count"):
            value = row.get(key)
            item[key] = value if type(value) is int and 0 <= value <= 2**63 - 1 else None
        compared, matched = item["compared_count"], item["matched_count"]
        valid_counts = type(compared) is int and type(matched) is int and 0 <= matched <= compared
        item["match_percent"] = (
            100 * matched / compared
            if valid_counts
            and isinstance(compared, int)
            and isinstance(matched, int)
            and compared > 0
            else None
        )
        verification = row.get("verification_status")
        item["verification_status"] = (
            str(verification)
            if valid_counts and compared != 0 and verification in set(ComparisonStatus)
            else ComparisonStatus.UNVERIFIED.value
        )
        error = row.get("last_error_code")
        item["last_error_code"] = (
            error
            if type(error) is str and error in PUBLIC_ERROR_CODES
            else "SOURCE_ERROR"
            if error
            else None
        )
        output.append(item)
    return output


def _time(value: object, now: datetime) -> str | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and len(value) <= 64:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed > now:
        return None
    return parsed.astimezone(UTC).isoformat()


def public_statistics(
    rows: tuple[dict[str, object], ...],
    now: datetime,
    *,
    stale_after_seconds: int = 1800,
) -> list[dict[str, object]]:
    if type(rows) is not tuple or len(rows) > len(PowerProvider):
        raise ValueError("invalid power statistics")
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in rows:
        provider = PowerProvider(str(row["provider_id"]))
        if provider.value in seen:
            raise ValueError("duplicate power statistics")
        seen.add(provider.value)
        raw_status = row.get("source_status")
        status = raw_status if raw_status in set(SourceStatus) else SourceStatus.UNAVAILABLE
        last_success = _time(row.get("last_success_at"), now)
        if status == SourceStatus.FRESH:
            if last_success is None:
                status = SourceStatus.UNAVAILABLE
            elif (now - datetime.fromisoformat(last_success)).total_seconds() > stale_after_seconds:
                status = SourceStatus.STALE
        public: dict[str, object] = {
            "provider_id": provider.value,
            "name": provider.label,
            "enabled": row.get("enabled") if type(row.get("enabled")) is bool else None,
            "source_status": str(status),
            "last_success_at": last_success,
            "last_attempt_at": _time(row.get("last_attempt_at"), now),
            # The renderer never exports raw exceptions, URLs, source HTML or arbitrary error text.
            "sync_error": bool(row.get("last_error_code")),
        }
        for key in (
            "group_count",
            "schedule_count",
            "subscriber_count",
            "pending_notifications",
            "revision",
        ):
            value = row.get(key)
            public[key] = value if type(value) is int and 0 <= value <= 2**63 - 1 else None
        result.append(public)
    return result


def power_html(
    csrf: str,
    rows: list[dict[str, object]],
    sources: list[dict[str, object]] | None = None,
) -> str:
    cards = []
    indexed = {str(row["provider_id"]): row for row in rows}
    for provider in PowerProvider:
        row = indexed.get(provider.value)
        if row is None:
            cards.append(
                f"<article><h2>{html.escape(provider.label)}</h2>"
                "<p>Данные недоступны.</p></article>"
            )
            continue
        controls = ""
        if type(row.get("revision")) is int and type(row.get("enabled")) is bool:
            hidden = (
                f'<input type="hidden" name="csrf" value="{html.escape(csrf, quote=True)}">'
                f'<input type="hidden" name="provider" value="{provider.value}">'
                f'<input type="hidden" name="expected_revision" value="{row["revision"]}">'
                '<label>Текущий пароль владельца<input type="password" '
                'name="current_password" autocomplete="current-password" required></label>'
            )
            enabled = "false" if row["enabled"] else "true"
            controls = (
                f'<form method="post" action="/power/refresh">{hidden}'
                "<button>Запросить обновление</button></form>"
                f'<form method="post" action="/power/enabled">{hidden}'
                f'<input type="hidden" name="enabled" value="{enabled}">'
                f"<button>{'Отключить' if row['enabled'] else 'Включить'} источник</button></form>"
            )
        entries = {
            "Активен": ("Да" if row["enabled"] else "Нет") if row["enabled"] is not None else None,
            "Статус источника": row["source_status"],
            "Успешное обновление": row["last_success_at"],
            "Последняя попытка": row["last_attempt_at"],
            "Групп": row["group_count"],
            "Графиков": row["schedule_count"],
            "Подписчиков": row["subscriber_count"],
            "Ожидающих уведомлений": row["pending_notifications"],
            "Ошибка синхронизации": "Есть" if row["sync_error"] else "Нет",
        }
        details = "".join(
            f"<dt>{html.escape(key)}</dt><dd>"
            + (html.escape(str(value)) if value is not None else "Данные недоступны")
            + "</dd>"
            for key, value in entries.items()
        )
        cards.append(
            f"<article><h2>{html.escape(provider.label)}</h2><dl>{details}</dl>{controls}</article>"
        )
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<link rel="stylesheet" href="/static/admin.css"><title>Time TL — Электроэнергия</title>'
        "</head><body><main><h1>⚡ Графіки електроенергії</h1>"
        '<p><a href="/power/alerts-energy-api">Настроить API-ключ Alerts Energy</a></p>'
        "<p>Обновление централизовано. Кнопка не обходит cooldown и ограничения источника. "
        "График не подтверждает фактическое наличие света.</p>"
        + "".join(cards)
        + _source_cards(csrf, sources or [])
        + '<p><a href="/">Главная панель</a></p></main></body></html>'
    )


def _source_cards(csrf: str, sources: list[dict[str, object]]) -> str:
    if not sources:
        return ""
    result = [
        "<section><h2>Источники графиков</h2><p>Сбор данных запрещён до проверки "
        "условий источника. Включение сохраняет только намерение владельца и не "
        "разрешает сбор данных или отправку уведомлений.</p>"
    ]
    for row in sources:
        provider = PowerProvider(str(row["provider_id"]))
        source = PowerSourceKind(str(row["source_kind"]))
        title = SOURCE_NAMES[source] + " · " + provider.label
        values = {
            "Запрошено включение": (
                "Да" if row["enabled"] is True else "Нет" if row["enabled"] is False else None
            ),
            "Сбор данных разрешён": "Нет — проверка условий не пройдена",
            "Приоритет": row["priority"],
            "Доступность": row["source_status"],
            "Проверка совпадений": row["verification_status"],
            "Совпадение, %": row["match_percent"],
            "Сравнено": row["compared_count"],
            "Совпало": row["matched_count"],
            "Успешное обновление": row["last_success_at"],
            "Последняя попытка": row["last_attempt_at"],
            "Код ошибки": row["last_error_code"],
        }
        details = "".join(
            f"<dt>{html.escape(label)}</dt><dd>"
            + (html.escape(str(value)) if value is not None else "Неизвестно")
            + "</dd>"
            for label, value in values.items()
        )
        controls = ""
        if (
            source in AGGREGATE_SOURCES
            and type(row["revision"]) is int
            and type(row["enabled"]) is bool
        ):
            hidden = (
                f'<input type="hidden" name="csrf" value="{html.escape(csrf, quote=True)}">'
                f'<input type="hidden" name="provider" value="{provider.value}">'
                f'<input type="hidden" name="source_kind" value="{source.value}">'
                f'<input type="hidden" name="expected_revision" value="{row["revision"]}">'
                '<label>Текущий пароль владельца<input type="password" name="current_password" '
                'autocomplete="current-password" required></label>'
            )
            enabled = "false" if row["enabled"] else "true"
            controls = (
                f'<form method="post" action="/power/source-enabled">{hidden}'
                f'<input type="hidden" name="enabled" value="{enabled}">'
                "<button>Изменить настройку включения</button></form>"
                f'<form method="post" action="/power/source-refresh">{hidden}'
                "<button>Запросить проверку без обхода ограничений</button></form>"
            )
        result.append(
            f"<article><h3>{html.escape(title)}</h3><dl>{details}</dl>{controls}</article>"
        )
    return "".join(result) + "</section>"


def register_power_admin(
    app: FastAPI,
    port: PowerAdminPort | None,
    *,
    session_for: Callable[[Request, bool], Awaitable[tuple[Session | None, str | None]]],
    reauthenticate: Callable[[Request, Session, str], Awaitable[tuple[bool, timedelta | None]]],
    stale_after_seconds: int = 1800,
    source_port: PowerSourceAdminPort | None = None,
) -> None:
    async def authorized(request: Request) -> Session | None:
        session, _ = await session_for(request, False)
        if session is None or session.must_change_password or session.username != "admin":
            return None
        return session

    @app.get("/power", response_class=HTMLResponse)
    @app.get("/api/power")
    async def power_page(request: Request) -> Response:
        session = await authorized(request)
        if session is None:
            return (
                JSONResponse({"detail": "unauthorized"}, 401)
                if request.url.path.startswith("/api/")
                else RedirectResponse("/login", 303)
            )
        try:
            if port is None:
                raise ValueError("power admin unavailable")
            now = utcnow()
            async with asyncio.timeout(5):
                rows = public_statistics(
                    await port.stats(now),
                    now,
                    stale_after_seconds=stale_after_seconds,
                )
                sources = (
                    public_source_statistics(
                        await source_port.stats(now), now, stale_after_seconds=stale_after_seconds
                    )
                    if source_port is not None
                    else []
                )
        except Exception:
            return JSONResponse({"detail": "power_data_unavailable"}, 503)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"providers": rows, "sources": sources})
        return HTMLResponse(power_html(session.csrf_token, rows, sources))

    @app.post("/power/refresh")
    @app.post("/power/enabled")
    @app.post("/power/source-enabled")
    @app.post("/power/source-refresh")
    async def power_change(request: Request) -> Response:
        session = await authorized(request)
        if session is None:
            return JSONResponse({"detail": "unauthorized"}, 401)
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 8192:
                return Response(status_code=413)
            raw.extend(chunk)
        try:
            parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=8)
            if any(len(values) != 1 for values in parsed.values()) or set(parsed) - {
                "csrf",
                "current_password",
                "provider",
                "expected_revision",
                "enabled",
                "source_kind",
            }:
                raise ValueError("invalid power form")
            form = {key: values[0] for key, values in parsed.items()}
        except (ValueError, UnicodeDecodeError):
            return Response(status_code=422)
        if not compare_digest(
            session.csrf_token.encode("utf-8"), form.get("csrf", "").encode("utf-8")
        ):
            return Response(status_code=403)
        valid, cooldown = await reauthenticate(request, session, form.get("current_password", ""))
        if not valid:
            return Response(status_code=429 if cooldown else 401)
        try:
            provider = PowerProvider(form.get("provider", ""))
            revision = form.get("expected_revision", "")
            if not revision.isascii() or not revision.isdecimal() or len(revision) > 10:
                raise ValueError("invalid provider revision")
            expected = int(revision)
            if expected > 2**31 - 1:
                raise ValueError("invalid provider revision")
            enabled = form.get("enabled")
            source_change = request.url.path in {"/power/source-enabled", "/power/source-refresh"}
            enable_change = request.url.path in {"/power/enabled", "/power/source-enabled"}
            source_kind = PowerSourceKind(form.get("source_kind", "")) if source_change else None
            if source_change and source_kind not in AGGREGATE_SOURCES:
                raise ValueError("invalid aggregate source")
            if source_change and expected < 1:
                raise ValueError("invalid aggregate source revision")
            if not source_change and "source_kind" in form:
                raise ValueError("unexpected aggregate source")
            if enable_change and enabled not in {"true", "false"}:
                raise ValueError("invalid provider enabled flag")
        except ValueError:
            return Response(status_code=422)
        if port is None or (source_change and source_port is None):
            return JSONResponse({"detail": "power_data_unavailable"}, 503)
        try:
            async with asyncio.timeout(5):
                if source_change:
                    if source_port is None or source_kind is None:
                        raise ValueError("source admin unavailable")
                    if enable_change:
                        await source_port.set_source_enabled(
                            provider,
                            source_kind,
                            enabled == "true",
                            expected_revision=expected,
                            actor_id=session.username,
                            now=utcnow(),
                        )
                    elif not await source_port.request_source_refresh(
                        provider,
                        source_kind,
                        actor_id=session.username,
                        now=utcnow(),
                    ):
                        return JSONResponse({"detail": "power_refresh_deferred"}, 429)
                elif enable_change:
                    await port.set_enabled(
                        provider,
                        enabled == "true",
                        expected_revision=expected,
                        actor_id=session.username,
                        now=utcnow(),
                    )
                elif not await port.request_refresh(
                    provider, actor_id=session.username, now=utcnow()
                ):
                    return JSONResponse({"detail": "power_refresh_deferred"}, 429)
        except PowerAdminConflict:
            return JSONResponse({"detail": "power_settings_changed"}, 409)
        except Exception:
            return JSONResponse({"detail": "power_data_unavailable"}, 503)
        return RedirectResponse("/power?saved=1", 303)
