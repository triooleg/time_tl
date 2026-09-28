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

Current release-preparation evidence recorded on 2026-09-28 is newer than the
historical 2026-09-23 baseline below:

- exact RC18 tag `time-tl-staging-20260928-rc18-russian-admin-onboarding`
  resolves to commit `0f68121503fa54fdbeea57af481df8ee0e1e73df` and tree
  `b9feaec78682f07e99fe008d78a1c64e235e43a9`;
- its recorded archive SHA-256 is
  `ae75ebb252f64501d4b0dc9a14c6aaae232034890b8ca17f940e029129a363c0`;
  the release archive and reproducibility archive matched;
- the Russian owner-admin localization and safe manual Telegram onboarding flow
  passed LOCAL/SIMULATION verification and independent bounded-STAGING review;
- the current ordinary full suite passed `596` tests, with the five opt-in
  destructive PostgreSQL cases skipped in that run;
- those five destructive cases separately passed against disposable PostgreSQL
  16.15, and a custom-format backup restored into a separate database with the
  migration head and table/count checks intact;
- production-preparation FIX3 commit
  `4e518949cbe23ca18b81ab1da649541daed02b29` passed independent R3 review at
  **LOCAL** level only. It adds a fail-closed detached release-attestation gate;
  it is not an exact production release or deployment.

RC18 has not been installed on the server. These facts do not replace the active
RC17 bounded-STAGING evidence and do not approve production or public delivery.

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
| G4 LOCAL QUALITY | PASS | RC18 ordinary full suite: `596 passed, 5 skipped`; the five opt-in destructive PostgreSQL tests separately passed on disposable PostgreSQL 16.15, and backup/restore verification passed. |
| G5 SIMULATION E2E | PASS | Exact-image server simulation, PostgreSQL fault/restart E2E, migration, backup/restore and persistence checks passed with fake provider and disabled Telegram. |
| G6 TELEGRAM STAGING | PASS (BOUNDED) | Exact RC17 one-chat allowlisted outbound `ALERT_STARTED` and `ALL_CLEAR_CONFIRMED` flow passed independent post-launch review. RC18 onboarding is verified only in LOCAL/SIMULATION; live inbound onboarding remains pending. |
| G7 SERVER READINESS | REVIEW | Exact image, backup, rollback, health, restart fence and monitoring evidence exist for bounded STAGING. Continued readiness depends on freshness, heartbeat, restart, open-outbox and retry monitoring. |
| G8 DEPLOY APPROVAL | PASS (RC17 SCOPE) | Exact RC17 package, preflight, revision-10 activation and ordered bounded-STAGING launch were approved and evidenced. This is not production approval. |
| G9 CANARY | BLOCKED | Live inbound onboarding and a separately approved public/production canary have not run. RC18 preparation and bounded one-chat RC17 STAGING evidence do not advance G9. |

## Deliberately closed capabilities

Production remains fail closed. The compile-time
`PRODUCTION_PUBLIC_RELEASE_APPROVED` switch is still `False`. FIX3 requires a
valid detached release attestation and matching active database revision before a
production role may start, but no production artifact has been attested, installed
or preflighted. The public switch must not be enabled until G7-G9 evidence and an
exact production release approval exist.

Coordinate-to-region resolution is not released. A user may select and confirm one
of the reviewed oblasts manually; raw Telegram coordinates are rejected and are not
persisted. This does not yet satisfy the requested geolocation experience.

## Required next evidence

1. If RC18 is to replace RC17 in STAGING, approve and deploy that exact package,
   then repeat the bounded checks without expanding the allowlist.
2. Exercise inbound `/start`, manual region selection, preferences, schedule and
   deletion from an approved allowlisted Telegram account. Geolocation remains
   deliberately disabled through `RejectingCoordinateResolver`.
3. Prepare and independently approve an exact production artifact plus its
   detached attestation; verify deployment-specific G7 backup, rollback, secrets,
   monitoring, database migration head and production preflight evidence.
