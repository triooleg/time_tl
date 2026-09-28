# Time TL RC8 incident and RC9 diagnostic deployment evidence — 2026-09-28

## Scope

This record covers the RC8 provider-envelope compatibility release, its bounded
server operation, and the later exact RC9 diagnostic-only deployment. It records
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

### Current verdict and external blocker

The server remains in `HOLD`. Independent safety review accepted the contained
state only while `bot` and all three workers remain stopped, and rejected both
raw-name and hash-based diagnostics for the unknown provider field because they
could disclose or make provider-controlled information re-identifiable. Such a
diagnostic must not be added merely to force schema discovery.

RC9 is `EXTERNALLY BLOCKED` pending either an authoritative provider schema
update that defines the live field or a provider-supplied sanitized response
sample suitable for review. The HTTP 200 is transport evidence, not a valid
alert snapshot and not an all-clear. Activation remains blocked. G6-G9 are not
advanced; there was no canary, bot/worker execution or message delivery.

No further preflight is authorized by this record.
