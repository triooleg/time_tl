"""Async owner panel backed by the production PostgreSQL repository contract."""
# ruff: noqa: E501

from __future__ import annotations

import asyncio
import html
import ipaddress
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from secrets import compare_digest, token_urlsafe
from typing import Any, Protocol
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from time_tl.config import AdminBackend, Settings
from time_tl.entrypoints.operator_preflight import (
    OperatorPreflightError,
    OperatorPreflightEvidence,
    probe_candidate,
)

from .app import (
    _CSS,
    _JS,
    COOKIE,
    GENERIC_LOGIN_ERROR,
    LOGIN_CSRF,
    _change_html,
    _dashboard_html,
    _login_html,
)
from .metrics import HostMetricsCollector
from .security import (
    PasswordPolicyError,
    Session,
    hash_password,
    utcnow,
    validate_new_password,
    verify_password,
)
from .settings import (
    ActivationCandidate,
    OwnerConfigurationUpdate,
    OwnerSettingsConflictError,
    OwnerSettingsError,
    OwnerSettingsService,
    OwnerSettingsValidationError,
)


class AsyncAdminAccount(Protocol):
    username: str
    password_hash: str
    must_change_password: bool
    bootstrap_expires_at: datetime | None


class AsyncAdminRepository(Protocol):
    async def account(self, username: str = "admin") -> AsyncAdminAccount | None: ...

    def bootstrap_is_valid(self, account: AsyncAdminAccount, now: datetime) -> bool: ...

    async def lockout_remaining(
        self, username: str, source_ip: str, now: datetime
    ) -> timedelta | None: ...

    async def record_failure(self, username: str, source_ip: str, now: datetime) -> None: ...

    async def reset_failures(self, username: str, source_ip: str) -> None: ...

    async def create_session(self, username: str, must_change: bool, now: datetime) -> Session: ...

    async def get_session(
        self, token: str, now: datetime, touch: bool = True
    ) -> Session | None: ...

    async def delete_session(self, token: str) -> None: ...

    async def change_password(self, username: str, password_hash: str, now: datetime) -> None: ...

    async def get_setting(self, key: str, default: Any = None) -> Any: ...

    async def set_settings(self, values: dict[str, Any], owner: str, action: str) -> None: ...

    async def dashboard_aggregates(self) -> dict[str, Any]: ...