4. Approve and run a controlled G9 canary before considering the compile-time
   public-release switch or wider delivery.

An isolated permanent SIMULATION deployment was the first server baseline. The
later STAGING-scoped, read-only provider preflights are recorded below; none
produced an accepted snapshot, activated a revision, started delivery-capable
services or sent a message. No production or canary deployment has occurred.

## Session Continuity

Last session: 2026-09-28
Stopped at: RC17 remains active in a bounded one-chat allowlisted STAGING launch.
The exact preflight, revision-10 activation, ordered startup, natural outbound
alert/all-clear flow and independent post-launch review passed. This is G6
evidence only. RC18 is a prepared, reproducible package with Russian admin and
LOCAL/SIMULATION onboarding evidence; it has not been deployed. FIX3 is LOCAL
production preparation only. Public, PRODUCTION and G9 remain blocked and
unauthorized.
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
the current official catalogue. At RC14 review time, general compatibility
expansion or mapping remained `BLOCKED` pending an official updated full
catalogue with UID, type and parent data, a provider-side correction, or an
official mapping. The later RC15 amendment below is a user-approved narrow rule,
not an official catalogue update. No raw UID, response body or secret is retained
here.

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
workers. This was LOCAL verification before packaging and did not itself prove a
deployment, preflight, activation or launch.

The exact RC14 release was subsequently approved and installed from tag
`time-tl-staging-20260928-rc14-uid-outbox-safety`, commit
`9b748bd92f220a78a5d2ade7c8925999d3f4f781`, archive SHA-256
`98c5f13d178be29684ba7ce98b517be5ddf8cd310f367097fe10f2c516ed683b`.
The verified archive contained 224 entries and was 374,281 bytes. Immutable
image `sha256:e2894574f901be2057b2b0e0d567af79aa7f566b4b217aa74b94efe1ec3b16b0`
was built. `admin-web` runs RC14, is healthy with restart count zero, and direct
and public `/login` checks returned HTTP 200. RC14 `bot` and all three workers
are `Created` and were never started. PostgreSQL remains at
`0006_outbox_terminal_retention`; no migration ran.

Exactly one RC14 STAGING preflight ran for pending revision 10. Telegram `getMe`
passed, then exactly one official `active.json` request failed closed as
`INVALID_RESPONSE / location_uid_unknown / count=1`; the preflight exited with
status 2 and did not retry. Owner state remained latest revision 10, pending
revision 10 and ACTIVE revision 6, with zero revision-10 activation audit rows.
The outbox was unchanged at 29 total, 24 delivered and 5 undelivered rows, and
the one-off count remained 0. There was no activation, service start, message,
persistence change or proxy change. The UID was not used, mapped or inferred.

Fresh verified rollback backup:
`/home/trio/time-tl-bot/backups/pre-rc14-uid-outbox-safety-20260928T120652Z`.
Its database dump is 692,590 bytes; recorded source and database hashes were
reverified. Current verdict is RC14 `HOLD`; phase 2 remains prohibited and no
launch claim is made.

RC15 adds only a bounded `+5000` live city-alias normalization while keeping the
byte-pinned official table as the sole canonical UID, Ukrainian name, type and
hierarchy. A provider alias is eligible only when `location_type` is exactly
`city`, the live UID is a canonical decimal integer greater than 5000, subtracting
5000 yields a pinned official `Громада`, and the normalized Ukrainian provider
title exactly matches the unique official `м. X` city prefix for that record.
No fuzzy title match or other numeric offset is permitted.

An accepted alias is replaced by its canonical base UID and the official pinned
name, type and hierarchy before downstream use. The provider alias UID and other
provider identity fields do not enter the digest, alert state, persistence,
messages or logs. Under the RC15 policy, an alias/canonical collision,
non-unique or mismatched title, wrong type, unknown base, malformed UID or any
other predicate failure rejects the complete snapshot and yields UNKNOWN or
STALE, never CLEAR. RC16's later collision amendment is recorded below.

