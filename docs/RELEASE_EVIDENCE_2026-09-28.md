# Time TL RC8 incident through RC14 local safety candidate — 2026-09-28

## Scope

This record covers the RC8 provider-envelope compatibility release, its bounded
server operation, the later exact RC9 diagnostic-only deployment, the exact RC10
through RC13 deployments and preflights, and the RC14 local safety
candidate. It records
local non-destructive verification, independent safety reviews and the actual
server outcomes, including the RC8 breach of the approved Telegram-disablement
boundary and the contained RC9 read-only diagnostic. It is not evidence of
Telegram staging, canary or production readiness.

The authorized packet was:

- tag: `time-tl-staging-20260928-rc8-bounded-meta`;
- commit: `eeca2f8fb80a2d6d8890dbd268799e470f460c82`;
- archive SHA-256:
  `bb44f8aa79b8d52518f3e369367d8adc6a9667d95e66d8558ad792d918bf03fe`;
- permitted operation: install `bot` and `admin-web`, then perform no more than
  one safe preflight while keeping all workers and Telegram disabled.

RC8 keeps the official `active.json` root contract. The patch adds bounded
compatibility for additional fields inside the official SDK `meta` object and
hardens JSON parsing against duplicate object keys and excessive nesting. The
required `last_updated_at` timestamp remains strict. At most eight additional
fields are accepted when all key, type, depth, element, node, string, numeric and
serialized size bounds pass. Their canonical representation is content-digest
input only; metadata never decides ACTIVE or CLEAR.

Duplicate JSON object keys at any level and JSON that is too deeply nested to
parse safely are classified as redacted invalid responses. Any RC8 compatibility
validation failure after an active observation produces `UNKNOWN`/`STALE` and
cannot create `CLEAR_CONFIRMED`.

## Local verification

| Environment | Check | Result |
|---|---|---|
| LOCAL | Focused provider and worker regression tests | `52 passed` |
| LOCAL | Full suite with import path explicitly pinned to `D:\Projects\Time_tl_RC8\src` | `508 passed, 5 skipped, 1 warning` |
| LOCAL | Ruff | `PASS` |
| LOCAL | strict mypy | `PASS`; 72 source files checked |
| LOCAL | `compileall` | `PASS` |
| LOCAL REVIEW | Independent safety re-review of the RC8 patch scope | `PASS` |

The single full-suite warning is a Starlette deprecation warning. The five
skipped tests are the opt-in destructive PostgreSQL E2E cases. They were not
enabled or rerun for RC8, so this record makes no new PostgreSQL fault/restart or
server claim.

## Gate status for this patch

| Gate | Verdict | Evidence or remaining work |
|---|---|---|
| G3 ALERT SAFETY | `PASS` | LOCAL patch scope only. Focused regressions cover bounded extra metadata, digest identity, duplicate object keys, excessive JSON depth, redacted failures, and no false all-clear after ACTIVE. No official-provider or server claim is implied. |
| G4 LOCAL QUALITY | `PASS` | Non-destructive suite scope. Full suite, Ruff, strict mypy and compileall passed. Five destructive PostgreSQL E2E cases remained skipped and were not rerun. |
| G6 TELEGRAM STAGING | `BLOCKED` (not advanced) | Live Telegram polling occurred contrary to the authorization, with no handled updates or delivery evidence. This breach is not a staging pass. |
| G7 SERVER READINESS | `REVIEW` (not advanced) | Backup, rollback, image, Compose, admin health and current schema were checked, but the database-owned runtime configuration caused an unauthorized outbound capability. The contained server state is `HOLD`, not readiness. |
| G8 DEPLOY APPROVAL | `REJECTED` (not advanced) | Exact-version approval was granted, but the executed operation breached its Telegram-disablement boundary. |
| G9 CANARY | `BLOCKED` (not advanced) | No canary was approved or performed. |

G0, G1, G2 and G5 are unchanged by this local patch. Passing G3 for the RC8 patch
scope and G4 locally does not prove staging, release or production readiness.

## Authorized server attempt and exact outcome

The server copy of the archive matched the approved SHA-256. The RC8 image was
built from the approved commit and has the recorded image ID prefix
`sha256:b31fc47c`. The actual pre-change server baseline was RC7, image ID prefix
`sha256:ccf9eb`; the older RC6 statement in the local-candidate record was stale
and is superseded by this observed baseline.