class MetricsCollector(Protocol):
    def collect(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ProxyPolicy:
    """Resolve a client address without trusting forwarding headers by default."""

    trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()

    @classmethod
    def from_cidrs(cls, values: tuple[str, ...]) -> ProxyPolicy:
        return cls(tuple(ipaddress.ip_network(value, strict=True) for value in values))

    def client_ip(self, request: Request) -> str:
        direct = request.client.host if request.client else "unknown"
        try:
            direct_ip = ipaddress.ip_address(direct)
        except ValueError:
            return direct
        if not any(direct_ip in network for network in self.trusted_networks):
            return direct
        forwarded = request.headers.get("x-forwarded-for")
        if not forwarded:
            return direct
        try:
            chain = [ipaddress.ip_address(item.strip()) for item in forwarded.split(",")]
        except ValueError:
            return direct
        if not chain:
            return direct
        for candidate in reversed(chain):
            if not any(candidate in network for network in self.trusted_networks):
                return str(candidate)
        return str(chain[0])


class AsyncMetricsCache:
    """Run a blocking collector off-loop and collapse concurrent scrapes."""

    def __init__(self, collector: MetricsCollector, *, ttl_seconds: float = 5.0) -> None:
        if not 1 <= ttl_seconds <= 60:
            raise ValueError("metrics cache TTL must be between 1 and 60 seconds")
        self._collector = collector
        self._ttl = ttl_seconds
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0
        self._lock = asyncio.Lock()

    async def collect(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._cached is not None and now - self._cached_at < self._ttl:
            return self._cached
        async with self._lock:
            now = time.monotonic()
            if self._cached is None or now - self._cached_at >= self._ttl:
                self._cached = await asyncio.to_thread(self._collector.collect)
                self._cached_at = time.monotonic()
            return self._cached


class BootstrapArtifactCleaner:
    """Delete the configured one-time plaintext file without persisting its path."""

    def __init__(self, path: str | Path | None) -> None:
        self._path = Path(path).resolve() if path else None

    async def cleanup(self) -> None:
        if self._path is not None:
            await asyncio.to_thread(self._path.unlink, missing_ok=True)


def _form(body: bytes) -> dict[str, str]:
    return {
        key: values[-1]
        for key, values in parse_qs(body.decode("utf-8"), keep_blank_values=True).items()
    }


def _duration(value: timedelta) -> str:
    seconds = max(1, math.ceil(value.total_seconds()))
    return f"{seconds // 60 + bool(seconds % 60)} мин."


def _safe_aggregates(raw: dict[str, Any]) -> dict[str, Any]:
    """Whitelist aggregate fields and omit instance IDs and free-form worker details."""

    def counts(value: Any) -> dict[str, int] | None:
        if not isinstance(value, dict):
            return None
        return {
            str(key): int(count)
            for key, count in value.items()
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0
        }

    allowed_workers = {"bot", "alert-worker", "schedule-worker", "sender-worker"}
    allowed_worker_states = {"healthy", "degraded", "critical", "unknown"}
    workers: list[dict[str, Any]] | None = None
    raw_workers = raw.get("workers")
    if isinstance(raw_workers, list):
        workers = []
        for item in raw_workers:
            if isinstance(item, dict) and item.get("name") in allowed_workers:
                status = str(item.get("status", "unknown"))
                worker = {
                    "name": str(item["name"]),
                    "status": status if status in allowed_worker_states else "unknown",
                    "heartbeat_at": item.get("heartbeat_at"),
                    "applied_config_revision": None,
                }
                details = item.get("details")
                if isinstance(details, dict):
                    revision = details.get("applied_config_revision")
                    if (
                        isinstance(revision, int)
                        and not isinstance(revision, bool)
                        and revision > 0
                    ):
                        worker["applied_config_revision"] = revision
                workers.append(worker)
    raw_provider = raw.get("provider")
    provider: dict[str, Any] | None = None
    if isinstance(raw_provider, dict):
        provider = {}
        for key in (
            "state",
            "persistence",
            "fetch_revision",
            "circuit_open",
            "consecutive_failures",
            "retry_not_before",
            "last_success_at",
        ):
            value = raw_provider.get(key)
            if isinstance(value, (str, int, bool)) or value is None:
                provider[key] = value
        provider["http_counts"] = counts(raw_provider.get("http_counts"))
        provider["rate_budget"] = None
        if isinstance(raw_provider.get("rate_budget"), (int, float)):
            provider["rate_budget"] = max(0, raw_provider["rate_budget"])

    raw_modes = counts(raw.get("modes"))
    modes: dict[str, int] | None = None
    if raw_modes is not None:
        modes = {}
        other = 0
        for key, value in raw_modes.items():
            if value < 5:
                other += value
            else:
                modes[key] = value
        if other:
            modes["other"] = other

    def optional_count(key: str) -> int | None:
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        return None

    return {
        "users": optional_count("users"),
        "profiles": optional_count("profiles"),
        "modes": modes,
        "alert_states": counts(raw.get("alert_states")),
        "outbox_pending": optional_count("outbox_pending"),
        "outbox_oldest_at": raw.get("outbox_oldest_at"),
        "workers": workers,
        "provider": provider,
        "outbox_delivered": optional_count("outbox_delivered"),
        "outbox_dead": optional_count("outbox_dead"),
        "outbox_retrying": optional_count("outbox_retrying"),
    }


def create_async_admin_app(
    settings: Settings,
    repository: AsyncAdminRepository,
    collector: MetricsCollector | None = None,
    *,
    owner_settings: OwnerSettingsService | None = None,
    proxy_policy: ProxyPolicy | None = None,
    bootstrap_artifact_path: str | Path | None = None,
    docs_root: str | Path = "docs",
    activation_preflight: Callable[[ActivationCandidate], Awaitable[OperatorPreflightEvidence]]
    | None = None,
) -> FastAPI:
    """Build the async app; persistence is mandatory and never downgraded."""

    if settings.admin_backend is not AdminBackend.POSTGRES:
        raise RuntimeError("async admin-web requires ADMIN_BACKEND=postgres")
    metrics = AsyncMetricsCache(collector or HostMetricsCollector(settings.runtime_state_dir))
    proxies = proxy_policy or ProxyPolicy()
    bootstrap_artifact = BootstrapArtifactCleaner(bootstrap_artifact_path)
    help_root = Path(docs_root).resolve()

    async def default_activation_preflight(
        candidate: ActivationCandidate,
    ) -> OperatorPreflightEvidence:
        if len(candidate.staging_allowlist) != 1:
            raise OperatorPreflightError(
                "STAGING self-activation requires exactly one approved chat ID"
            )
        from aiogram import Bot

        return await probe_candidate(
            candidate,
            candidate.staging_allowlist[0],
            bot_factory=Bot,
        )

    run_activation_preflight = activation_preflight or default_activation_preflight

    async def cleanup_bootstrap_at_expiry() -> None:
        account = await repository.account("admin")
        if (
            account is None
            or not account.must_change_password
            or account.bootstrap_expires_at is None
        ):
            return
        delay = max(0.0, (account.bootstrap_expires_at - utcnow()).total_seconds())
        if delay:
            await asyncio.sleep(delay)
        current = await repository.account("admin")
        if (
            current is not None
            and current.must_change_password
            and not repository.bootstrap_is_valid(current, utcnow())
        ):
            await bootstrap_artifact.cleanup()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        expiry_task = asyncio.create_task(cleanup_bootstrap_at_expiry())
        try:
            yield
        finally:
            if not expiry_task.done():
                expiry_task.cancel()
                try:
                    await expiry_task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

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
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            }
        )
        if settings.admin_cookie_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    async def session_for(
        request: Request, touch: bool = True
    ) -> tuple[Session | None, str | None]:
        token = request.cookies.get(COOKIE)
        return (
            await repository.get_session(token, utcnow(), touch) if token else None,
            token,
        )

    def redirect_login(expired: bool = False) -> RedirectResponse:
        response = RedirectResponse("/login?expired=1" if expired else "/login", 303)
        response.delete_cookie(COOKIE)
        return response

    async def owner_reauthenticated(
        request: Request, session: Session, supplied_password: str
    ) -> tuple[bool, timedelta | None]:
        source_ip, now = proxies.client_ip(request), utcnow()
        remaining = await repository.lockout_remaining(session.username, source_ip, now)
        if remaining:
            return False, remaining
        account = await repository.account(session.username)
        if account is None or not verify_password(account.password_hash, supplied_password):
            await repository.record_failure(session.username, source_ip, now)
            return False, await repository.lockout_remaining(session.username, source_ip, now)
        await repository.reset_failures(session.username, source_ip)
        return True, None

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> HTMLResponse:
        csrf = token_urlsafe(24)
        message = (
            "Сеанс завершён. Войдите снова."
            if request.query_params.get("expired")
            else "Пароль изменён. Панель готова к работе."
            if request.query_params.get("changed")
            else ""
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
        if not cookie_csrf or not compare_digest(cookie_csrf, data.get("csrf", "")):
            return Response(status_code=403)
        username = data.get("username", "")
        source_ip, now = proxies.client_ip(request), utcnow()
        remaining = await repository.lockout_remaining(username, source_ip, now)
        if remaining:
            return HTMLResponse(
                _login_html(
                    cookie_csrf,
                    f"Слишком много попыток входа. Повторите через {_duration(remaining)}",
                ),
                status_code=429,
            )
        account = await repository.account(username)
        bootstrap_valid = bool(account and repository.bootstrap_is_valid(account, now))
        if account and account.must_change_password and not bootstrap_valid:
            await bootstrap_artifact.cleanup()
        valid = bool(
            account
            and bootstrap_valid
            and verify_password(account.password_hash, data.get("password", ""))
        )
        if not valid:
            await repository.record_failure(username, source_ip, now)
            remaining = await repository.lockout_remaining(username, source_ip, now)
            if remaining:
                return HTMLResponse(
                    _login_html(
                        cookie_csrf,
                        f"Слишком много попыток входа. Повторите через {_duration(remaining)}",
                    ),
                    status_code=429,
                )
            return HTMLResponse(_login_html(cookie_csrf, GENERIC_LOGIN_ERROR), status_code=401)
        assert account is not None
        await repository.reset_failures(username, source_ip)
        session = await repository.create_session(username, account.must_change_password, now)
        response = RedirectResponse(
            "/change-password" if account.must_change_password else "/", 303
        )
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
        session, _ = await session_for(request)
        return HTMLResponse(_change_html(session.csrf_token)) if session else redirect_login(True)

    @app.post("/change-password")
    async def change_password(request: Request) -> Response:
        session, _ = await session_for(request, False)
        if not session:
            return redirect_login(True)
        data = _form(await request.body())
        if not compare_digest(session.csrf_token, data.get("csrf", "")):
            return Response(status_code=403)
        try:
            validate_new_password(data.get("password", ""), data.get("confirmation", ""))
        except PasswordPolicyError as exc:
            return HTMLResponse(_change_html(session.csrf_token, str(exc)), status_code=422)
        await repository.change_password(
            session.username, hash_password(data["password"]), utcnow()
        )
        await bootstrap_artifact.cleanup()
        response = RedirectResponse("/login?changed=1", 303)
        response.delete_cookie(COOKIE)
        return response

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        session, token = await session_for(request, False)
        data = _form(await request.body())
        if not session or not compare_digest(session.csrf_token, data.get("csrf", "")):
            return Response(status_code=403)
        if token:
            await repository.delete_session(token)
        response = RedirectResponse("/login", 303)
        response.delete_cookie(COOKIE)
        return response

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> Response:
        session, _ = await session_for(request)
        if not session:
            return redirect_login(True)
        if session.must_change_password:
            return RedirectResponse("/change-password", 303)
        poll = await repository.get_setting("poll", settings.alert_poll_interval_seconds)
        stale = await repository.get_setting("stale", settings.alert_stale_after_seconds)
        owner_view = None
        if owner_settings is not None:
            try:
                owner_view = await owner_settings.view()
            except OwnerSettingsError:
                owner_view = None
        saved_message = ""
        if request.query_params.get("setup") == "saved":
            saved_message = "Чернетку збережено. Реальні сервіси не активовано."
        elif request.query_params.get("setup") == "activated":
            saved_message = (
                "Read-only preflight пройдено. Версію позначено ACTIVE; "
                "дочекайтеся збігу applied revision усіх воркерів."
            )
        elif request.query_params.get("setup") == "rolled-back":
            saved_message = "Попередню активну версію відновлено. Дочекайтеся збігу heartbeat."
        page = _dashboard_html(
            session.csrf_token,
            settings.environment.value,
            int(poll),
            int(stale),
            saved_message
            or (
                "Налаштування збережено. Наступний цикл застосує нові значення."
                if request.query_params.get("saved")
                else ""
            ),
            owner_view,
        )
        page = page.replace(
            "Агрегований PostgreSQL backend ще не підключено.",
            "Очікуємо локальні агрегати.",
        )
        return HTMLResponse(page)

    @app.get("/api/dashboard")
    async def dashboard_api(request: Request) -> JSONResponse:
        session, _ = await session_for(request)
        if not session or session.must_change_password:
            return JSONResponse({"detail": "unauthorized"}, 401)
        poll = await repository.get_setting("poll", settings.alert_poll_interval_seconds)
        stale = await repository.get_setting("stale", settings.alert_stale_after_seconds)
        aggregates = _safe_aggregates(await repository.dashboard_aggregates())
        owner_public = None
        if owner_settings is not None:
            try:
                owner_public = (await owner_settings.view()).public_dict()
            except OwnerSettingsError:
                owner_public = {"status": "unavailable"}
        if owner_public is not None and isinstance(aggregates.get("workers"), list):
            active_revision = owner_public.get("active_revision")
            required_roles = {"bot", "alert-worker", "schedule-worker", "sender-worker"}
            applied = {
                str(worker.get("name")): worker.get("applied_config_revision")
                for worker in aggregates["workers"]
                if isinstance(worker, dict) and worker.get("name") in required_roles
            }
            matching = sorted(
                role for role in required_roles if applied.get(role) == active_revision
            )
            owner_public["convergence"] = {
                "state": (
                    "converged"
                    if active_revision is not None and len(matching) == len(required_roles)
                    else "partial"
                    if matching
                    else "pending"
                ),
                "active_revision": active_revision,
                "matching_roles": matching,
                "missing_or_mismatched_roles": sorted(required_roles - set(matching)),
            }
        sanitized_provider = aggregates.pop("provider")
        provider = dict(sanitized_provider) if isinstance(sanitized_provider, dict) else {}
        provider.update(
            {
                "poll_interval_seconds": int(poll),
                "stale_after_seconds": int(stale),
                "settings_apply_state": "next_cycle",
            }
        )
        last_success = provider.get("last_success_at")
        if isinstance(last_success, str):
            try:
                last_success_at = datetime.fromisoformat(last_success)
                if utcnow() - last_success_at > timedelta(seconds=int(stale)):
                    provider["state"] = "STALE"
                    provider["freshness"] = "expired"
                else:
                    provider["freshness"] = "fresh"
            except (TypeError, ValueError):
                provider["state"] = "STALE"
                provider["freshness"] = "invalid_timestamp"
        else:
            provider["state"] = "STALE" if provider.get("circuit_open") else "UNKNOWN"
            provider["freshness"] = "unavailable"
        return JSONResponse(
            {
                "generated_at": utcnow().isoformat(),
                "host": await metrics.collect(),
                "provider": provider,
                "aggregates": aggregates,
                "workers": aggregates["workers"],
                "owner_configuration": owner_public,
            }
        )

    @app.post("/settings/service")
    async def save_service_settings(request: Request) -> Response:
        session, _ = await session_for(request, False)
        if not session or session.must_change_password:
            return redirect_login(True)
        if owner_settings is None:
            return HTMLResponse("Захищене сховище налаштувань недоступне.", 503)
        data = _form(await request.body())
        if not compare_digest(session.csrf_token, data.get("csrf", "")):
            return Response(status_code=403)
        authenticated, remaining = await owner_reauthenticated(
            request, session, data.get("current_password", "")
        )
        if not authenticated:
            if remaining:
                return HTMLResponse(f"Забагато спроб. Повторіть через {_duration(remaining)}", 429)
            return HTMLResponse(GENERIC_LOGIN_ERROR, 401)

        async def validation_response(message: str) -> HTMLResponse:
            try:
                owner_view = await owner_settings.view()
            except OwnerSettingsError:
                return HTMLResponse("Захищене сховище налаштувань недоступне.", 503)
            poll = await repository.get_setting("poll", settings.alert_poll_interval_seconds)
            stale = await repository.get_setting("stale", settings.alert_stale_after_seconds)
            return HTMLResponse(
                _dashboard_html(
                    session.csrf_token,
                    settings.environment.value,
                    int(poll),
                    int(stale),
                    message,
                    owner_view,
                ),
                422,
            )

        try:
            allowlist_raw = data.get("staging_allowlist", "").strip()
            try:
                allowlist = (
                    tuple(int(item.strip()) for item in allowlist_raw.split(",") if item.strip())
                    if allowlist_raw
                    else None
                )
            except ValueError as exc:
                raise OwnerSettingsValidationError(
                    "STAGING allowlist має містити лише числові Telegram chat ID, розділені комами."
                ) from exc
            command = OwnerConfigurationUpdate(
                expected_revision=int(data.get("expected_revision", "0")),
                environment=data.get("environment", ""),
                alert_provider=data.get("alert_provider", ""),
                telegram_transport=data.get("telegram_transport", ""),
                poll_interval_seconds=int(data.get("poll_interval_seconds", "0")),
                stale_after_seconds=int(data.get("stale_after_seconds", "0")),
                staging_allowlist=allowlist,
                telegram_token=data.get("telegram_token") or None,
                alerts_in_ua_token=data.get("alerts_in_ua_token") or None,
                clear_telegram_token=data.get("clear_telegram_token") == "yes",
                clear_alerts_in_ua_token=data.get("clear_alerts_in_ua_token") == "yes",
                clear_staging_allowlist=data.get("clear_staging_allowlist") == "yes",
                onboarding_text=data.get("onboarding_text"),
            )
            await owner_settings.save(command, session.username)
        except (KeyError, ValueError):
            return await validation_response(
                "Перевірте числові поля: версію, інтервал опитування та поріг застарівання."
            )
        except OwnerSettingsValidationError as exc:
            return await validation_response(str(exc))
        except OwnerSettingsConflictError:
            return HTMLResponse("Конфігурацію змінено. Оновіть сторінку.", 409)
        except OwnerSettingsError:
            return HTMLResponse("Захищене сховище налаштувань недоступне.", 503)
        return RedirectResponse("/?setup=saved#service-setup", 303)

    @app.post("/settings/service/request-activation")
    async def legacy_request_service_activation(request: Request) -> Response:
        del request
        return HTMLResponse(
            "Окремий операторський handoff більше не використовується. "
            "Оновіть сторінку та скористайтеся перевіркою й активацією STAGING.",
            410,
        )

    @app.post("/settings/service/activate")
    async def activate_service_settings(request: Request) -> Response:
        session, _ = await session_for(request, False)
        if not session or session.must_change_password:
            return redirect_login(True)
        if owner_settings is None:
            return HTMLResponse("Захищене сховище налаштувань недоступне.", 503)
        data = _form(await request.body())
        if not compare_digest(session.csrf_token, data.get("csrf", "")):
            return Response(status_code=403)
        if data.get("activation_ack", "").strip() != "АКТИВУВАТИ STAGING":
            return HTMLResponse("Введіть точне підтвердження активації STAGING.", 422)
        authenticated, remaining = await owner_reauthenticated(
            request, session, data.get("current_password", "")
        )
        if not authenticated:
            if remaining:
                return HTMLResponse(f"Забагато спроб. Повторіть через {_duration(remaining)}", 429)
            return HTMLResponse(GENERIC_LOGIN_ERROR, 401)
        pending_created = False
        revision = 0
        try:
            revision = int(data.get("expected_revision", "0"))
            view = await owner_settings.view()
            if view.environment == "production" or settings.environment.value == "production":
                return HTMLResponse(
                    "PRODUCTION потребує окремого схвалення точної release-версії.", 403
                )
            if view.environment != settings.environment.value or view.environment != "staging":
                return HTMLResponse("Самоактивація дозволена лише для поточного STAGING.", 422)
            await owner_settings.request_activation(revision, session.username)
            pending_created = True
            candidate = await owner_settings.activation_candidate(revision)
            evidence = await run_activation_preflight(candidate)
            evidence_id = f"owner-preflight-v1-r{revision}-{token_urlsafe(18)}"
            await owner_settings.mark_active(
                revision,
                session.username,
                evidence_id,
                {
                    "check_profile": "telegram_get_me+alerts_full_snapshot",
                    "covered_region_count": evidence.covered_region_count,
                    "allowlist_count": evidence.allowlist_count,
                },
            )
        except OperatorPreflightError as exc:
            if pending_created:
                message = str(exc).lower()
                failure_code = (
                    "telegram_get_me_failed"
                    if "telegram" in message
                    else "alerts_full_snapshot_failed"
                    if "alerts" in message
                    else "preflight_failed"
                )
                await owner_settings.record_preflight_failure(
                    revision, session.username, failure_code
                )
            return HTMLResponse("Read-only preflight не пройдено. Активну версію не змінено.", 502)
        except (ValueError, OwnerSettingsValidationError):
            return HTMLResponse("Конфігурація ще не готова до активації.", 422)
        except OwnerSettingsConflictError:
            return HTMLResponse("Конфігурацію змінено. Оновіть сторінку.", 409)
        except OwnerSettingsError:
            return HTMLResponse("Захищене сховище налаштувань недоступне.", 503)
        return RedirectResponse("/?setup=activated#service-setup", 303)

    @app.post("/settings/service/rollback")
    async def rollback_service_settings(request: Request) -> Response:
        session, _ = await session_for(request, False)
        if not session or session.must_change_password:
            return redirect_login(True)
        if owner_settings is None:
            return HTMLResponse("Захищене сховище налаштувань недоступне.", 503)
        data = _form(await request.body())
        if not compare_digest(session.csrf_token, data.get("csrf", "")):
            return Response(status_code=403)
        authenticated, remaining = await owner_reauthenticated(
            request, session, data.get("current_password", "")
        )
        if not authenticated:
            if remaining:
                return HTMLResponse(f"Забагато спроб. Повторіть через {_duration(remaining)}", 429)
            return HTMLResponse(GENERIC_LOGIN_ERROR, 401)
        try:
            expected_active = int(data.get("expected_active_revision", "0"))
            view = await owner_settings.view()
            expected_ack = f"ПОВЕРНУТИ {view.rollback_revision}"
            if (
                view.rollback_revision is None
                or data.get("rollback_ack", "").strip() != expected_ack
            ):
                return HTMLResponse("Введіть точне підтвердження rollback.", 422)
            if settings.environment.value == "production":
                return HTMLResponse("Rollback PRODUCTION потребує окремого release-рішення.", 403)
            await owner_settings.rollback(
                expected_active, settings.environment.value, session.username
            )
        except ValueError:
            return HTMLResponse("Невірна активна версія.", 422)
        except OwnerSettingsValidationError as exc:
            return HTMLResponse(str(exc), 422)
        except OwnerSettingsConflictError:
            return HTMLResponse("Активну версію змінено. Оновіть сторінку.", 409)
        except OwnerSettingsError:
            return HTMLResponse("Захищене сховище налаштувань недоступне.", 503)
        return RedirectResponse("/?setup=rolled-back#service-setup", 303)

    @app.post("/settings/provider")
    async def save_provider(request: Request) -> Response:
        session, _ = await session_for(request, False)
        if not session or session.must_change_password:
            return redirect_login(True)
        return HTMLResponse(
            "Цей маршрут вимкнено. Змініть інтервали у майстрі налаштування сервісу; "
            "вони набудуть чинності лише після окремої активації.",
            410,
        )

    @app.get("/help/{document}", response_class=HTMLResponse)
    async def help_document(document: str, request: Request) -> Response:
        session, _ = await session_for(request)
        if not session or session.must_change_password:
            return redirect_login(True)
        allowed = {"setup": "SETUP_BOT.md", "admin": "ADMIN_PANEL.md"}
        filename = allowed.get(document)
        if filename is None:
            return HTMLResponse("Інструкція не знайдена.", 404)
        path = help_root / filename
        if not path.is_file():
            return HTMLResponse("Інструкція ще не створена.", 404)
        content = await asyncio.to_thread(path.read_text, encoding="utf-8")
        return HTMLResponse(
            "<html><body><main><h1>Довідка</h1><pre>"
            + html.escape(content)
            + "</pre></main></body></html>"
        )

    @app.get("/static/admin.css")
    async def css() -> Response:
        return Response(_CSS, media_type="text/css")

    @app.get("/static/admin.js")
    async def js() -> Response:
        return Response(_JS, media_type="application/javascript")

    return app
