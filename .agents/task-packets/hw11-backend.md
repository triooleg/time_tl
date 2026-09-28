# Agent task packet

- **Task ID:** HW11-BACKEND
- **Goal:** Implement additive PostgreSQL persistence and domain contracts for manual personal homework.
- **Owner:** `backend_data_engineer`
- **Allowed paths:** `src/time_tl/domain/homework.py`, `src/time_tl/infrastructure/db/models.py`, `src/time_tl/infrastructure/db/homework.py`, `src/time_tl/infrastructure/db/__init__.py`, `migrations/versions/0008_homework_foundation.py`, `tests/homework/**`, migration-focused tests directly required for 0008.
- **Read-only dependencies:** schedule domain/resolver, account repository, migrations 0001-0007, bot protocols.
- **Dependencies:** Existing schema at migration 0007.
- **Acceptance criteria:** Personal-only ownership; subjects derived/stored per profile; bounded nonempty text; due date; version checks; durable expiring single-use draft; idempotent completion state; cascade deletion; fresh/upgrade-safe migration.
- **Required commands/tests:** focused homework tests; migration tests if available; Ruff and mypy for owned files.
- **Forbidden actions:** Do not edit bot/UI, config, Compose, workers, outbox, alerts, production files, secrets, or deploy. Do not commit. Do not revert or overwrite other agents' or user's work.
- **Expected evidence:** Changed-file list, exact test commands/results, schema invariants and remaining risks.
- **Handoff target:** `/root` integrator.

## Completion report

- Files changed:
- Tests and exact results:
- Assumptions:
- Unverified items:
- Risks or follow-up:
- Recommended verdict: `REVIEW` / `BLOCKED`

