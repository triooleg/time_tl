# External source register

Updated: 2026-09-28. Only the user-approved alerts.in.ua service is permitted for
production alert decisions.

## Official active-alert full snapshot

- Documentation: <https://devs.alerts.in.ua/#apialerts_active>
- Official Python SDK envelope consumer:
  <https://github.com/alerts-ua/alerts-in-ua-py/blob/master/alerts_in_ua/alerts.py>
- Endpoint: `https://api.alerts.in.ua/v1/alerts/active.json`
- Authentication: bearer token stored only as `ALERTS_IN_UA_TOKEN`
- Scope: all currently active documented threats carrying explicit provider
  `location_uid` values

This is the sole reviewed live alert source for the full territory hierarchy.
The compact IoT adapters are disabled legacy/regression paths and are not live
substitutes. Every returned row is validated before alert type filtering: root
shape, documented fields, positive integer ID,
canonical decimal UID, UID presence in the byte-pinned official catalogue,
compatible catalogue/API location type, documented alert/level/threat enums,
timezone-aware timestamps and uniqueness. A known non-air row is then excluded
from Time TL air-raid decisions; an invalid or unknown row of any type invalidates
the whole snapshot. Both `red` and `yellow` `air_raid` rows are ACTIVE.

Two explicit official root variants are accepted: the documentation example
containing only `alerts`, and the official SDK envelope containing exactly
`alerts`, `meta` and `disclaimer`. In the envelope, `meta` must contain the
SDK-consumed `last_updated_at` timestamp in `%Y/%m/%d %H:%M:%S %z` format. RC8
also accepts at most eight additional metadata fields under a bounded
compatibility policy: lowercase ASCII snake-case keys of at most 64 characters,
no normalized spelling conflict with `last_updated_at`, at most three nested
container levels, 32 elements per container, 128 total extra-value nodes, 2,048
characters per string or nested object key, signed 64-bit integers, finite
floats, and at most 32,768 canonical UTF-8 bytes across all extras. Supported
values are JSON scalars, lists and objects. The `disclaimer` remains a bounded
string.

Additional metadata is digest-only: its canonical form participates in content
identity but cannot infer or alter ACTIVE or CLEAR. Duplicate JSON object keys at
the root or any nested level, JSON too deep for safe parsing, a metadata bound or
type violation, partial envelopes, unknown root fields, malformed timestamps and
invalid disclaimers invalidate the entire snapshot. These failures use bounded,
redacted diagnostics and move affected alert state to `UNKNOWN`/`STALE`; they
never synthesize `CLEAR_CONFIRMED`.

The current official API schema does not document an alert-row field named
`calculated`. The official Python SDK reads that field, but its public source
does not establish a documented wire type or safety semantics for it. This is a
source-contract discrepancy, not permission to broaden the accepted schema.
RC9 therefore does **not** accept `calculated`: any response containing it remains
`INVALID_RESPONSE` and follows the existing fail-closed `UNKNOWN`/`STALE` path.
The RC9 change is diagnostic-only and emits only a fixed opaque category for the
observed value (`null`, `boolean` or `other`); it does not treat the value as alert
state, accept the row, or expose the value itself in diagnostics.

The one authorized RC9 server preflight on 2026-09-28 received HTTP 200 from
`active.json` but failed closed as `alert_fields_unknown / count=1`; the unknown
field was not `calculated`. Raw-name and hash-based field discovery were rejected
by independent safety review because provider-controlled information could be
disclosed or made re-identifiable. At that point RC9 remained the deployed
server version; it did not accept that response as a snapshot or advance alert
or all-clear state. The later RC10 server evidence is recorded below.

The user then explicitly directed that an undocumented alert-row field must not
be used. The independently reviewed RC10 local compatibility candidate therefore
does not identify, interpret or retain it. A snapshot may contain at most one
undocumented key per alert row, and every row that contains an undocumented key
must use the same key. Its value must satisfy the existing bounded JSON structure
rules and a 32,768-byte per-row serialized bound. The undocumented key and value
are discarded before content digest calculation and cannot enter alert episodes,
state, diagnostics, logs or persistence.

This tolerance does not weaken the documented contract: required and optional
documented fields, enums, UIDs, timestamps, threats and row uniqueness remain
strictly validated. More than one undocumented key, different undocumented keys
across rows, or an invalid or oversized undocumented value rejects the complete
snapshot. Parent/child ACTIVE applicability and the two-successful-observation
requirement for `CLEAR_CONFIRMED` are unchanged.

