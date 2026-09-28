---
phase: 11-homework-foundation
status: local_pass_production_hold
scope: local_only
---

# Phase 11: Manual personal homework

## Goal

Add a disabled-by-default, additive homework vertical slice without changing the alert state
machine, alert provider, alert worker, sender worker, public runtime state, or production stack.

OCR/image digitization and the Moiashkola integration are explicitly out of scope for this
phase. Shared schedules and homework notifications are also deferred.

## Wave 1

1. Backend persistence
   - Add subject, personal homework, per-user completion-state, and durable draft tables.
   - Add migration `0008` that is safe both after `0007` and on a fresh database affected by
     the historical `Base.metadata.create_all()` bootstrap.
   - Add an async repository with strict ownership checks, bounded text, optimistic versioning,
     durable preview/confirmation, and idempotent completion changes.
2. Telegram UX
   - Add Ukrainian homework menu and manual create/list/view/edit/delete/complete flows.
   - Subjects come only from enabled lessons in the active personal schedule.
   - Suggest the next matching lesson date using the existing schedule resolver; require an
     explicit preview confirmation before persistence.
   - Hide and reject the entire feature when `HOMEWORK_ENABLED=false`.
   - Keep the main menu compact: schedule, conditional homework, notifications, territory,
     settings, and help. Move deletion under settings/privacy and reuse the existing double
     confirmation.
   - Do not expose photo, import, shared-schedule, quiet-hours, language, or other controls
     until their backend capability exists.

## Integration

- Root owns configuration, application wiring, cross-module reconciliation, and full checks.
- No notification event or outbox type is added in this phase.
- Account deletion must cascade through all new personal data.
- No new external dependency or outbound integration is allowed.

## Acceptance

- `HOMEWORK_ENABLED` defaults to false and the existing menu is byte-for-byte equivalent when
  disabled except where tests intentionally instantiate the enabled mode.
- A user cannot read or mutate another user's homework or draft.
- Manual draft survives application restart, expires safely, and saves exactly once.
- CRUD and complete/uncomplete work for the owner.
- Invalid/empty/oversized text and stale versions fail without mutation.
- Migration upgrade/downgrade checks, focused tests, Ruff, strict mypy, and full tests pass.
- Independent safety review confirms no alert-path regression and no production action.

## Result

- LOCAL code verdict: PASS after independent safety review.
- Final local checks: 679 passed, 6 PostgreSQL E2E skipped; Ruff, strict mypy for 76
  source files, compileall, Alembic head, and diff-check passed.
- Production/public verdict: HOLD. The updated destructive PostgreSQL migration harness must
  run against an isolated database before an exact-version release can be prepared.
- No server, Compose, production database, worker, alert state, outbox, or Telegram delivery
  was changed during this phase.