Fresh rollback and backup evidence was verified before the change. The Compose
file hash remained unchanged with prefix `b00192a`. The database remained at
Alembic revision `0006_outbox_terminal_retention`; the migration job was not run
and RC8 contains no migration. After containment:

- `admin-web` runs the RC8 image, is healthy, has restart count zero and returned
  HTTP 200 from `/login`;
- `bot` runs the RC8 image but is stopped/exited with restart count zero;
- `alert-worker`, `schedule-worker` and `sender-worker` are `Created`/stopped;
- PostgreSQL and node-exporter were not recreated.

## Telegram-disablement boundary breach

The deployment operation is `REJECTED`. Database-backed owner configuration took
precedence over container environment settings: ACTIVE revision 6 selected live
Telegram transport. The RC8 bot therefore performed live Telegram polling from
`08:27:11Z` through `08:31:24Z`, approximately 4 minutes 13 seconds, contrary to
the explicit approval. The bot was then stopped.

Evidence for that polling window shows zero handled updates, zero Telegram send
markers and zero delivered outbox messages. There is no evidence that a message
was sent, but continuous Telegram disablement cannot be claimed. Alert, schedule
and sender workers remained stopped, so no provider snapshot or notification
delivery workflow ran and no false all-clear was observed.

The current server condition is `HOLD`, and is acceptable only while `bot` and all
three workers remain stopped. This is containment evidence, not a successful
deployment or a gate advancement.

## Initial RC8 preflight not executed

During the initial RC8 deployment attempt, no operator preflight container was
created and no alerts.in.ua or Telegram preflight call was made. Pending revision
9 specified `production`, so RC8's STAGING-only preflight rejected it before
external calls. Owner audit entries created by that operation: zero. Owner
settings were not changed during that operation.

The remediation boundary established after that incident was two-step and
separately reviewable:

1. Authorize creation of the next pending owner revision for STAGING only,
   preserving the already reviewed provider, credential references and approved
   allowlist, without activation, external calls or starting any service.
2. After verifying the exact revision and approved test account, separately
   authorize one read-only preflight only.

Neither step authorizes activation, canary, workers, bot startup or messages.

## RC9 diagnostic-only release

RC9 is a separate diagnostic release. Its exact identity is:

- tag: `time-tl-staging-20260928-rc9-alert-field-diag`;
- commit: `fb4c64d96e740ddd811b88f35b36a1c3187c8312`;
- archive SHA-256:
  `1e271f50c8fd20facfa8037252fd094ecf59e0688c546c1e43a104ea17519760`.

It does not change or supersede the RC8 deployment verdict, server containment
requirements or gate state.

The current official API schema omits the alert-row field `calculated`. The
official Python SDK reads it, but neither its wire type nor its safety semantics
is documented. RC9 does not accept the field and does not use it in alert-state
decisions. A response containing `calculated` remains `INVALID_RESPONSE` and
fails closed through `UNKNOWN`/`STALE`; it cannot produce ACTIVE, CLEAR or
`CLEAR_CONFIRMED` from that response. The patch adds only fixed, opaque diagnostic
classification of the observed value as `null`, `boolean` or `other`, without
recording or exposing the value itself.

### RC9 local verification

| Environment | Check | Result |
|---|---|---|
| LOCAL | Focused provider and preflight regression tests | `57 passed` |
| LOCAL | Full suite with the RC8 checkout import path explicitly pinned | `513 passed, 5 skipped, 1 warning` |
| LOCAL | Ruff | `PASS` |
| LOCAL | strict mypy | `PASS`; 72 source files checked |
| LOCAL | `compileall` | `PASS` |
| LOCAL | `git diff --check` | `PASS` |
| LOCAL REVIEW | Independent safety review of the diagnostic-only scope | `PASS` |

The warning and skips retain the same boundary as RC8: one Starlette deprecation
warning and five opt-in destructive PostgreSQL E2E tests not enabled for this
run. This table is LOCAL evidence only; the separately authorized server action
and its evidence are recorded below.

### Exact RC9 server operation