The exact RC10 release was later installed and subjected to one authorized
preflight. Telegram `getMe` passed and one `active.json` request returned HTTP
200, but the payload was rejected as
`location_oblast_uid_format_invalid / count=1`. The HTTP response was not an
accepted provider snapshot and did not advance alert or all-clear state. RC10
was not activated; the bot and workers remain stopped.

The RC11 diagnostic candidate kept this predicate fail closed and reports
only a fixed invalid-value shape category: `boolean`, `integer`,
`noncanonical_string` or `other`. It does not emit the rejected value, does not
emit the ignored undocumented key or value, and does not change catalogue
validation for a canonical UID. A rejected response still cannot establish or
advance a validated baseline. Its one authorized server preflight received one
HTTP 200 response from `active.json` and classified the rejected optional field
as `location_oblast_uid_integer / count=1`. That response was not accepted as a
snapshot; revision 10 was not activated and delivery-capable services remained
stopped.

RC12 historically treated this optional value as an authoritative top-level
ancestor assertion. Its exact release was installed, but the single authorized
preflight at `11:17:43Z` failed closed as
`location_oblast_uid_unknown / count=1`. The response was not accepted as a
snapshot, revision 10 was not activated, and bot/workers remained stopped.

The RC13 contract correction does not use `location_oblast_uid` as
authority. When present, it is accepted only as a dedicated bounded optional
input and then discarded. It is separate from the one permitted generic bounded
undocumented alert-row field, so its presence does not consume that generic
compatibility slot. Neither its key nor value participates in the canonical
digest or enters alert state, persistence, diagnostics or logs.

The required `location_uid`, its pinned catalogue membership and its compatible
pinned location type remain the sole location authority for each row. Ignoring
the optional oblast hint cannot repair a missing, malformed, unknown or
type-incompatible required UID. Complete-row validation, bounded generic-extra
handling, parent/child applicability, freshness, restart behavior and the two-
successful-observation requirement for `CLEAR_CONFIRMED` remain unchanged.
RC13 passed local code review and tests, then was packaged and installed under
the exact identity recorded in the release-evidence document. Its one authorized
STAGING preflight made one `active.json` request and failed closed as
`location_uid_unknown / count=1`. It did not accept a snapshot, activate revision
10, start the bot or workers, or send a message.

The alert schema documents required `location_uid` as a string carrying an int32
value. The official SDK provides no alternate UID-to-hierarchy mapping. A
2026-09-28 refresh of the documentation-linked official CSV was byte-identical to
the pinned catalogue: 142,029 bytes, SHA-256
`a5bf1d312ee7258b16caaaf6c06e3df56fca68b0c14a060ec7c5ee7ce40b9620`,
1,622 records, 27 roots and 1,595 parent links. Consequently the RC13 preflight
result is an upstream API/catalogue inconsistency, not evidence supporting an
invented local hierarchy mapping. At RC14 review time, general UID compatibility
expansion or mapping was `BLOCKED` until the provider published an updated full
catalogue with UID, type and parent data, corrected the API response, or supplied
an official mapping. The later RC15 amendment below is a user-approved narrow
alias rule and is not an official catalogue row. The raw rejected UID and
response body are not recorded.

The authoritative rule is unchanged: a required `location_uid` is usable only
when it exists in the official pinned catalogue and its API location type is
compatible with the catalogue region type. An unknown, mismatched or malformed
UID is not used, mapped, inferred or partially ignored. A single invalid row
invalidates the complete snapshot globally, including any otherwise known rows,
and produces UNKNOWN or STALE rather than CLEAR. Recovery still requires a
complete valid snapshot and the normal two-observation all-clear rule. RC14 adds
adversarial regression evidence for this existing contract; it does not add a
new UID source or compatibility rule. Its subsequently authorized single
STAGING preflight again failed closed as `location_uid_unknown / count=1`. The
unknown UID was not used, mapped or inferred, no snapshot was accepted, and
revision 10 was not activated. This is HOLD evidence, not source compatibility
or launch evidence.

The user-approved RC15 compatibility amendment is a bounded live alias rule,
not a new catalogue source. The byte-pinned official table remains the sole
canonical UID, Ukrainian name, location type and hierarchy. A live alias is
eligible only when all of the following hold:

- `location_type` is exactly `city`;
- the live `location_uid` is canonical decimal, greater than 5000;
- subtracting exactly 5000 produces a UID present in the pinned catalogue as
  `Громада`;
