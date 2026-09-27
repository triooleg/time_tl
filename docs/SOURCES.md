# External source register

Updated: 2026-09-23. Only the user-approved alerts.in.ua service is permitted for
production alert decisions.

## Official 27-oblast IoT snapshot

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

## Polling and cache contract

Official guidance documents conditional requests and limits of approximately 8–10
requests/minute soft and 12 hard per source IP. Time TL stays below that boundary:

- exactly one global poller for all users;
- minimum 15-second interval, normally no more than 4 requests/minute;
- application limiter capped at 5/minute with burst 1;
- conditional `If-Modified-Since` only after a validated full response in the
  current adapter process;
- the first request after process restart is unconditional;
- `304` is accepted only with a validated baseline;
- `429` honors valid `Retry-After` and applies bounded backoff;
- `401/403` opens an operator circuit.

Provider HTTP redirects and environment proxy inheritance are disabled. The body is
streamed and bounded at 4096 bytes: both an oversized `Content-Length` and an
oversized actual decoded body fail closed.

Applying an accepted snapshot, per-scope baseline/outbox, provider success time,
fetch revision, `Last-Modified` and backoff reset is one repository transaction.
A non-monotonic reused revision cannot expose a newer marker over an older CLEAR
baseline; state is reset to `UNKNOWN` and the cursor remains at its last accepted
value for safe retry.

## Official location UID catalogue

- Documentation: <https://devs.alerts.in.ua/#location-uid>
- Linked sheet: <https://docs.google.com/spreadsheets/d/1XnTOzcPHd1LZUrarR1Fk43FUyl8Ae6a6M7pcwDRjNdA/edit?usp=sharing>
- Pinned CSV: `data/alerts_in_ua/locations.csv`
- Retrieval date: 2026-09-20
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

## Refresh procedure

1. Fetch the exact official source during an approved source-review task.
2. Compare schema, order, record counts, bytes and checksum.
3. Review provider terms, limits and token scope again.
4. Update artifact, tests, retrieval date and this register in one reviewed change.
5. Keep runtime capability closed on any unexplained drift.

Do not scrape the public map, substitute another provider, invent hierarchy for
UIDs absent from the pinned official order or use history endpoints for realtime
all-clear decisions.