Observed mappings covered by the exact rule are `5349 -> 349` (Марганець),
`5351 -> 351` (Нікополь), `5564 -> 564` (Запоріжжя) and `6293 -> 1293`
(Харків). These are compatibility inputs; the base records remain the only
canonical identities.

RC15 LOCAL evidence: 98 focused tests passed; the full suite passed 550 tests
with five destructive PostgreSQL E2E skips and one Starlette warning. Ruff,
strict mypy for 72 source files, compileall and `git diff --check` passed.
Independent safety review returned `PASS` for the LOCAL scope with no blockers.
The user-approved alias amendment is not itself an official catalogue row. A
changed catalogue hash or alias shape requires renewed review, and an exact
package, deploy, single preflight and live review remain required. This LOCAL
evidence preceded packaging and did not itself prove deployment, preflight,
activation or launch.

The exact RC15 release was subsequently approved and installed from tag
`time-tl-staging-20260928-rc15-plus5000-city-alias`, commit
`289aa18d28b30e454168083100d57dcc30f669f8`, archive SHA-256
`59574cf1e7e2869f8feeec742f0cfbcaa63ddc91a3643fedb53f2e8c8661b018`.
The verified archive contained 224 entries and was 380,541 bytes. Immutable
image `sha256:d3cc001abd3d9b4e84708a1528bd515a7755664b70eec652c1fddc718707d4cd`
was built. `admin-web` runs RC15, is healthy with restart count zero, and direct
and public `/login` checks returned HTTP 200. RC15 `bot` and all three workers
remain `Created`, with zero `StartedAt` and restart count zero. PostgreSQL remains
at `0006_outbox_terminal_retention`; no migration ran.

Exactly one RC15 STAGING preflight ran for pending revision 10. Telegram `getMe`
passed, then exactly one official `active.json` request failed closed as
`INVALID_RESPONSE / location_uid_alias_collision / count=1`; the preflight
exited with status 2 and did not retry. Owner state remained latest revision 10,
pending revision 10 and ACTIVE revision 6, with zero revision-10 activation audit
rows. The outbox remained 29 total, 24 delivered and 5 undelivered rows, and the
one-off count remained 0. There was no activation, service start, message,
persistence change or proxy change, and no raw provider identity is recorded.

Fresh verified rollback backup:
`/home/trio/time-tl-bot/backups/pre-rc15-plus5000-city-alias-20260928T135219Z`.
Its database dump is 692,590 bytes; recorded source and database hashes were
reverified. Compose hash prefix remained `b00192`. Current server verdict is RC15
`HOLD`; phase 2 remains prohibited.

A separate read-only collision diagnostic received one HTTP 200 snapshot with
56 rows and found three official base identities represented by both their direct
and `+5000` alias forms: `349/5349`, `351/5351` and `564/5564`. Each base had one
alias row and one direct row. Within each pair, location type and start time were
equal and finish time was null, while numeric alert IDs and update timestamps
differed. The diagnostic made no state change and does not establish semantic
equivalence between the two provider episodes.

RC16 therefore permits independently validated direct and alias rows with
distinct unique numeric alert IDs to coexist as separate episodes on the same
official base location identity. It performs no semantic deduplication without
authoritative equivalence evidence. If either episode disappears while the other
remains, the scope remains ACTIVE. Only absence of both can begin the unchanged
two-successful-observation all-clear sequence.

The canonical digest includes every accepted canonicalized row, sorted by its
unique numeric alert ID. Raw alias/direct source identity fields remain excluded.
A duplicate provider alert ID or any invalid row still invalidates the complete
snapshot and yields UNKNOWN or STALE, never CLEAR.