The exact archive transfer, server-side SHA-256 verification, immutable image
build, `admin-web` replacement, stopped `bot` creation and one diagnostic
read-only preflight were explicitly authorized. Activation, bot or worker start,
and message delivery were expressly excluded.

The server archive SHA-256 matched the approved value. The immutable RC9 image
has prefix `sha256:fea4ff501b87`. The unchanged Compose file remained in use.
After the operation:

- `admin-web` runs RC9, is healthy, has restart count zero and returned HTTP 200;
- `bot`, `alert-worker`, `schedule-worker` and `sender-worker` are `Created` with
  zero `StartedAt` values and were never started;
- PostgreSQL remains at `0006_outbox_terminal_retention`; no migration ran;
- owner configuration remains latest revision 10, pending revision 10 and ACTIVE
  revision 6; revision 10 has zero activation audit entries.

Exactly one RC9 diagnostic read-only preflight ran at `09:22:57Z` for pending
revision 10. Telegram `getMe` passed. The official provider made exactly one
`active.json` request and returned HTTP 200, after which RC9 failed closed with
the bounded result `INVALID_RESPONSE / alert_fields_unknown / count=1`. The
response did not match RC9's `calculated` diagnostic category. No retry,
activation, message, delivered outbox item, raw response, unknown field name or
unknown value was recorded.

A fresh rollback backup is retained at
`/home/trio/time-tl-bot/backups/pre-rc9-alert-field-diag-20260928T092016Z`.
The RC8 and RC7 images also remain available for rollback.

### RC9 verdict before the RC10 compatibility decision

The server remains in `HOLD`. Independent safety review accepted the contained
state only while `bot` and all three workers remain stopped, and rejected both
raw-name and hash-based diagnostics for the unknown provider field because they
could disclose or make provider-controlled information re-identifiable. Such a
diagnostic must not be added merely to force schema discovery.

At that point RC9 was `EXTERNALLY BLOCKED` pending either an authoritative
provider schema update that defined the live field or a provider-supplied
sanitized response sample suitable for review. The later user decision and RC10
local candidate are recorded below; they do not retroactively make the RC9
response valid. Its HTTP 200 was transport evidence, not a valid alert snapshot
and not an all-clear. Activation remains blocked. G6-G9 are not advanced; there
was no canary, bot/worker execution or message delivery.

No further preflight is authorized by this record.

## RC10 local compatibility candidate

After RC9 established that the live extra alert-row field was not the SDK's
`calculated` field, the user directed that an undocumented field must not be
used. RC10 is a narrow local candidate implementing that decision. It does not
identify, interpret or retain the provider-controlled key or value.

The compatibility boundary is:

- at most one undocumented key is tolerated per alert row;
- all rows that contain an undocumented key must use the same key;
- the value is limited by the existing bounded JSON type, depth, node, container,
  string and numeric rules, plus a 32,768-byte per-row serialized maximum;
- the undocumented key and value are removed before canonical digest generation
  and cannot enter alert episodes, state, diagnostics, logs or persistence;
- every documented field remains strictly validated, including required fields,
  types, enums, UID/catalogue relationships, timestamps and threat rows;
- inconsistent keys, multiple extra keys or structurally invalid extra values
  reject the complete snapshot through the existing fail-closed path;
- parent/child hierarchy applicability and the two-successful-observation
  requirement before `CLEAR_CONFIRMED` are preserved.

### RC10 local verification

| Environment | Check | Result |
|---|---|---|
| LOCAL | Focused provider and preflight regressions | `58 passed` |
| LOCAL | Full suite with this checkout explicitly pinned through `PYTHONPATH` | `514 passed, 5 skipped, 1 warning` |
| LOCAL | Ruff | `PASS` |
| LOCAL | strict mypy | `PASS`; 72 source files checked |
| LOCAL | `compileall` | `PASS` |
| LOCAL | `git diff --check` | `PASS` |
| LOCAL REVIEW | Independent safety review of the code-only RC10 scope | `PASS` |

