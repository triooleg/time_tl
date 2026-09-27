# ADR-001: Provider-namespaced territory hierarchy

Status: **ACCEPTED / ACTIVATION REVIEW**
Reviewed: 2026-09-27

## Context

Time TL must let a Telegram user select an oblast, then a raion, then a
hromada.  Alert start and confirmed all-clear must include applicable ancestor
alerts without producing duplicate user notifications.

The approved production provider is `alerts.in.ua`.  Its official API exposes:

- active alerts with `location_uid` and `location_type`;
- a compact all-UID air-raid snapshot;
- an officially linked catalogue of UID, Ukrainian name and location type.

The alerts.in.ua catalogue is flat and has no `parent_uid`. Row order is not an
authoritative relationship and is not used to infer safety decisions. The
official KATOTTG 4.0.0 glossary published by the State Statistics Service has
explicit `parent`, category and stable KATOTTG identifiers. It is therefore the
relationship source; alerts.in.ua remains the sole alert-state provider and its
UID remains the runtime alert identity.

The official UkraineAlarm v3 API documents a recursive region catalogue with
`regionId`, `regionName`, `regionType` and `regionChildIds`, including
`State`, `District` and `Community`.  It requires a separate application and
key, has not been approved for Time TL production, and its IDs are not
interchangeable with `alerts.in.ua` UIDs.

## Decision

1. Keep `alerts.in.ua` as the only production alert provider until an explicit
   provider-change decision is approved.
2. Keep existing oblast-only subscriptions working during the staged migration.
3. Reconcile exact normalized names only within the expected type and parent;
   never use fuzzy matching or spreadsheet row order.
4. Never join two sources by a bare UID. A territory identity
   is `(provider, uid)`.
5. Accept a hierarchy only from the official machine-readable KATOTTG artefact
   with an explicit parent relationship and recorded source checksums.
6. Any missing UID, missing parent, partial catalogue, stale catalogue or
   catalogue revision mismatch is `UNKNOWN`/`STALE`, never all-clear.
7. A scope or catalogue change invalidates the previous clear baseline and
   requires fresh reconciliation.
8. Ambiguous matches are stored as non-selectable when their explicit parent is
   provable, or omitted and reported when it is not. They are never assigned by
   position. The current deterministic 4.0.0 build persists all 1,622 provider
   UIDs: 1,620 selectable and 2 hidden/unresolved (`1180`, `1181`).
9. Names beginning with `м.` may be disambiguated only by an exact official
   KATOTTG descendant of category `M` (city) under the candidate hromada.

## Compatible persistence plan

The existing `users.confirmed_region_id`, `users.region_name`,
`alert_scopes.selected_region_id` and `alert_scopes.applicable_region_ids`
remain valid for oblast-only users.

The normalized cached catalogue is versioned and atomically activated:

```text
territory_catalog_versions
territories
active_territory_catalogs
  provider
  uid
  name_uk
  kind
  parent_uid
  catalog_revision
  enabled
  PRIMARY KEY (provider, uid)
```

Add `provider` and `catalog_revision` to an alert scope.  Keep
`applicable_region_ids` as the persisted, audited closure for restart safety.
Do not add duplicate oblast/raion/hromada columns to `users`; the selected leaf
and the catalogue closure already model the hierarchy.

## Effective-state and deduplication contract

The official alerts.in.ua documentation defines the compact
`/v1/iot/active_air_raid_alerts.json` response for all oblasts, raions and
hromadas as `A` (active alert for the UID), `P` (partial alert within a raion
or hromada), and `N` (no alert). For a selected leaf and its stored oblast ->
raion -> hromada closure:

- `A` on the selected territory or any applicable ancestor makes the selected
  scope active;
- `P` on an ancestor means that only some child territory is active and does
  not by itself activate the selected hromada;
- `P` on the exact selected UID is handled conservatively as active;
- a parent transition between whole and partial alert must not create another
  alert-start notification while the effective scope remains active;
- all-clear is emitted only when every applicable active episode has ended and
  the existing second-confirmation rule succeeds on complete fresh snapshots;
- the outbox dedupe identity is based on the user scope and effective state
  transition, not on each matching oblast/raion/hromada record.

## Current catalogue evidence

The deterministic local catalogue build has been verified against the source
identified as `SSSU:CL_KATOTTG(4.0.0)`, with SHA-256
`ea1d3ba0f1642a6e987d974d917478183dfc1215edfea4f7dfe8fef47af73fbe`.
It persists 1,622 alerts.in.ua UIDs, serves 1,620 as selectable through the
reconciled database catalogue, and keeps UIDs `1180` and `1181` hidden and
unresolved. These figures supersede the earlier 1,331 persisted / 1,327
selectable / 295 unresolved dry-run.

The raw alerts.in.ua registry remains flat and is not itself a hierarchy. The
oblast -> raion -> hromada relationships exposed at runtime come from the
reconciled, versioned database catalogue built with the explicit KATOTTG parent
relationships. This local build evidence does not by itself prove PostgreSQL
migration compatibility, staging behaviour, or production readiness.

## Activation evidence

Before production activation, retain the reproducible reconciliation report
and exact source checksum above, run the migrations and catalogue import
against an isolated real PostgreSQL instance, independently review the result,
atomically activate it, restart the bot and alert worker, and run a staging
canary. UkraineAlarm remains unapproved and is not an automatic fallback.

## Gate status

- G0 SOURCE, oblast-only `alerts.in.ua`: **PASS**.
- G0 SOURCE, KATOTTG hierarchy: **REVIEW** (local deterministic build verified;
  UIDs `1180` and `1181` remain hidden/unresolved).
- Database-served reconciled catalogue: **REVIEW** pending isolated real
  PostgreSQL migration/import evidence and a staging canary.
- G3 ALERT SAFETY for hierarchy: **REVIEW** pending the isolated real
  PostgreSQL evidence and staging canary; no production or G3 `PASS` claim is
  made by the catalogue build.
- Provider change to UkraineAlarm: **REVIEW REQUIRED**.

## Official references

- https://devs.alerts.in.ua/
- https://docs.google.com/spreadsheets/d/1XnTOzcPHd1LZUrarR1Fk43FUyl8Ae6a6M7pcwDRjNdA/edit?usp=sharing
- https://stat.gov.ua/uk/page-contents/kodyfikator-administratyvno-terytorialnykh-odynyts-ta-terytoriy-terytorialnykh-0
- https://stat.gov.ua/sdmx/workspaces/default:integration/registry/sdmx-plus/structure/glossary/SSSU/CL_KATOTTG/4.0.0?references=children&detail=full
- https://api.ukrainealarm.com/
- https://github.com/UkraineAlarm/UkraineAlarm-javascript/blob/master/docs/RegionsApi.md
- https://github.com/UkraineAlarm/UkraineAlarm-javascript/blob/master/docs/RegionViewModel.md
- https://github.com/UkraineAlarm/UkraineAlarm-javascript/blob/master/docs/V2RegionType.md
- https://github.com/UkraineAlarm/UkraineAlarm-javascript/blob/master/docs/AlertsApi.md
- https://www.ukrainealarm.com/faq