RC16 LOCAL evidence: 100 focused tests passed; the full suite passed 552 tests
with five destructive PostgreSQL E2E skips and one Starlette warning. Ruff,
strict mypy for 72 source files, compileall and `git diff --check` passed.
Independent safety review returned `PASS` with no findings. Residual provider
shape drift still requires review, and the destructive PostgreSQL cases were not
run. This LOCAL evidence preceded packaging and did not itself prove deployment,
preflight, activation or launch.

The exact RC16 release was subsequently approved and installed from tag
`time-tl-staging-20260928-rc16-city-collision-episodes`, commit
`c09201c8ad4ad000fdfe3d50f8b2e27478c0bc71`, archive SHA-256
`6fecf77d25d5b55293f0f790ecdd05e0a5ba6d09d730fab71edb74372c8c57dd`.
The verified archive contained 224 entries and was 383,778 bytes. Immutable
image `sha256:1bff5e55c00d4d526e4d0d8062f17d999ce2f0fb318c7b55410612c3b5011ecc`
was built with unchanged Compose SHA-256
`b00192a54e676a0646a89cf33d5e24ea6de167a1db84409749fadc4b9f0b472d`.
`admin-web` runs RC16, is healthy with restart count zero, and direct and public
`/login` checks returned HTTP 200. RC16 `bot` and all three workers remain
`Created` and were never started. PostgreSQL remains at
`0006_outbox_terminal_retention`; no migration ran.

Exactly one RC16 STAGING preflight ran for pending revision 10. Telegram `getMe`
passed, then exactly one official `active.json` request failed closed as
`INVALID_RESPONSE / location_type_catalogue_mismatch / count=1`; the preflight
exited with status 2 and did not retry. Owner state remained latest revision 10,
pending revision 10 and ACTIVE revision 6, with zero activation audit rows. The
outbox remained 29 total, 24 delivered and 5 undelivered rows, and the one-off
count remained 0. There was no activation, service start, message, persistence
change or proxy change.

Fresh verified rollback backup:
`/home/trio/time-tl-bot/backups/pre-rc16-city-collision-episodes-20260928T140756Z`.
Its database dump is 692,590 bytes; recorded source and database hashes were
rechecked. Current server verdict is RC16 `HOLD`; phase 2 remains prohibited.

A one-request read-only RC16 type diagnostic received HTTP 200 with 59 rows. Its
sole relevant direct row used canonical UID `31`, Ukrainian title `м. Київ` and
provider type `oblast`. The pinned exact UID/name record is a root
`Місто з спеціальним статусом` with no parent. There was one direct row and no
alias row; the diagnostic made no state change.

RC17 permits this narrow provider-type compatibility only for a direct pinned
`Місто з спеціальним статусом`: provider type may be `city` or the observed
`oblast` only when the canonical UID and normalized Ukrainian title exactly equal
the pinned official UID and name. Downstream code uses only the official pinned
identity, type and root hierarchy. Ordinary `Область` rows remain unchanged; an
unknown UID is never title-mapped or inferred. Any other drift rejects the whole
snapshot and leaves the release in HOLD.

RC17 LOCAL evidence: 106 focused tests passed; the full suite passed 558 tests
with five destructive PostgreSQL E2E skips and one Starlette warning. Ruff,
strict mypy for 72 source files, compileall and `git diff --check` passed.
Independent safety review returned `PASS` with no findings. This LOCAL evidence
preceded packaging and did not itself prove deployment, preflight, activation or
launch.

The exact RC17 release was approved and installed from tag
`time-tl-staging-20260928-rc17-special-city-type`, tag object
`0923633763cf721bd13514a035424342370c6818`, commit
`1bc6730a00361e412e56b9451e96837e23c95619`, tree
`276cde8e4e5d0338a2ba4986196b426947a77784`. The 224-entry archive was
387,260 bytes with SHA-256
`95bbef9453454e1907c27d97748dd61553d8c3191bef8ec313495f68f2021a48`.
Immutable image
`sha256:ca1318020b3d64dc40cb093ae09df24e9c2393b6757bed8c38ee48b3e6b96c07`
was used for all five application roles.

