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

An isolated permanent SIMULATION deployment was the first server baseline. The
later STAGING-scoped, read-only provider preflights are recorded below; none
produced an accepted snapshot, activated a revision, started delivery-capable
services or sent a message. No production or canary deployment has occurred.

## Session Continuity

Last session: 2026-09-28
Stopped at: RC13 is installed, but its single preflight rejected the provider
response as `location_uid_unknown / count=1`; activation did not occur and the
server remains in `HOLD`. RC14 is a LOCAL safety candidate. It does
not broaden UID compatibility; an upstream API/catalogue inconsistency remains
`BLOCKED` pending authoritative provider evidence.
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
re-identifiable.

The user subsequently directed that an undocumented alert-row field must not be
used. A narrow RC10 local candidate implements that decision without learning,
logging or interpreting the field. It may discard at most one undocumented key
per alert row, requires the same undocumented key wherever an extra key is
present in the snapshot, and applies the existing bounded JSON value limits plus
a 32,768-byte per-row serialized limit. The key and value are excluded from the
canonical digest, alert episodes, state, diagnostics, logs and persistence.
Documented alert fields remain strict. Invalid documented data, more than one
extra key, inconsistent extra keys or an out-of-bounds extra value still reject
the complete snapshot. Existing hierarchy handling and the two-successful-
observation all-clear rule remain in force.

RC10 local evidence: 58 focused tests passed; the full suite passed 514 tests
with five opt-in destructive PostgreSQL E2E skips and one Starlette warning.
Ruff, strict mypy for 72 source files, compileall and diff checks passed. The
checkout source path was explicitly pinned through `PYTHONPATH` because a stale
editable-install `.pth` entry otherwise resolved another checkout. That local
editable entry was then corrected to `D:\Projects\Time_tl_RC8\src`, and an
unpinned focused rerun passed 58 tests from the current checkout. Independent
safety review passed the code-only compatibility scope. This is LOCAL evidence
only: no exact RC10 tag, commit, archive or image has been established, and RC10
has not been transferred, installed or run on the server.

The reported public admin HTTP 403 was transient and could not be reproduced:
the public `/login` request and the backend endpoint both returned HTTP 200 in
the current read-only check. No proxy, access-control or application setting was
changed, so this is current reachability evidence rather than proof of a repaired
root cause.

The exact RC10 release was subsequently approved and installed from tag
`time-tl-staging-20260928-rc10-ignore-undocumented`, commit
`42352cb6c04b00e912b847befb8aa91c67d009c9`, archive SHA-256
`2ed1cf7cfedf7132776487a406b0293a028e8c4817316763210f515679b940e3`.
The server archive matched the approved digest and immutable RC10 image
`sha256:577fa52128fdecfeaa9370a56039f04fdb1da266881f9f62d899130c72fbc16d`
was built. `admin-web` runs RC10, is healthy and returned HTTP 200. RC10 `bot` and
all three workers were created stopped and were never started. Database revision
remains `0006_outbox_terminal_retention`; no migration ran. Owner state remains
ACTIVE revision 6 and pending revision 10.

Exactly one RC10 preflight ran at `10:19:39Z` for pending revision 10. Telegram
`getMe` passed and the official provider made one `active.json` request that
returned HTTP 200. The response then failed closed as
`INVALID_RESPONSE / location_oblast_uid_format_invalid / count=1`. There was no
retry, activation, message or delivered outbox item. Independent safety review
rejected activation and accepted the contained `HOLD` state only while `bot` and
all workers remained stopped. At that point the server truth was RC10 `HOLD`,
not a staging pass; G6, G7 and G9 remain unadvanced.

Fresh verified rollback backup:
`/home/trio/time-tl-bot/backups/pre-rc10-ignore-undocumented-20260928T101742Z`.

RC11 is a narrow LOCAL diagnostic candidate for that rejected optional
`location_oblast_uid` predicate. It preserves rejection and classifies only the
invalid value shape as `boolean`, `integer`, `noncanonical_string` or `other`.
It does not expose the value or the ignored undocumented field, does not advance
the validated provider baseline, and cannot produce a snapshot, alert state,
outbox message or all-clear from the rejected response.

RC11 LOCAL evidence: 66 focused tests passed; the full suite passed 522 tests
with five opt-in destructive PostgreSQL E2E skips and one Starlette warning.
Ruff, strict mypy for 72 source files, compileall and `git diff --check` passed.
Independent safety review passed this code-only diagnostic scope.

