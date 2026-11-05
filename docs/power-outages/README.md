# Power schedules: subscribed Alerts Energy API

## Current scope — 2026-11-04

The user confirmed annual Alerts Energy API access and replaced the HTML-only
request with the documented API as the primary candidate. Historical HTML audits
record the old approach; their collection restrictions do not block this API.

[API guidance](https://alerts.energy/developers/api) and
[terms section 7](https://alerts.energy/terms) permit the owner's application/bot
under an active subscription within documented limits. Resale and bulk
redistribution have separate restrictions.

## Adding and keeping the key

The admin-only update is deployed (see
[exact receipt](../operations/ENERGY-API-ADMIN-20261104.md)). Open the owner
dashboard → Alerts Energy API → Настроить API-ключ графиков отключений.
Direct page: /power/alerts-energy-api. It is independent of the power feature
flag and requires the existing OWNER_ALERTS_FERNET_KEY.

1. Enter the provider key and current owner password; click «Сохранить ключ».
2. Separately click «Проверить доступ API» with owner reauthentication.
   This calls only documented GET /api/v1/check, consuming account quota.
3. Metadata shows whether a key is configured and the last check result/time.
   The key is never displayed again; it can be replaced or deleted.

Saving does not activate collection or Telegram. PostgreSQL stores a
purpose-bound encrypted envelope in a separate runtime_settings record. HKDF
derives the service encryption key from the existing owner seed; no new persisted
seed is generated. Preserve the original seed in protected backup/escrow:
losing it prevents decrypting the saved provider key. Existing encrypted database
backups include the encrypted record.

HTML, public metadata, audit and generic settings access expose neither plaintext
nor ciphertext. Credential responses are no-store. Owner authorization, CSRF,
password, bounded strict forms, CAS and persisted check cooldown apply.
Save/delete/re-save cannot erase account cooldown. An old check cannot mark a
new key valid, but its quota delay is retained.

## API contract and current coverage

Base: https://alerts.energy/api/v1. Bearer header over HTTPS, never key in URL.
Only documented check, regional suppliers and shutdowns routes are used.
No HTML or internal-site fallback exists.

Known Telegram binding candidates cover DTEK Dnipro and CEK Dnipro. The legacy
single-provider fetch checks suppliers before reading its region; central map
collection instead reads every operator/group with one regional call and never
uses that legacy two-call path. Unsupported identities are retained in the map,
without guessing a Telegram binding. Known-provider group mapping is 1.1–6.2.
See [central map cache](MAP-CACHE.md) for the 300-second country cycle.

[Schedule documentation](https://alerts.energy/developers/api/operators/source-registry-areas-slug-shutdowns)
defines explicit Kyiv dates/statuses and numeric whole/half-hour intervals.
Uncertain possible periods remain UNKNOWN. Empty/waiting means no confirmed
schedule; emergency, cancelled-GPV or unclassified applicable announcements
suppress prior plans. Missing timestamps, wrong dates, unknown groups and
ambiguous DST data cannot authorize reminders. A plan is not physical supply.

[Shared account limits](https://alerts.energy/developers/api/authentication):
5 requests/second and 10,000/day, resetting at Kyiv midnight. Respect Retry-After
and rate headers. Missing/malformed bounded 429 delay conservatively retains
RATE_LIMITED with a one-day pause. Central polling, owner checks and the runtime
address port now share a persisted PostgreSQL account budget. Viewing schedules
does not consume API requests. External use of the same account is not included
in the local counter; provider 429 is authoritative.

## Shared geography and address lookup

Reuse the actor's confirmed active territory and external KATOTTG. Alert-provider
UIDs, legacy numeric offsets and matching names are not address identifiers.
PostgresPowerBotPort.location_seed() provides a read-only hint without changing
air-alert territory.

The current alert catalogue usually ends at a hromada, which may contain several
settlements. The locally implemented wizard follows:

Existing oblast/hromada → choose settlement if needed → street → house/corpus
→ API operator/group candidates → explicit confirmation.

A confirmed special-status city can seed its validated city code directly.
Unknown mappings require clarification, never silent selection.

Documented address methods:
[street search](https://alerts.energy/developers/api/addresses/address-directory-street-search),
[houses](https://alerts.energy/developers/api/addresses/address-directory-streets-id-houses),
[house assignments](https://alerts.energy/developers/api/addresses/address-directory-streets-id-queues).

Street search requires settlement KATOTTG. Directories can be incomplete or
return multiple assignments: confirm a candidate, and never interpret an empty
match as no outages. Resolve the address transiently; retain confirmed operator/
group rather than raw street/house. Power setup must not change air-alert or lesson
settings. The wizard is implemented/reviewed locally, including cancellation,
replay, late responses and subscription CAS. It is not installed in the running
bot. POWER_ADDRESS_LOOKUP_ENABLED defaults false and requires power/API flags.
Staging/production address startup stays hard-blocked until shared durable quota
and bounded canary admission. Production composition passes the shared daily
budget; the process-only pacer remains available only to isolated local tests.

## Activation boundary

Credential management and the dedicated central map collector are deployed.
Actual canary plus automatic subsequent cycle each collected 27 regions and
408 operator/group records with exactly28 requests, retaining explicit stale/
unavailable status. Graph viewing reads local PostgreSQL. Publication remains
distinct from source_checked_at: every
PLANNED group/day needs a recent check, while waiting tomorrow never authorizes
reminders or invalidates checked today. Emergency suppression remains mandatory.

API is the primary map source. Central polling and shared quota passed their
separate release checks; authoritative notification admission stays disabled.
Key entry alone cannot bypass these boundaries. Cached source comparison never merges intervals;
verified fresh original-operator data wins conflict. The existing official-only
outbox rejects aggregate kinds; alerts.in.ua air-alert logic is unchanged.

Feature checkout head is 0011_power_outages. The live admin/collector overlay stays
on schema0009/ACTIVE15 and existing tables, without migration or Telegram startup.
The existing four bot/workers retain their prior stopped states. Telegram runtime
wiring and the address wizard are a separate release. No passwords/seeds changed.
See [deployment proof](../operations/POWER-MAP-20261104.md) for Engine cloning,
rollback and the unreconciled historical Compose desired-state limitation.

## Verification and publication

See [WORK-STATE](WORK-STATE.md) for exact results and evidence boundaries.
Tests use synthetic data/MockTransport and a guarded disposable local PostgreSQL.
Read-only key checking is not schedule synchronization or Telegram proof.
Windows-skipped Linux checks do not establish production readiness.

The branch inherits three local-only calendar commits. Resolve ancestor
publication scope before push/remote PR. [PR-DRAFT](PR-DRAFT.md) is local only.
