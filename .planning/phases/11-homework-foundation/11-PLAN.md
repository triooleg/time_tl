---
phase: 11-homework-foundation
status: production_candidate_contract_in_progress
scope: local_and_release_preparation
---

# Phase 11: Manual personal homework

## Goal

Add a disabled-by-default, additive homework vertical slice without changing the alert state
machine, alert provider, alert worker, sender worker or existing public runtime state. The
implementation phase keeps production closed; a later exact RC23 promotion updates the signed
production contract only after the local and STAGING gates pass.

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
- Isolated PostgreSQL 17.11 verdict: PASS. All 6 protected destructive restart/fault tests ran
  against the disposable `time_tl_e2e` database, including
  `0007 -> 0008 -> 0007 -> 0008`; the temporary server was then stopped and removed.
- The real PostgreSQL run found and closed a 63-byte identifier-limit defect in the draft
  confirmation foreign key. A schema regression test now compiles the table with the
  PostgreSQL dialect and enforces the identifier limit.
- Final local checks after the fix: 679 passed, 6 environment-gated PostgreSQL E2E skipped in
  the ordinary suite; Ruff, strict mypy for 76 source files, compileall, and diff-check passed.
- Independent safety re-review: PASS. The alert and outbox paths were unchanged.
- Production/public verdict: HOLD until a new exact artifact/commit/tag/hash and attestation
  with `migration_head=0008` are prepared and the approved migration, preflight, and canary
  gates pass. Any persistent database already stamped `0008` must be inspected before reuse
  because Alembic would not rerun an edited historical revision.
- No server, Compose, production database, worker, alert state, outbox, or Telegram delivery
  was changed during this phase.
