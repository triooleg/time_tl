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

The pinned file contains 1,622 unique UIDs: 27 top-level entries, 126 districts and
1,469 communities. Its columns do not contain explicit parent UIDs. Row order and
names are not a safe hierarchy, so the flat CSV is not used to propagate district/
community all-clear.

The current runtime exposes only the reviewed top-level oblast list for manual user
selection. `reviewed_region_resolver` remains false: coordinates are rejected and
deleted/not stored rather than guessed. A future geolocation implementation must use
an independently reviewed official polygon or hierarchy source and preserve that
privacy boundary.

## Refresh procedure

1. Fetch the exact official source during an approved source-review task.
2. Compare schema, order, record counts, bytes and checksum.
3. Review provider terms, limits and token scope again.
4. Update artifact, tests, retrieval date and this register in one reviewed change.
5. Keep runtime capability closed on any unexplained drift.

Do not scrape the public map, substitute another provider, infer hierarchy from
names/order or use history endpoints for realtime all-clear decisions.