- the normalized Ukrainian provider title exactly matches the unique official
  `м. X` city prefix for that pinned record.

No fuzzy matching and no other numeric offset are permitted. A matched row is
canonicalized to the base UID and official pinned name, type and hierarchy. The
provider alias UID and provider identity fields are excluded from digest, state,
persistence, messages and logs. Under RC15, alias/canonical collision, title
ambiguity or mismatch, wrong type, missing base or malformed input invalidated
the complete snapshot and produced UNKNOWN or STALE, never CLEAR. RC16's bounded
collision amendment is recorded below.

The observed exact mappings are `5349 -> 349` (Марганець), `5351 -> 351`
(Нікополь), `5564 -> 564` (Запоріжжя) and `6293 -> 1293` (Харків). These aliases
do not become persisted or selectable identities; their official base records
remain canonical. A changed catalogue hash or a different alias shape requires
review before acceptance. RC15 was subsequently packaged and installed. Its one
authorized STAGING preflight made one `active.json` request and failed closed as
`location_uid_alias_collision / count=1`; revision 10 remained unactivated and
bot/workers remained stopped.

A separate read-only diagnostic examined one HTTP 200 snapshot containing 56
rows. Three official bases appeared in both direct and `+5000` alias forms:
`349/5349`, `351/5351` and `564/5564`. Each base had exactly one direct and one
alias row. Each pair shared location type and start time and had null finish time,
but its numeric alert IDs and update timestamps differed. No state was changed,
and those similarities are not authoritative proof that the provider episodes
are semantically identical.

RC16 consequently does not deduplicate the pair. Independently validated direct
and alias rows with distinct unique numeric alert IDs coexist as separate alert
episodes on the same official base location identity. If one disappears while
the other remains, the location remains ACTIVE. Only absence of both episodes
can begin the existing two-successful-observation path to `CLEAR_CONFIRMED`.

The canonical digest contains all accepted canonicalized rows sorted by unique
numeric alert ID, while raw alias/direct source identity fields remain excluded.
A duplicate provider alert ID, invalid alias, invalid direct row or any other
invalid row rejects the complete snapshot and yields UNKNOWN or STALE, never
CLEAR. RC16 was subsequently packaged and installed. Its one authorized STAGING
preflight made one `active.json` request and failed closed as
`location_type_catalogue_mismatch / count=1`; revision 10 remained unactivated
and bot/workers remained stopped.

A one-request read-only type diagnostic received HTTP 200 with 59 rows. Its sole
relevant direct row carried canonical UID `31`, title `м. Київ` and provider type
`oblast`; there was no alias row. The exact pinned UID/name record is a root
`Місто з спеціальним статусом` with no parent. The diagnostic changed no state.

RC17 permits only this bounded direct-row type compatibility: a pinned
`Місто з спеціальним статусом` accepts provider type `city` or the observed
`oblast` only when the canonical UID and normalized Ukrainian title exactly
equal the pinned official UID and name. The official pinned identity, type and
root hierarchy are used downstream. Ordinary `Область` validation is unchanged.
An unknown UID is never matched by title, mapped or inferred. Any other type,
UID, title or catalogue drift invalidates the complete snapshot and yields
UNKNOWN or STALE, never CLEAR. RC17 currently has LOCAL evidence only; exact
package, deployment, one preflight and independent live-result review remain
release gates.

A validated empty `alerts` list is a complete observation. It does not bypass the
state machine: an existing ACTIVE scope still needs two consecutive successful
observations without an applicable episode before `CLEAR_CONFIRMED`. A valid
`304` counts only when the same adapter process first established a validated
complete `200` baseline. Unknown UIDs, schema drift, stale or reused cache
markers with changed content, and any source failure yield UNKNOWN/STALE, never
clear.

## Legacy aggregate 27-oblast IoT snapshot

- Documentation: <https://devs.alerts.in.ua/#v1iotactive_air_raid_alerts_by_oblastjson>
- Endpoint: `https://api.alerts.in.ua/v1/iot/active_air_raid_alerts_by_oblast.json`
- Authentication: bearer token stored only as `ALERTS_IN_UA_TOKEN`
- Scope: aggregate air-raid status for 27 top-level entries in the provider's fixed
  order, including Crimea, Kyiv city and Sevastopol

The payload must be a JSON string of exactly 27 characters:

- `A`: whole-oblast air raid is ACTIVE;
- `P`: a raion/community inside the oblast is ACTIVE; Time TL conservatively treats
  the whole selected oblast as ACTIVE;
