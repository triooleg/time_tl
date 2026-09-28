# Agent task packet

- **Task ID:** HW11-TELEGRAM
- **Goal:** Implement Ukrainian manual-personal-homework Telegram UX behind an injected disabled-by-default capability.
- **Owner:** `telegram_ux_builder`
- **Allowed paths:** `src/time_tl/bot/homework_router.py`, `src/time_tl/bot/async_service.py`, `src/time_tl/bot/service.py`, `tests/bot/test_homework_router.py`, narrowly necessary updates to `tests/bot/test_async_service.py` and `tests/bot/test_telegram_application.py`.
- **Read-only dependencies:** existing schedule editor/router, schedule domain/resolver, `src/time_tl/domain/homework.py` contract when available, aiogram wiring.
- **Dependencies:** Define UX against a small async homework port/protocol; root will reconcile backend wiring.
- **Acceptance criteria:** Hidden when disabled; explicit Ukrainian menu; active schedule required; subjects only from enabled lessons; manual text; next matching lesson suggestion; preview then confirm; list/detail/edit/delete/complete/uncomplete; ownership errors fail closed; callback data <=64 bytes.
- **Required commands/tests:** focused bot tests; Ruff and mypy for owned files.
- **Forbidden actions:** Do not edit DB models/migrations/backend repository/config/Compose/workers/outbox/alerts/secrets or deploy. Do not commit. Do not revert or overwrite other agents' or user's work. Do not add OCR, image upload, Moiashkola, shared schedules, imports, or notifications.
- **Expected evidence:** Changed-file list, exact tests/results, UX state map, assumptions and remaining risks.
- **Handoff target:** `/root` integrator.

## Completion report

- Files changed:
- Tests and exact results:
- Assumptions:
- Unverified items:
- Risks or follow-up:
- Recommended verdict: `REVIEW` / `BLOCKED`