The explicit source-path pin was necessary because a stale editable-install
`.pth` entry otherwise resolved a different checkout. The local editable entry
was then corrected to `D:\Projects\Time_tl_RC8\src`; an unpinned focused rerun
resolved the current source and passed 58 tests. The warning remains the
Starlette deprecation warning, and the five skipped tests remain opt-in
destructive PostgreSQL E2E cases. RC10 has no exact release tag, commit, archive
hash or immutable image yet. It has not been transferred, installed or executed
on the server; this table must not be represented as server or staging evidence.

### Current server and admin evidence

The server remains on RC9 `HOLD`: owner revision 6 is ACTIVE, revision 10 is
pending, and `bot`, `alert-worker`, `schedule-worker` and `sender-worker` remain
stopped. No RC10 preflight, activation, bot/worker start or message delivery has
occurred.

The user-reported public admin HTTP 403 was transient and was not reproduced by
the current read-only diagnosis: both the public `/login` route and its backend
endpoint returned HTTP 200. No proxy, access-control or application mutation was
made, so the cause of the screenshot's earlier 403 is not established and no
repair claim is made.

That statement described the state before the exact RC10 approval and is
superseded by the server evidence below.

## Exact RC10 server operation and HOLD

The exact approved RC10 identity was:

- tag: `time-tl-staging-20260928-rc10-ignore-undocumented`;
- commit: `42352cb6c04b00e912b847befb8aa91c67d009c9`;
- archive SHA-256:
  `2ed1cf7cfedf7132776487a406b0293a028e8c4817316763210f515679b940e3`.

The approved scope allowed archive transfer, server-side SHA-256 verification,
an immutable image build, `admin-web` replacement, and exactly one preflight for
pending revision 10. Activation and allowlisted bot/worker startup were
conditional on a completely successful preflight and an independent safety
`PASS`; messages outside the approved allowlist were prohibited.

The server archive matched the approved SHA-256 and immutable RC10 image
`sha256:577fa52128fdecfeaa9370a56039f04fdb1da266881f9f62d899130c72fbc16d`
was built. `admin-web` runs RC10, is healthy and returned HTTP 200. RC10 `bot`,
`alert-worker`, `schedule-worker` and `sender-worker` were created stopped and
have never been started. PostgreSQL remains at
`0006_outbox_terminal_retention`; no migration ran. Owner state remains ACTIVE
revision 6 and pending revision 10.

Exactly one RC10 preflight ran at `10:19:39Z`. Telegram `getMe` passed. The
official provider made exactly one `active.json` request and returned HTTP 200,
after which RC10 failed closed with
`INVALID_RESPONSE / location_oblast_uid_format_invalid / count=1`. There was no
retry, activation, message or delivered outbox item.

A fresh verified rollback backup is retained at
`/home/trio/time-tl-bot/backups/pre-rc10-ignore-undocumented-20260928T101742Z`.

Independent safety review rejected activation because the preflight condition
did not pass. It accepted the contained `HOLD` state only while `bot` and all
workers remain stopped. Thus the exact deployment/preflight authorization was
used, but G6 Telegram staging, G7 server readiness and G9 canary were not
advanced. The HTTP 200 is transport evidence only, not a valid alert snapshot or
an all-clear.

## RC11 local diagnostic candidate

RC11 is a code-only diagnostic refinement for the exact predicate rejected by
the RC10 preflight. It keeps an invalid optional `location_oblast_uid` fail
closed and emits only one fixed shape category:

- `location_oblast_uid_boolean`;
- `location_oblast_uid_integer`;
- `location_oblast_uid_noncanonical_string`;
- `location_oblast_uid_other`.

The rejected value and the ignored undocumented key/value are not emitted. A
rejected response produces no snapshot, cannot advance the validated baseline,
and after an ACTIVE observation moves state to STALE without adding outbox work
or producing `CLEAR_CONFIRMED`. Canonical strings still undergo the existing
official catalogue and location-type validation.

### RC11 local verification

| Environment | Check | Result |
|---|---|---|
| LOCAL | Focused provider and preflight regressions | `66 passed` |
| LOCAL | Full suite | `522 passed, 5 skipped, 1 warning` |
| LOCAL | Ruff | `PASS` |
| LOCAL | strict mypy | `PASS`; 72 source files checked |
| LOCAL | `compileall` | `PASS` |
| LOCAL | `git diff --check` | `PASS` |
| LOCAL REVIEW | Independent safety review of the code-only RC11 scope | `PASS` |

