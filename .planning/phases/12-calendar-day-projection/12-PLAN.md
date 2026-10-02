# Phase 12 implementation plan

Status: PASS for the bounded LOCAL slice. No Git commit/push or deployment performed.
Evidence: 12-SUMMARY.md and 12-VERIFICATION.md; persistent memberships remain pending.

## Objective

Add a backward-compatible, default-off My Day view and pure calendar aggregation.
Existing active profile is a read bridge; persistent memberships are not implemented
or enabled in this slice. See ADR 0012 for scope and safety boundaries.

## Owned tasks

1. C12-01 schedule_engineer: domain/calendars.py and domain decision-table tests.
   Reuse schedule resolution, isolate memberships, retain all overlaps/current events,
   validate bounds and timezone edge cases; never include join URLs.
2. C12-02 telegram_ux_builder: bot/my_day.py and renderer tests. Ukrainian paginated
   day/current/next output, explicit optional-data failure, bounded messages/callbacks.
3. C12-03 root integrator: config, async facade/menu/router and runtime wiring plus
   integration tests. Flag off unchanged; enabled reads existing owner data only.
4. C12-04 independent safety_reviewer: read actual diff/tests, verify safety worker
   isolation, ownership, no false clear/empty state, privacy and evidence claims.

All workers have non-overlapping allowed paths; no children/server/Git/secret work.
Integrate only after actual diff inspection and tests. Root preserves RC25 dirty
source changes and other agent configuration; no reverting or staging unrelated work.

## Required checks

Targeted domain/renderer/facade/router/config tests; legacy resolver/profile/worker,
homework, alert and simulation regressions; full pytest; Ruff src/tests/scripts;
mypy src/time_tl. No integration test uses a production database or real Telegram.
Independent review PASS is required for the local safety claim. Staging/deploy require
a separately pinned release and explicit scope approval; no current activation.

## Handoff

Document exact tests and skips, acceptance scope, remaining persistent calendars work
and any blocking findings in 12-SUMMARY.md / 12-VERIFICATION.md. Do not call the full
new concept or persistent subscriptions implemented based on pure fixture tests.