The exact RC11 release was subsequently approved and installed from tag
`time-tl-staging-20260928-rc11-oblast-uid-shape-diag`, commit
`2e9a29ea1feaa3730eee7f022ae7316ad1ee28cf`, archive SHA-256
`b8c869990a323be1627c1ac986e1f93841a973361654a2cf928852630aca0550`.
The server archive matched the approved digest and immutable RC11 image
`sha256:10355985d0e6343382e95ef60db57eccc43fad3e665c757f226bcfebe87c2095`
was built. `admin-web` runs RC11, is healthy, and both public and direct checks
returned HTTP 200. RC11 `bot` and all three workers are `Created` with zero
startup evidence and were never started. Database revision remains
`0006_outbox_terminal_retention`; no migration ran. Owner state remains ACTIVE
revision 6 and pending revision 10.

Exactly one RC11 preflight ran at `10:44:57Z` for pending revision 10. Telegram
`getMe` passed and the official provider made one `active.json` request that
returned HTTP 200. The response then failed closed as
`INVALID_RESPONSE / location_oblast_uid_integer / count=1`. There was no retry,
activation, message or delivered outbox item; the delivered-outbox baseline
remained 24. No raw UID was recorded. Fresh rollback backup:
`/home/trio/time-tl-bot/backups/pre-rc11-oblast-uid-shape-diag-20260928T104023Z`.
At that point the server truth was RC11 `HOLD`, not a staging pass.

RC12 is a narrow LOCAL compatibility candidate. It accepts a positive integer
`location_oblast_uid` only by normalizing it to the same canonical decimal form
as the documented string representation, then requires an exact match to the
top-level ancestor derived from the byte-pinned official region hierarchy for
that alert's `location_uid`. Boolean, non-positive, noncanonical, unknown and
hierarchy-mismatched values still reject the complete snapshot. Integer and
equivalent string representations produce the same canonical digest. The
undocumented alert-row key/value remains discarded and unused under the RC10
boundary.

RC12 LOCAL evidence: 82 focused tests, 238 expanded tests and 530 full-suite
tests passed; the full suite retained five opt-in destructive PostgreSQL E2E
skips and one Starlette warning. Ruff, strict mypy for 72 source files,
compileall and `git diff --check` passed. Independent safety review passed the
code-only scope. This historical LOCAL statement preceded the exact RC12 release
and is superseded for current server status by the evidence below.

The exact RC12 release was subsequently approved and installed from tag
`time-tl-staging-20260928-rc12-pinned-oblast-uid`, commit
`62205d230bee769adfa989a2328528f75372bcec`, archive SHA-256
`8d5f7c6ef71cb625c78fd42247d07e689c92327853594a02d5a4be8186b8728e`.
The server archive matched that digest and immutable image
`sha256:cb5551417738fde13a857882a215a02b2d8752c738d6c654a9bd8ea479f8ee99`
was built. `admin-web` runs RC12, is healthy and returned HTTP 200. RC12 `bot`
and all three workers are `Created`, have no startup evidence and were never
started. PostgreSQL remains at `0006_outbox_terminal_retention`; no migration
ran. Owner state remains ACTIVE revision 6 and pending revision 10.

Exactly one RC12 preflight ran at `11:17:43Z`. Telegram `getMe` passed and the
official provider made exactly one `active.json` request. The response then
failed closed as `INVALID_RESPONSE / location_oblast_uid_unknown / count=1`.
There was no retry, activation or message. The outbox remained 29 total rows and
24 delivered rows. Fresh rollback backup:
`/home/trio/time-tl-bot/backups/pre-rc12-pinned-oblast-uid-20260928T111523Z`.
At that point the server truth was RC12 `HOLD`, not a staging pass.

RC13 corrects the source contract locally: optional `location_oblast_uid` is a
dedicated bounded, non-authoritative ignored input. It is separate from the one
generic bounded undocumented alert-row field and is excluded from digest,
alert state, persistence and logs. It cannot validate, override or contradict
the required `location_uid`; authority remains the required `location_uid` plus
its pinned location type and catalogue membership. All existing complete-row,
catalogue, hierarchy, freshness, two-observation all-clear and fail-closed
barriers remain unchanged.

