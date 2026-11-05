# Central Alerts Energy map cache

User instruction and deployment authorization: 2026-11-04. Air alerts remain
exclusively alerts.in.ua; this collector concerns electricity schedules only.

## Source and cadence

The authenticated overview returned **27 regions** on 2026-11-04. The documented
[overview](https://alerts.energy/developers/api/operators/source-registry-areas-overview)
and [regional schedules](https://alerts.energy/developers/api/operators/source-registry-areas-slug-shutdowns)
require **28 HTTP requests per country cycle**: one overview plus one per region,
including all returned operators and groups. No extra supplier/address calls are
made by the map collector. A regional cache can contain unsupported operator IDs
without guessing their internal provider enum or notification authority.

Every 300 seconds means 288 cycles/day and 8064 calls/day for current coverage.
The [account quota](https://alerts.energy/developers/api/authentication) is shared
across keys: 5 requests/second and 10,000/day, reset at Kyiv midnight. We admit at
most 4 requests/second under a PostgreSQL row lock. National estimated base load
must fit within 9000/day even on a 25-hour Kyiv DST day (300 cycles), reserving
headroom. Current coverage uses at most 8400 calls on that day. External clients using the same
account are outside the local counter; provider 429 remains authoritative.

## Stored data and consumers

Existing `runtime_settings`, without a migration:

- `power_map:state`: singleton cycle ticket, credential revision, next due time,
  expected region scope, completion/error metadata.
- `power_map:region:{slug}`: validated public region/group/day wire graph, content
  hash, semantic revision, source publication/check and fetch times.
- `power_api:account_budget`: shared daily attempts, next admitted slot and
  provider cooldown. Key replacement does not reset it. Failed attempts count.

One dedicated advisory lock plus expiring cycle ticket prevents parallel polling.
Next due time is committed before the first HTTP call. A complete cycle is bounded
to 240 seconds, with credential/lease fencing before requests and commit. A crash
leaves its ticket reserved until bounded expiry; it cannot trigger a request burst.
No retry loop follows 401/403/429 within a cycle. Failed/removed/mismatched regions
retain historical payloads without exposing them as fresh under a changed mapping.

Content versions ignore observation/publication timestamp churn while retaining
dates, operators, groups, hours, statuses and effective emergency restrictions.
Dates use Europe/Kyiv. A fresh check of an old publication can still be current;
a fresh download of an old check is stale. Empty/waiting means no published plan.
Emergency/cancelled plans are suppressed. A published plan never proves supply.

`PowerMapScheduleReader` reads only PostgreSQL for known Dnipro DTEK/CEK mappings;
the generic map preserves all other groups for explicit mapping later. Other
operators cannot become guessed Telegram bindings. Existing official-only
notification/outbox admission stays unchanged; this collector sends no messages.

## Operations

Role: `python -B -m time_tl.runtime.power_map_worker`, default disabled. Required
environment is only `POWER_MAP_COLLECTOR_ENABLED=true`, existing `DATABASE_URL`,
existing `OWNER_ALERTS_FERNET_KEY`; the API key is decrypted from its protected DB
record in memory. No Telegram tokens, Docker socket, host ports or raw address
files belong in this container. `--once` performs a due fenced cycle; `--health`
reads local collector heartbeat only. A healthy process is not proof every graph
is available. Never repeat a one-shot to work around a pending cycle or cooldown.

Owner admin routes `/power/map`, `/api/power/map` and
`/api/power/map/regions/{slug}` read only the local DB. Owner access check is still
explicit and quota-consuming; merely viewing the map never calls the API.

Deployment proof is recorded separately in operations notes. LOCAL tests and a
synthetic map do not establish live API compatibility or Telegram readiness.