The warning is the existing Starlette deprecation warning. The five skipped
tests are the opt-in destructive PostgreSQL E2E cases. These results were LOCAL
evidence only and did not themselves authorize another provider call,
activation, bot/worker startup or messages. RC11 was subsequently approved for
the exact diagnostic-only server operation recorded below.

## Exact RC11 diagnostic server operation and HOLD

The exact approved RC11 identity was:

- tag: `time-tl-staging-20260928-rc11-oblast-uid-shape-diag`;
- commit: `2e9a29ea1feaa3730eee7f022ae7316ad1ee28cf`;
- archive SHA-256:
  `b8c869990a323be1627c1ac986e1f93841a973361654a2cf928852630aca0550`.

The approved operation allowed archive transfer, server-side digest
verification, immutable image construction, `admin-web` replacement, stopped
`bot`/worker creation and exactly one diagnostic preflight for pending revision
10. Activation, bot/worker startup and messages were explicitly excluded.

The server archive matched the approved SHA-256. Immutable RC11 image
`sha256:10355985d0e6343382e95ef60db57eccc43fad3e665c757f226bcfebe87c2095`
was built. `admin-web` runs RC11, is healthy, and returned HTTP 200 through both
the public and direct checks. RC11 `bot`, `alert-worker`, `schedule-worker` and
`sender-worker` are `Created`, have no startup evidence and were never started.
PostgreSQL remains at `0006_outbox_terminal_retention`; no migration ran. Owner
configuration remains ACTIVE revision 6 and pending revision 10.

Exactly one RC11 preflight ran at `10:44:57Z`. Telegram `getMe` passed. The
official provider made exactly one `active.json` request and returned HTTP 200,
after which the response failed closed as
`INVALID_RESPONSE / location_oblast_uid_integer / count=1`. There was no retry,
activation, message or delivered outbox item. The delivered-outbox baseline
remained 24. No raw UID, provider identity value or secret is included in this
record.

A fresh rollback backup is retained at
`/home/trio/time-tl-bot/backups/pre-rc11-oblast-uid-shape-diag-20260928T104023Z`.
At that point the server verdict was RC11 `HOLD`; this diagnostic outcome is not a
staging pass and does not advance G6, G7 or G9.

## RC12 local integer compatibility candidate

RC12 is a code-only compatibility change for the single shape established by
the RC11 diagnostic. A positive integer `location_oblast_uid` is normalized to
its canonical decimal string, but is accepted only when it is present as a
top-level UID in the byte-pinned official catalogue and exactly equals the
expected top-level ancestor of that alert row's `location_uid`.

The expected-ancestor mapping is derived from the already pinned and validated
provider hierarchy, covers every pinned region UID exactly, maps each top-level
region to itself, and maps every district/community through its pinned parents.
Boolean and non-positive values, unknown UIDs, noncanonical strings, unsupported
types and hierarchy mismatches continue to reject the complete snapshot. An
accepted integer and its equivalent canonical string are normalized before the
content digest, so they cannot create different content identities. The RC10
rule for discarding and never using the single bounded undocumented alert-row
field remains unchanged.

### RC12 local verification

| Environment | Check | Result |
|---|---|---|
| LOCAL | Focused provider, registry and preflight regressions | `82 passed` |
| LOCAL | Expanded alert/runtime regressions | `238 passed` |
| LOCAL | Full suite | `530 passed, 5 skipped, 1 warning` |
| LOCAL | Ruff | `PASS` |
| LOCAL | strict mypy | `PASS`; 72 source files checked |
| LOCAL | `compileall` | `PASS` |
| LOCAL | `git diff --check` | `PASS` |
| LOCAL REVIEW | Independent safety review of the code-only RC12 scope | `PASS` |

The warning is the existing Starlette deprecation warning and the five skipped
tests are the opt-in destructive PostgreSQL E2E cases. This was the LOCAL
evidence available before exact RC12 packaging and deployment. It did not by
itself authorize a provider call, activation, bot/worker startup or messages.

## Exact RC12 server operation and HOLD

The exact approved RC12 identity was:

