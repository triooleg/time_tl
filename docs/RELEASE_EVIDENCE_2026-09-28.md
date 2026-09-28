# Time TL RC8 incident through RC12 local compatibility — 2026-09-28

## Scope

This record covers the RC8 provider-envelope compatibility release, its bounded
server operation, the later exact RC9 diagnostic-only deployment, the exact RC10
and RC11 deployments and preflights, and the RC12 local compatibility candidate. It records
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
The current server verdict remains RC11 `HOLD`; this diagnostic outcome is not a
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
tests are the opt-in destructive PostgreSQL E2E cases. RC12 is not committed,
tagged, packaged, deployed or preflighted. These results do not authorize a
provider call, activation, bot/worker startup or messages. The current server
truth remains RC11 `HOLD`, with ACTIVE revision 6, pending revision 10 and all
delivery-capable services stopped.