Fresh verified rollback backup:
`/home/trio/time-tl-bot/backups/pre-rc17-special-city-type-20260928T142821Z`;
the database dump was 692,590 bytes. Before activation, database revision was
`0006_outbox_terminal_retention`, owner state was latest 10 / pending 10 /
ACTIVE 6, activation audit count was zero, outbox was 29 total / 24 delivered /
5 undelivered, allowlist size was one, and workers were `Created`.

Phase 1 executed exactly one RC17 preflight. Telegram `getMe` ran once and the
official provider was called once. The full HTTP 200 snapshot passed against the
1,622-record catalogue, `Last-Modified` was accepted, and `persisted=no`. Owner
state was unchanged. Correlation ID:
`operator-preflight-v1-r10-rc17-95bbef9453454e19`.

Revision 10 was activated once with that exact correlation. Activation status
for revision 10 recorded `passed`; exactly one
`owner_configuration_activated` audit targeted revision 10, and evidence ID
SHA-256 was
`18768e932b01b0bb4a24c347acd2afc0f4bfad9e26eebf3c189c623c7019b083`.
Owner state became latest 10 / pending null / ACTIVE 10 with allowlist size one;
outbox arithmetic was unchanged at activation. No second preflight ran.

Phase 2 followed the required order. `alert-worker` started first: the durable
restart fence and first successful poll advanced revision 9937 to 9939, refreshed
success time, and left circuit open false and failure count zero. Initial live
state was one ACTIVE scope; restart/first poll did not create an all-clear, whose
historical count remained 10. The natural ACTIVE observation added one
`ALERT_STARTED` outbox row. `schedule-worker` then started healthy while sender
remained stopped, followed by healthy `bot` in STAGING live mode with allowlist
size one and no synthetic or manual input.

Before sender start, six undelivered rows were inspected. Five historical
`LESSON_REMINDER` rows were already terminally dead, expired or blocked by the
combined policy and were not deliverable. The one open `ALERT_STARTED` passed
state, freshness, mode, scope, revision, recipient and allowlist checks.
`sender-worker` started last.

During stabilization, the provider naturally transitioned ACTIVE to
`CLEAR_CONFIRMED`, producing and delivering one allowlisted
`ALL_CLEAR_CONFIRMED`. Final provider fetch revision was 9948, circuit open was
false, failure count zero, freshness age six seconds, and state distribution was
one `CLEAR_CONFIRMED`. Final outbox was 31 total / 26 delivered / 5 dead / 0
open. Since sender startup, one `ALERT_STARTED` and one `ALL_CLEAR_CONFIRMED`
were delivered to the sole allowlisted recipient; outside-allowlist deliveries,
failure rows, retries and attempt increments were zero. Sanitized transport
evidence inferred two attempts, two successes and zero failures, but does not
prove exactly-once delivery.

All five exact-image services finished healthy at owner revision 10 with restart
count zero. Worker heartbeats were healthy with ages 4–13 seconds; one-off count
was zero, and direct/public `/login` returned HTTP 200. No manual/synthetic
event, second preflight, proxy, DNS, firewall, code or image change occurred.

Independent post-launch review returned `PASS` with no findings for this bounded
one-chat allowlisted STAGING scope. It independently verified restart/false-clear
safety and outbox arithmetic. G6 passes for the outbound alert/all-clear flow
only. Inbound onboarding UX was not exercised. Residual risks are Telegram
send-before-commit duplicate ambiguity/no exactly-once guarantee and five skipped
destructive PostgreSQL E2E tests. Monitoring must HOLD on freshness, heartbeat,
restart, open-outbox or retry drift. G9, public and PRODUCTION remain blocked and
unauthorized.
Resume file: none.
