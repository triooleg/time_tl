# Time TL RC8 local candidate evidence — 2026-09-28

## Scope

This record covers the RC8 provider-envelope compatibility patch in the local
candidate identified by the accompanying release manifest. It records local non-destructive verification and an
independent safety re-review of the patch scope. It is not a release manifest,
immutable version, server verification, Telegram staging result, deployment
approval or production evidence.

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
| G6 TELEGRAM STAGING | `BLOCKED` (unchanged; not advanced) | No Telegram test bot, allowlist or live Telegram E2E was used. |
| G7 SERVER READINESS | `BLOCKED` (unchanged; not advanced) | No server, backup, rollback, image or Compose verification was performed for RC8. |
| G8 DEPLOY APPROVAL | `BLOCKED` (unchanged; not advanced) | The exact local packet is identified as `time-tl-staging-20260928-rc8-bounded-meta`, but it has no deployment approval. |
| G9 CANARY | `BLOCKED` (unchanged; not advanced) | No canary or real-user delivery was performed. |

G0, G1, G2 and G5 are unchanged by this local patch. Passing G3 for the RC8 patch
scope and G4 locally does not prove staging, release or production readiness.

## Environment boundary

No server, network, alerts.in.ua request, Telegram transport, secret, migration,
Portainer stack, DNS, reverse proxy, firewall or push was created or changed for
this evidence record. Local-only commit, annotated tag and archive preparation
are recorded in the accompanying RC8 manifest. The current STAGING deployment
remains RC6, as recorded by the prior RC7 local-candidate manifest; RC7 and RC8
local evidence do not imply a STAGING update.
