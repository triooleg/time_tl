# Project state

Updated: 2026-09-23. This document separates verified local evidence from staging,
server and production evidence.

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

## Verified evidence (LOCAL only)

The following evidence was recorded on 2026-09-23:

- `pytest`: 241 passed, one opt-in PostgreSQL E2E skipped;
- `unittest discover`: 233 run, one opt-in PostgreSQL E2E skipped;
- Ruff: passed;
- strict mypy: 66 source files passed;
- `compileall`: passed;
- Alembic offline upgrade SQL through head: passed;
- Compose YAML: eight services parsed;
- admin JavaScript syntax check: passed.

These checks do not prove a container build, live PostgreSQL restart/fault recovery,
Telegram delivery, server readiness, Portainer deployment or public safety.

## Gate status

| Gate | Verdict | Evidence or blocker |
|---|---|---|
| G0 SOURCE | REVIEW | Official 27-oblast endpoint/schema and limits reviewed; a project token and pre-staging recheck are still required. |
| G1 SPEC | PASS | Product modes, schedule semantics, privacy and release boundaries are documented. |
| G2 SCHEDULE | PASS | Local decision-table and integration tests cover parity, weekends, overrides, gaps and disabled lessons. |
| G3 ALERT SAFETY | PASS (LOCAL) | Local fail-closed, atomic cursor, stale, replay, ordering and sender recheck tests pass. This is not production evidence. |
| G4 LOCAL QUALITY | PASS | Evidence list above. |
| G5 SIMULATION E2E | BLOCKED | Deterministic simulation exists, but the gate still requires real PostgreSQL transaction fault injection and process-restart recovery evidence. |
| G6 TELEGRAM STAGING | BLOCKED | Requires a separate bot token, alerts.in.ua token, approved test chat allowlist and live-bot capability review. |
| G7 SERVER READINESS | BLOCKED | No exact stack has been built or run on the mini-server; backup/restore, volumes, resources and rollback remain unverified. |
| G8 DEPLOY APPROVAL | BLOCKED | No approval has been recorded for an exact commit/image digest/Compose revision. |
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
2. Run real PostgreSQL fault injection and restart tests for alert cursor/snapshot,
   outbox, worker lease and sender authorization.
3. Review and approve a privacy-safe coordinate-to-oblast resolver, or explicitly
   approve manual oblast selection as the release scope.
4. Produce an immutable image and record commit, digest, migration revision and
   exact Compose file.
5. Verify server backup/restore and rollback before requesting G8 approval.
6. Run allowlisted Telegram staging, then a separately approved canary.

No deployment, Portainer mutation, production password generation or real-user
message delivery has been performed.
