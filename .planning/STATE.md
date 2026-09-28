# Project state

Baseline gate/state snapshot: 2026-09-23. Session continuity updated: 2026-09-28.
This document separates verified local and server simulation evidence from
Telegram staging and production evidence.

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

## Session Continuity

Last session: 2026-09-28
Stopped at: `EXTERNALLY BLOCKED` after the exact RC9 diagnostic deployment.
The earlier exact RC8 packet
`time-tl-staging-20260928-rc8-bounded-meta`, commit
`eeca2f8fb80a2d6d8890dbd268799e470f460c82`, archive SHA-256
`bb44f8aa79b8d52518f3e369367d8adc6a9667d95e66d8558ad792d918bf03fe`,
was authorized for a bot/admin-only install plus one safe preflight, with workers
and Telegram required to remain disabled. Server archive identity matched. The
actual pre-change baseline was RC7 (`sha256:ccf9eb` prefix), not the previously
documented RC6. Fresh backup and rollback were verified, and the unchanged
Compose hash has prefix `b00192a`.

RC8 image prefix `sha256:b31fc47c` is installed. `admin-web` is healthy; `bot` is
stopped/exited; `alert-worker`, `schedule-worker` and `sender-worker` are
Created/stopped. The database remains at `0006_outbox_terminal_retention`; no
migration ran.

The executed deployment verdict is `REJECTED`. ACTIVE database owner revision 6
overrode environment settings and selected live Telegram transport. The bot
polled Telegram from `08:27:11Z` to `08:31:24Z` (approximately 4 minutes 13
seconds) before containment. Handled updates, Telegram send markers and delivered
outbox messages were all zero. There is no evidence of messages, but continuous
disablement cannot be claimed. No provider call or preflight occurred. Pending
revision 9 is production-scoped and is blocked by the STAGING-only preflight;
owner audit entries from the operation are zero.

Current server status remains `HOLD` only while the bot and all three workers
remain stopped. G6-G9 were not advanced. Pending owner revision 10 exists while
ACTIVE revision remains 6.

A separate diagnostic-only RC9 release was built from tag
`time-tl-staging-20260928-rc9-alert-field-diag`, commit
`fb4c64d96e740ddd811b88f35b36a1c3187c8312`, archive SHA-256
`1e271f50c8fd20facfa8037252fd094ecf59e0688c546c1e43a104ea17519760`.
The official API schema omits
`calculated`, while the official Python SDK reads it without documenting its
wire type or safety semantics. RC9 does not accept this field: a containing
response remains `INVALID_RESPONSE` and fails closed. It adds only fixed opaque
`null`/`boolean`/`other` diagnostic categories and does not expose or use the
field value for alert state.

RC9 local evidence: 57 focused tests passed; the full suite passed 513 tests with
five opt-in destructive PostgreSQL E2E skips and one Starlette warning; Ruff,
strict mypy for 72 source files, compileall and diff checks passed. Independent
safety review passed for the diagnostic-only scope. At completion of local
verification, deployment remained separately gated; local evidence did not
itself authorize package transfer, activation, canary, bot or worker startup, or
messages.

The exact RC9 archive was subsequently authorized, transferred and verified on
the server with the same SHA-256. Immutable image prefix
`sha256:fea4ff501b87` was built. `admin-web` now runs that image, is healthy and
returned HTTP 200. RC9 `bot` and all three workers are `Created` with zero
`StartedAt` values; they were never started. Database revision remains
`0006_outbox_terminal_retention`; no migration ran. Owner state remains
latest/pending revision 10 and ACTIVE revision 6.

Exactly one RC9 diagnostic read-only preflight ran at `09:22:57Z` for pending
revision 10. Telegram `getMe` passed; the provider made one
`active.json` request and received HTTP 200, then failed closed as
`INVALID_RESPONSE / alert_fields_unknown / count=1`. There was no retry,
activation, message, or delivered outbox item. No raw provider response, unknown
field name or value was retained in release evidence.

Fresh rollback backup
`/home/trio/time-tl-bot/backups/pre-rc9-alert-field-diag-20260928T092016Z`
and the RC8/RC7 images are retained. Independent review rejected raw or hashed
unknown-field diagnostics because they could disclose or make a provider field
re-identifiable. The release is therefore `EXTERNALLY BLOCKED` pending an
authoritative provider schema update or a provider-supplied sanitized response
sample. Activation, bot/workers, messages and all-clear processing remain
blocked; G6-G9 are not advanced.
Resume file: none.
