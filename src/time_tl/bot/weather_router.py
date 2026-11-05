"""Private, dependency-injected cache-only weather views and explicit consent."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import Any, Protocol

from time_tl.domain.weather import ForecastSnapshot

from .models import Button, View
from .weather_presentation import render_forecast, short_weather_locality

BACK = ((Button("⬅️ Погода", "menu:weather"),),)
SOURCES = (Button("ℹ️ Джерела та ліцензії", "wt:sources"),)
SOURCES_VIEW = View(
    "ℹ️ Джерела та ліцензії\n\n"
    "Прогноз: MET Norway. Time TL групує й оформлює прогнозні дані.\n"
    "https://api.met.no/\n"
    "Ліцензія CC BY 4.0: https://creativecommons.org/licenses/by/4.0/\n\n"
    "Координати громад: © OpenStreetMap contributors (ODbL).\n"
    "https://www.openstreetmap.org/copyright",
    BACK,
)
SETTING_FLAGS = (
    ("daily", "daily_forecast_enabled", "🌅 Щоденний прогноз"),
    ("change", "weather_changes_enabled", "🔄 Зміни прогнозу"),
    ("rain", "rain_alert_enabled", "🌧 Дощ"),
    ("snow", "snow_alert_enabled", "❄️ Сніг"),
    ("wind", "wind_alert_enabled", "💨 Вітер"),
    ("temp", "temperature_alert_enabled", "🌡 Зміни температури"),
    ("frost", "frost_alert_enabled", "🥶 Заморозки"),
)


@dataclass(frozen=True)
class WeatherUiPreferences:
    forecast_requested: bool = False
    weather_enabled: bool = False
    revision: int = 0
    daily_forecast_enabled: bool = False
    daily_forecast_time: time = time(7)
    weather_changes_enabled: bool = True
    rain_alert_enabled: bool = True
    snow_alert_enabled: bool = True
    wind_alert_enabled: bool = True
    temperature_alert_enabled: bool = True
    frost_alert_enabled: bool = True
    notification_lead_minutes: int = 120


@dataclass(frozen=True)
class WeatherContext:
    locality: str
    has_precise_point: bool
    point_id: str | None = None
    collection_enabled: bool = True


class WeatherBotPort(Protocol):
    async def context(self, actor: int) -> WeatherContext: ...
    async def preferences(self, actor: int) -> WeatherUiPreferences: ...
    async def snapshot(self, actor: int) -> ForecastSnapshot | None: ...
    async def request_forecast(self, actor: int) -> WeatherUiPreferences: ...
    async def save_notifications(
        self, actor: int, enabled: bool, expected_revision: int
    ) -> WeatherUiPreferences: ...
    async def disable_weather(self, actor: int, expected_revision: int) -> WeatherUiPreferences: ...
    async def save_settings(
        self, actor: int, expected_revision: int, **changes: object
    ) -> WeatherUiPreferences: ...


class WeatherUx:
    def __init__(
        self,
        port: WeatherBotPort,
        *,
        notifications_available: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.port, self.notifications_available, self.clock = port, notifications_available, clock

    async def home(self, actor: int) -> View:
        context = await self.port.context(actor)
        if not context.has_precise_point:
            return View(
                "🌤 Погода\nОберіть точнішу територію. Підтвердженої погодної точки ще немає; "
                "прогноз обласного центру не замінює прогноз вашої громади.",
                (
                    (Button("📍 Моя територія", "menu:region"),),
                    (Button("🏠 Головне меню", "menu:home"),),
                ),
            )
        prefs = await self.port.preferences(actor)
        locality = short_weather_locality(context.locality)
        if not prefs.forecast_requested:
            return View(
                f"🌤 Погода\n📍 {locality}\n\n"
                "Отримувати прогноз для вашої громади? "
                "Сповіщення можна ввімкнути окремо."
                + (
                    ""
                    if context.collection_enabled
                    else "\nНовий прогноз поки недоступний. Запит можна зберегти."
                ),
                (
                    (Button("✅ Отримувати прогноз", "wt:request"),),
                    SOURCES,
                    (Button("🏠 Головне меню", "menu:home"),),
                ),
            )
        return View(
            f"🌤 Погода\n📍 {locality}",
            (
                (Button("🌡 Прогноз на зараз", "wt:now"),),
                (Button("🌤 Прогноз на завтра", "wt:tomorrow"),),
                (Button("📅 Прогноз на 3 дні", "wt:three"),),
                (Button("⚠️ Погодні попередження", "wt:warnings"),),
                (Button("🔔 Налаштування", "wt:settings"),),
                SOURCES,
                (Button("🏠 Головне меню", "menu:home"),),
            ),
        )

    async def forecast(self, actor: int, mode: str) -> View:
        if mode not in {"now", "tomorrow", "three"}:
            raise ValueError("unknown_weather_view")
        context = await self.port.context(actor)
        prefs = await self.port.preferences(actor)
        if not context.has_precise_point or not prefs.forecast_requested:
            return await self.home(actor)
        snapshot = await self.port.snapshot(actor)
        now = self.clock()
        if snapshot is None:
            if not context.collection_enabled:
                return View("🌤 Прогноз поки недоступний.", BACK)
            return View("🌤 Прогноз для вашої території готується. Спробуйте трохи пізніше.", BACK)
        if context.point_id is None or snapshot.point_id != context.point_id:
            return View("Територію змінено. Відкрийте прогноз знову.", BACK)
        if not snapshot.usable(now):
            return View(
                "⚠️ Прогноз тимчасово недоступний або застарів. "
                "Це не означає відсутності погодних явищ.",
                BACK,
            )
        return View(
            render_forecast(snapshot, context.locality, now, mode),
            (SOURCES,) + BACK,
            parse_mode="HTML",
        )

    async def settings(self, actor: int) -> View:
        context = await self.port.context(actor)
        prefs = await self.port.preferences(actor)
        rows: list[tuple[Button, ...]] = []
        selected_types = []
        lines = ["🔔 Погодні повідомлення"]
        if context.has_precise_point:
            lines.append(f"📍 {short_weather_locality(context.locality)}")
        else:
            lines.append("📍 Оберіть територію для прогнозу.")
        status = "увімкнено" if prefs.weather_enabled else "вимкнено"
        lines.append(f"Загальний перемикач: {status}.")
        if self.notifications_available:
            rows.append(
                (
                    Button(
                        "🔕 Вимкнути"
                        if prefs.weather_enabled
                        else "🔔 Увімкнути погодні повідомлення",
                        f"wt:notify:{int(not prefs.weather_enabled)}:{prefs.revision}",
                    ),
                )
            )
        for short, field, label in SETTING_FLAGS:
            enabled = getattr(prefs, field)
            mark = "✅" if enabled else "❌"
            selected_types.append(f"{label} — {'увімкнено' if enabled else 'вимкнено'}")
            rows.append((Button(f"{label} — {mark}", f"wt:flag:{short}:{prefs.revision}"),))
        if not self.notifications_available:
            lines.append("Доставка погодних повідомлень ще не ввімкнена.")
        elif not prefs.weather_enabled:
            lines.append("Погодні повідомлення призупинено. Вибір типів збережено.")
        elif not any(getattr(prefs, field) for _, field, _ in SETTING_FLAGS):
            lines.append("Усі типи вимкнено. Погодні повідомлення не надходитимуть.")
        elif not prefs.forecast_requested:
            lines.append("Для отримання повідомлень спочатку увімкніть отримання прогнозу.")
        time_label = " (місцевий час)" if context.has_precise_point else ""
        lines.extend((
            "",
            *selected_types,
            "",
            f"Час щоденного прогнозу: {prefs.daily_forecast_time:%H:%M}{time_label}",
            f"Попередження за {prefs.notification_lead_minutes} хв",
        ))
        rows.append(
            tuple(
                Button(f"{lead} хв", f"wt:lead:{lead}:{prefs.revision}")
                for lead in (30, 60, 120, 180)
            )
        )
        rows.append(
            tuple(
                Button(f"{hour:02d}:00", f"wt:time:{hour:02d}00:{prefs.revision}")
                for hour in (7, 8, 18)
            )
        )
        rows.append(
            (Button("🛑 Вимкнути погоду та збір для мене", f"wt:disable:{prefs.revision}"),)
        )
        return View("\n".join(lines), tuple(rows) + BACK)


def build_weather_router(ux: WeatherUx, *, router: Any) -> None:
    from aiogram import F, Router
    from aiogram.fsm.context import FSMContext
    from aiogram.types import CallbackQuery

    from .aiogram_adapter import _callback_user_id, _edit

    selected: Router = router

    @selected.callback_query(F.data == "menu:weather")
    @selected.callback_query(F.data.startswith("wt:"))
    async def weather_action(callback: CallbackQuery, state: FSMContext) -> None:
        actor = _callback_user_id(callback)
        try:
            if (
                callback.message is None
                or callback.message.chat.type != "private"
                or callback.message.chat.id != actor
            ):
                raise ValueError("weather_private_chat_required")
            if await state.get_state() is not None:
                await _edit(callback, View("Спочатку завершіть поточну дію.", BACK))
                return
            raw = callback.data or ""
            if len(raw.encode()) > 64:
                raise ValueError("invalid_weather_callback")
            async with asyncio.timeout(5):
                if raw == "menu:weather":
                    view = await ux.home(actor)
                elif raw == "wt:request":
                    context = await ux.port.context(actor)
                    if not context.has_precise_point:
                        raise ValueError("weather_point_missing")
                    await ux.port.request_forecast(actor)
                    view = await ux.home(actor)
                elif raw in {"wt:now", "wt:tomorrow", "wt:three"}:
                    view = await ux.forecast(actor, raw.split(":")[1])
                elif raw == "wt:settings":
                    view = await ux.settings(actor)
                elif raw == "wt:sources":
                    view = SOURCES_VIEW
                elif raw == "wt:warnings":
                    view = View(
                        "Офіційний машинозчитуваний канал попереджень ще не "
                        "підтверджено. Прогнозні рекомендації не є офіційними "
                        "попередженнями про небезпеку.",
                        BACK,
                    )
                else:
                    parts = raw.split(":")
                    if (
                        len(parts) == 4
                        and parts[1] == "notify"
                        and parts[2] in {"0", "1"}
                        and ux.notifications_available
                    ):
                        await ux.port.save_notifications(actor, parts[2] == "1", int(parts[3]))
                    elif len(parts) == 4 and parts[1] == "flag":
                        field = next(
                            (field for short, field, _ in SETTING_FLAGS if short == parts[2]), None
                        )
                        if field is None:
                            raise ValueError("invalid_weather_flag")
                        prefs = await ux.port.preferences(actor)
                        await ux.port.save_settings(
                            actor, int(parts[3]), **{field: not getattr(prefs, field)}
                        )
                    elif len(parts) == 4 and parts[1] == "lead":
                        lead = int(parts[2])
                        if lead not in (30, 60, 120, 180):
                            raise ValueError("invalid_weather_lead")
                        await ux.port.save_settings(
                            actor, int(parts[3]), notification_lead_minutes=lead
                        )
                    elif len(parts) == 4 and parts[1] == "time":
                        if parts[2] not in ("0700", "0800", "1800"):
                            raise ValueError("invalid_weather_time")
                        await ux.port.save_settings(
                            actor, int(parts[3]), daily_forecast_time=time(int(parts[2][:2]))
                        )
                    elif len(parts) == 3 and parts[1] == "disable":
                        await ux.port.disable_weather(actor, int(parts[2]))
                    else:
                        raise ValueError("invalid_weather_callback")
                    view = await ux.settings(actor)
        except Exception:
            view = View("⚠️ Дію не виконано. Спробуйте відкрити погоду знову.", BACK)
        await _edit(callback, view)