- `N`: no active air-raid indication for that oblast in this complete snapshot.

An unknown character, wrong length/type, oversized body, missing validated cache
baseline, partial response, timeout or HTTP failure is source failure. It is never
interpreted as `N` or `CLEAR_CONFIRMED`.

The adapter uses stable synthetic per-oblast episode IDs. A transition between `P`
and `A` does not generate a second alert-start. The provider is deliberately
oblast-only; it does not claim a community-level all-clear.

The all-territory IoT endpoint remains disabled for live wiring because its
current positions include UIDs absent from the official location catalogue. It is
retained only for regression evidence and is not a production substitute for
`active.json`.

## Polling and cache contract

Official guidance documents conditional requests and limits of approximately 8–10
requests/minute soft and 12 hard per source IP. Time TL stays below that boundary:

- exactly one global poller for all users;
- minimum 15-second interval, normally no more than 4 requests/minute;
- live full-territory application limiter capped at 4/minute with burst 1;
- conditional `If-Modified-Since` only after a validated full response in the
  current adapter process;
- the first request after process restart is unconditional;
- `304` is accepted only with a validated baseline;
- `429` honors valid `Retry-After` and applies bounded backoff;
- `401/403` opens an operator circuit.

Provider HTTP redirects and environment proxy inheritance are disabled. The body is
streamed and bounded: `active.json` has a dedicated 1 MiB maximum; both an
oversized `Content-Length` and oversized actual body fail closed.

Applying an accepted snapshot, per-scope baseline/outbox, provider success time,
fetch revision, `Last-Modified` and backoff reset is one repository transaction.
A non-monotonic reused revision cannot expose a newer marker over an older CLEAR
baseline; state is reset to `UNKNOWN` and the cursor remains at its last accepted
value for safe retry.

## Official location UID catalogue

- Documentation: <https://devs.alerts.in.ua/#location-uid>
- Linked sheet: <https://docs.google.com/spreadsheets/d/1XnTOzcPHd1LZUrarR1Fk43FUyl8Ae6a6M7pcwDRjNdA/edit?usp=sharing>
- Pinned CSV: `data/alerts_in_ua/locations.csv`
- Pinned artifact retrieval date: 2026-09-20
- Documentation-linked source rechecked: 2026-09-28; byte-identical
- Bytes: 142029
- SHA256: `a5bf1d312ee7258b16caaaf6c06e3df56fca68b0c14a060ec7c5ee7ce40b9620`

The pinned file contains 1,622 unique UIDs: 27 roots, 126 districts and
1,469 communities. Its published order is canonical: districts belong to the
active oblast block and communities to the active district block. The reviewed
initial root run `31,30,8,29` is explicit: Kyiv, Sevastopol and Crimea are
childless roots in this export; the following UID 38 district belongs to Volyn
UID 8. Any different multi-root run fails closed. The resulting tree has exactly
1,595 parent links and no orphan rows.

KATOTTG validates the provider relationship and may add an external code, but it
cannot reparent an alerts.in.ua row. A mismatch remains unresolved and hidden,
as before. UIDs absent from this byte-pinned provider list, including IoT
positions 1805..1814, remain unresolved. A non-space value at such a position
invalidates the complete snapshot and produces UNKNOWN/STALE, never
CLEAR_CONFIRMED.

Only UIDs present in this byte-pinned alerts.in.ua file may be persisted as
territories or exposed in the Telegram selection tree. KATOTTG-only terms are
validation/enrichment evidence and never become selectable territory rows.

Activation and every active Telegram read require the exact pinned UID set; an
extra or missing UID aborts before the active pointer can be used for display.

`region_registry_full.WIRE_RUNTIME` remains false for the all-territory IoT wire.
Live full-hierarchy workers use only the separately reviewed
`ACTIVE_JSON_WIRE_RUNTIME` capability and `/v1/alerts/active.json`;
if it is false, startup aborts before owner settings, database, provider, lease,
health or polling resources are created. LOCAL and SIMULATION keep their offline
provider behavior.

## Refresh procedure

1. Fetch the exact official source during an approved source-review task.
2. Compare schema, order, record counts, bytes and checksum.
3. Review provider terms, limits and token scope again.
4. Update artifact, tests, retrieval date and this register in one reviewed change.
5. Keep runtime capability closed on any unexplained drift.

Do not scrape the public map, substitute another provider, invent hierarchy for
UIDs absent from the pinned official order or use history endpoints for realtime
all-clear decisions.
