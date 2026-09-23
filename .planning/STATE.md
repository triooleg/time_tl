# Project state

Updated: 2026-09-23. This document separates verified local and server simulation
evidence from Telegram staging and production evidence.

## Implemented

- Flexible weekly schedules: odd/even dates and weeks, weekdays including working
  Saturdays, date overrides, gaps, disabled lessons and variable breaks.
- Up to three saved profiles per student with exactly one active profile, portable
  export/import and explicit confirmation of a shared configuration.
- Telegram onboarding and menus backed by PostgreSQL, with three modes: lessons,
  lessons plus alerts, and alerts only.
- PostgreSQL schedule, alert, worker heartbeat and transactional outbox adapters;
  real schedule and sender worker loops; final delivery authorization recheck.
- One global alerts.in.ua poller using the official 27-oblast IoT snapshot. `A` and
  `P` are ACTIVE; `N` is only accepted from a complete validated response.
- Fail-closed freshness, restart, hierarchy, deduplication and two-observation
  all-clear logic. Snapshot/outbox and poll cursor/`Last-Modified` commit atomically.
- HTTP redirects and environment proxies are disabled for the provider transport;
  responses are streamed with a 4096-byte maximum.
- PostgreSQL admin backend with aggregate statistics, provider status, worker
  heartbeats and host metrics obtained from the read-only `node-exporter` service.
- Dockerfile includes migrations, data and operator documentation. Compose defines
  eight services: bot, alert-worker, schedule-worker, sender-worker, admin-web,
  migrate, node-exporter and PostgreSQL.

## Verified evidence

The following LOCAL evidence was recorded on 2026-09-23 for exact commit
`4ea3b1bedfc31142ae2c53d32e550e4e8a20dcff`:

- `pytest`: 242 passed, one opt-in PostgreSQL E2E skipped;
- `unittest discover`: 233 run, one opt-in PostgreSQL E2E skipped;
- Ruff: passed;
- strict mypy: 66 source files passed;
- `compileall`: passed;
- Alembic offline upgrade SQL through head: passed;
- Compose YAML: eight services parsed;
- admin JavaScript syntax check: passed.

The exact source was then built as immutable image
`sha256:08eaa3d06e7ca5d97f11d760c44e67bc085ac395cba196f04a05839d2658da21`
and installed as a permanent **SIMULATION** stack at `/home/trio/time-tl-bot`.
On the mini-server:

- the real PostgreSQL 16 E2E fault/restart harness passed: `1 passed`;
- Alembic reached revision `0003`;
- `admin-web`, `bot`, `alert-worker`, `schedule-worker`, `sender-worker` and
  `postgres` were healthy; `node-exporter` was running;
- PostgreSQL backup into an isolated target and restore verification passed;
- restart persistence verification passed;
- the admin endpoint was bound only to `127.0.0.1:8081`.

The deployed environment deliberately uses the fake provider and disabled Telegram
transport. It has no real provider/bot tokens or users. Schedule and sender remain
inert under the closed simulation capability gates. This is not Telegram staging or
production evidence.

## Gate status

| Gate | Verdict | Evidence or blocker |
|---|---|---|
| G0 SOURCE | REVIEW | Official 27-oblast endpoint/schema and limits reviewed; a project token and pre-staging recheck are still required. |
| G1 SPEC | PASS | Product modes, schedule semantics, privacy and release boundaries are documented. |
| G2 SCHEDULE | PASS | Local decision-table and integration tests cover parity, weekends, overrides, gaps and disabled lessons. |
| G3 ALERT SAFETY | PASS (SIMULATION) | Local fail-closed tests and the real PostgreSQL 16 fault/restart harness pass. No official-provider or production claim is implied. |
| G4 LOCAL QUALITY | PASS | Evidence list above. |
| G5 SIMULATION E2E | PASS | Exact-image server simulation, PostgreSQL fault/restart E2E, migration, backup/restore and persistence checks passed with fake provider and disabled Telegram. |
| G6 TELEGRAM STAGING | BLOCKED | Requires a separate bot token, alerts.in.ua token, approved test chat allowlist and live-bot capability review. |
| G7 SERVER READINESS | BLOCKED | The simulation stack, storage, restart and backup/restore are verified, but staging readiness still requires secrets handling, rollback evidence and the approved access/TLS boundary. |
| G8 DEPLOY APPROVAL | BLOCKED | Approval covered the exact simulation deployment only; no staging or production release has been approved. |
| G9 CANARY | BLOCKED | Requires G6-G8 and approved canary accounts. |

## Deliberately closed capabilities

Production remains fail closed. `production_oblast_scope_release`,
`reviewed_region_resolver`, `schedule_worker` and `sender_worker` capability flags
are still false. The worker implementations exist, but the flags must not be enabled
until their corresponding staging/release evidence is independently approved.

Coordinate-to-region resolution is not released. A user may select and confirm one
of the reviewed oblasts manually; raw Telegram coordinates are rejected and are not
persisted. This does not yet satisfy the requested geolocation experience.

## Required next evidence

1. Obtain dedicated Telegram and alerts.in.ua tokens without reusing unrelated
   project secrets; approve staging chat IDs.
2. Review and approve a privacy-safe coordinate-to-oblast resolver, or explicitly
   approve manual oblast selection as the release scope.
3. Verify staging secrets injection, access/TLS boundary and rollback without
   exposing credentials, then request an exact staging approval.
4. Run allowlisted Telegram staging, then a separately approved canary.

An isolated permanent SIMULATION deployment has been performed. No Telegram staging,
production deployment, official-provider polling or real-user message delivery has
been performed.