RC13 LOCAL evidence: 83 focused tests, 239 expanded tests and 531 full-suite
tests passed; the full suite retained five opt-in destructive PostgreSQL E2E
skips and one Starlette warning. Ruff, strict mypy for 72 source files,
compileall and `git diff --check` passed. Independent final code review passed,
and the combined code, ADR and release-evidence re-review passed after the
documentation blocker was corrected. That was LOCAL evidence before packaging
and did not itself authorize or prove a server operation.

The exact RC13 release was subsequently approved and installed from tag
`time-tl-staging-20260928-rc13-ignore-oblast-uid-hint`, commit
`f44d7ceddaf0f4baad366a03b155075edc9e4445`, archive SHA-256
`6ed97ab51e6a5ced4671bc20c1d7b92a1ea2097837d8863c69e1fc2fd7efdc1b`.
The server archive matched the approved digest and immutable image
`sha256:9683f8d69728154cf80722a8b166469c517e8ec1797d069f83dabf599e471a56`
was built. `admin-web` runs RC13, is healthy with restart count zero, and both
direct and public `/login` checks returned HTTP 200. RC13 `bot` and all three
workers are `Created` and were never started. PostgreSQL remains at
`0006_outbox_terminal_retention`; no migration ran. Owner state remained latest
revision 10, pending revision 10 and ACTIVE revision 6, with zero activation
audit entries for revision 10.

Exactly one RC13 STAGING preflight ran for pending revision 10. Telegram `getMe`
passed, then the one official `active.json` request failed closed as
`INVALID_RESPONSE / location_uid_unknown / count=1` and exited with status 2.
There was no retry, activation, startup, message or persistence/proxy change.
The outbox was unchanged at 29 total, 24 delivered and 5 undelivered rows.
Fresh verified rollback backup:
`/home/trio/time-tl-bot/backups/pre-rc13-ignore-oblast-uid-hint-20260928T113445Z`.

RC14 read-only diagnosis re-fetched the documentation-linked official catalogue
on 2026-09-28. It was byte-identical to the pinned file: 142,029 bytes, SHA-256
`a5bf1d312ee7258b16caaaf6c06e3df56fca68b0c14a060ec7c5ee7ce40b9620`,
1,622 records, 27 roots and 1,595 parent links. The official alert schema requires
`location_uid` as a string carrying an int32 value, and the official SDK provides
no alternate mapping. The live API therefore supplied a required UID absent from
the current official catalogue. Any compatibility expansion or mapping remains
`BLOCKED` pending an official updated full catalogue with UID, type and parent
data, a provider-side correction, or an official mapping. No raw UID, response
body or secret is retained here.

The RC14 LOCAL candidate preserves the existing authoritative UID
rule: `location_uid` is usable only when present in the official pinned catalogue
and its API type is compatible with the catalogue region type. An unknown,
mismatched or otherwise invalid required UID is never used, mapped or inferred.
One such row invalidates the complete snapshot globally and results in UNKNOWN,
or STALE when prior valid state exists, with no CLEAR. Adversarial tests cover
mixed known/unknown rows, prior ACTIVE state, repeated failures, restart,
parent/child scopes, recovery requiring the normal two valid clear observations,
and sanitized preflight output.

RC14 also contains LOCAL queue-safety changes. A late `LESSON_REMINDER` is
terminally suppressed at or after `starts_at`, both during claim and at the final
delivery boundary. The sender's final transaction pins the active owner revision,
the current STAGING allowlist and the expected recipient; runtime wiring passes
those expectations into the repository. Current LOCAL verification is
`542 passed, 5 skipped, 1 warning`; Ruff, strict mypy for 72 source files,
compileall and `git diff --check` pass. Independent queue-safety review issued
`PASS` for allowlisted STAGING with no CRITICAL, HIGH or MEDIUM findings. The
five skipped cases remain destructive
PostgreSQL E2E tests, and send-before-commit duplicate ambiguity remains a
residual risk.

Any future authorized startup must be sequential: start `alert-worker` alone,
establish and verify the durable restart fence, then start `schedule-worker`, and
start `sender-worker` last. Sender must not start in parallel with the other
workers. This LOCAL verification is not deployment, preflight, activation or
launch evidence. Current server verdict remains RC13 `HOLD`.
Resume file: none.
