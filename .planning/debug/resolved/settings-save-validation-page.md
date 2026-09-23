---
status: resolved
trigger: "После сохранения настроек открывается отдельная страница с текстом 'Перевірте введені налаштування.'"
created: 2026-09-23
updated: 2026-09-23
resolved: 2026-09-23
---

## Symptoms

- expected_behavior: После сохранения DRAFT мастер остаётся в админ-панели и показывает успех либо конкретные ошибки рядом с полями.
- actual_behavior: Браузер переходит на `/settings/service` и показывает только текст `Перевірте введені налаштування.`.
- error_messages: `Перевірте введені налаштування.`
- timeline: Проявилось при первом сохранении Telegram-настроек после создания бота.
- reproduction: Войти в админ-панель, заполнить мастер настройки Telegram и нажать кнопку сохранения.
- privacy: Значения токенов не записывать в диагностику, тесты или журналы.

## Current Focus

- hypothesis: confirmed
- test: POST с фиктивным token и нечисловым allowlist возвращает полный dashboard 422, безопасное сообщение и не отображает token.
- expecting: confirmed
- next_action: complete full verification and prepare exact release
- reasoning_checkpoint:
- tdd_checkpoint:

## Evidence

- timestamp: 2026-09-23; evidence: `src/time_tl/admin/async_app.py` возвращал bare `HTMLResponse` для всех ошибок разбора/валидации.
- timestamp: 2026-09-23; evidence: регрессионный тест маршрута прошёл; 9 focused tests passed.
- timestamp: 2026-09-23; evidence: Ruff passed and `git diff --check` found no errors.

## Eliminated

## Resolution

- root_cause: POST `/settings/service` сворачивал ошибки разбора и валидации в отдельный bare 422 response, полностью заменяя dashboard.
- fix: Повторно отображать полный dashboard с безопасным сообщением; для allowlist показывать требование числовых Telegram chat ID; не повторять введённые tokens.
- verification: 9 focused pytest tests passed; Ruff passed; regression confirms no token echo and unchanged revision.
- files_changed: `src/time_tl/admin/async_app.py`, `tests/admin/test_async_app.py`