- tag: `time-tl-staging-20260928-rc12-pinned-oblast-uid`;
- commit: `62205d230bee769adfa989a2328528f75372bcec`;
- archive SHA-256:
  `8d5f7c6ef71cb625c78fd42247d07e689c92327853594a02d5a4be8186b8728e`.

The server archive matched the approved digest. Immutable RC12 image
`sha256:cb5551417738fde13a857882a215a02b2d8752c738d6c654a9bd8ea479f8ee99`
was built. `admin-web` runs RC12, is healthy and returned HTTP 200. RC12 `bot`,
`alert-worker`, `schedule-worker` and `sender-worker` are `Created`, have no
startup evidence and were never started. PostgreSQL remains at
`0006_outbox_terminal_retention`; no migration ran. Owner state remains ACTIVE
revision 6 and pending revision 10.

Exactly one RC12 preflight ran at `11:17:43Z`. Telegram `getMe` passed and the
official provider made exactly one `active.json` request. The response failed
closed as `INVALID_RESPONSE / location_oblast_uid_unknown / count=1`. There was
no retry, activation or message. The outbox remained 29 total rows and 24
delivered rows.

A fresh rollback backup is retained at
`/home/trio/time-tl-bot/backups/pre-rc12-pinned-oblast-uid-20260928T111523Z`.
At that point the server verdict was RC12 `HOLD`; the result is not a staging pass and
does not advance G6, G7 or G9.

## RC13 local contract correction

RC13 removes authority from the optional `location_oblast_uid` hint. When
present, that field is accepted only as a dedicated bounded input and is then
discarded. It is separate from the one generic bounded undocumented alert-row
field, so both may coexist without merging their contracts. The optional hint
is excluded from canonical digest generation, alert state, persistence,
diagnostics and logs.

The required `location_uid`, compatible pinned location type and byte-pinned
catalogue membership remain authoritative. The optional hint cannot repair or
override a missing, malformed, unknown or type-incompatible required UID. The
existing complete-row validation, generic-extra bounds, hierarchy scope,
freshness/restart behavior, two-successful-observation all-clear rule and all
fail-closed barriers remain unchanged.

### RC13 local verification

| Environment | Check | Result |
|---|---|---|
| LOCAL | Focused provider, registry and preflight regressions | `83 passed` |
| LOCAL | Expanded alert/runtime regressions | `239 passed` |
| LOCAL | Full suite | `531 passed, 5 skipped, 1 warning` |
| LOCAL | Ruff | `PASS` |
| LOCAL | strict mypy | `PASS`; 72 source files checked |
| LOCAL | `compileall` | `PASS` |
| LOCAL | `git diff --check` | `PASS` |
| LOCAL REVIEW | Independent final code review | `PASS` |
| LOCAL REVIEW | Combined code, ADR and release-evidence re-review | `PASS` |

The warning is the existing Starlette deprecation warning and the five skipped
tests are the opt-in destructive PostgreSQL E2E cases. The combined independent
re-review found no residual safety or documentation blocker. This was LOCAL
evidence before exact RC13 packaging. It did not itself authorize a provider
call, activation, service startup or messages.

## Exact RC13 server operation and HOLD

The exact approved RC13 identity was:

- tag: `time-tl-staging-20260928-rc13-ignore-oblast-uid-hint`;
- commit: `f44d7ceddaf0f4baad366a03b155075edc9e4445`;
- archive SHA-256:
  `6ed97ab51e6a5ced4671bc20c1d7b92a1ea2097837d8863c69e1fc2fd7efdc1b`;
- immutable image:
  `sha256:9683f8d69728154cf80722a8b166469c517e8ec1797d069f83dabf599e471a56`.

The server archive matched the approved digest. `admin-web` runs RC13, is
healthy with restart count zero, and returned HTTP 200 through both direct and
public `/login` checks. RC13 `bot`, `alert-worker`, `schedule-worker` and
`sender-worker` are `Created` and were never started. PostgreSQL remains at
`0006_outbox_terminal_retention`; no migration ran. Owner state before and after
the operation was latest revision 10, pending revision 10 and ACTIVE revision 6.
Revision 10 has zero activation audit entries.

Exactly one RC13 STAGING preflight ran for pending revision 10. Telegram `getMe`
passed. The official provider then made exactly one `active.json` request, which
failed closed as `INVALID_RESPONSE / location_uid_unknown / count=1`; the
preflight exited with status 2. There was no retry, activation, bot/worker start,
message, persistence change or proxy change. The outbox was unchanged at 29
total rows, 24 delivered rows and 5 undelivered rows.

A fresh rollback backup is retained at
`/home/trio/time-tl-bot/backups/pre-rc13-ignore-oblast-uid-hint-20260928T113445Z`.
The current server verdict is RC13 `HOLD`. This transport and fail-closed
evidence is not a successful Telegram staging run, canary or launch and does not
advance G6 or G9. G7 server-readiness evidence is partial: the backup, immutable
image, health checks and rollback material passed, while the provider contract
blocker prevents a release-ready verdict.

## RC14 upstream-contract diagnosis — BLOCKED

A read-only source diagnosis re-fetched the documentation-linked official
location CSV on 2026-09-28. It was byte-identical to the pinned catalogue:

- 142,029 bytes;
- SHA-256 `a5bf1d312ee7258b16caaaf6c06e3df56fca68b0c14a060ec7c5ee7ce40b9620`;
- 1,622 records;
- 27 roots;
- 1,595 parent links.

The official alert schema requires `location_uid` as a string carrying an int32
value. The official SDK supplies no alternate mapping from an API UID to type and
parent hierarchy. The RC13 STAGING response therefore exposed an inconsistency
between the official live API and the current documentation-linked official
catalogue. Its raw UID and body are deliberately omitted.

Any RC14 UID compatibility expansion is `BLOCKED`: accepting an unknown required
UID, inventing its type or parent, or deriving hierarchy from an unofficial
source would weaken the reviewed alert and all-clear boundary. Compatibility
work can resume only with an official updated full catalogue containing UID,
type and parent data, a provider-side correction, or an official mapping.

## RC14 local safety candidate

The user's authoritative UID rule matches the existing production code: a
required `location_uid` is usable only when present in the official pinned
catalogue and type-compatible with that catalogue region. Unknown, malformed or
type-mismatched UIDs are never used, mapped or inferred. One invalid row rejects
the complete snapshot globally, so known rows from the same mixed response are
also discarded. The result is UNKNOWN with no prior valid state or STALE while
preserving prior valid state; it cannot produce CLEAR or `CLEAR_CONFIRMED`.

New adversarial LOCAL tests cover mixed known and unknown rows, failure after an
ACTIVE observation, repeated unknown responses, process restart, parent and
child scopes, recovery followed by the normal two-observation clear sequence,
and sanitized preflight failure output. These tests strengthen evidence for the
existing fail-closed rule; they do not make the live unknown UID valid.

The same candidate adds two backend delivery barriers:

- a `LESSON_REMINDER` at or after its `starts_at` is terminally suppressed both
  when claiming a batch and at the final delivery boundary;
- the sender final-authorization transaction pins the active owner revision,
  current STAGING allowlist and expected recipient, with those expectations
  supplied by runtime wiring.

### RC14 current local verification

| Environment | Check | Result |
|---|---|---|
| LOCAL | Full suite | `542 passed, 5 skipped, 1 warning` |
| LOCAL | Ruff | `PASS` |
| LOCAL | strict mypy | `PASS`; 72 source files checked |
| LOCAL | `compileall` | `PASS` |
| LOCAL | `git diff --check` | `PASS` |
| LOCAL REVIEW | Independent safety review | `PASS` for allowlisted STAGING; no CRITICAL, HIGH or MEDIUM findings |

The warning is the existing Starlette deprecation warning. The five skipped
tests are the destructive PostgreSQL E2E cases, so the new transactional
barriers do not yet have fresh destructive-database evidence in this run. A
residual send-before-commit duplicate ambiguity also remains: an external send
can succeed before the database records completion.

If a later exact release is independently accepted and explicitly authorized,
worker startup must be sequential: start `alert-worker` alone, establish and
verify the durable restart fence, then start `schedule-worker`, and start
`sender-worker` last. The sender must not be started in parallel with the other
workers.

This LOCAL evidence is not an immutable image, deployment, preflight,
activation or launch identity. It does
not change the RC13 server `HOLD`: bot and workers remain stopped and revision 10
remains unactivated.
