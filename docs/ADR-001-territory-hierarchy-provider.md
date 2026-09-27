# ADR-001: alerts.in.ua territory hierarchy and live alert source

Status: **ACCEPTED / STAGING ACTIVATION REVIEW**
Reviewed: 2026-09-27

## Context

Time TL lets a Telegram user select an oblast, then a raion, then a hromada.
Alert start and confirmed all-clear must include alerts applicable through that
hierarchy without producing duplicate user notifications.

The user-approved production provider is `alerts.in.ua`. Its official sources
used by Time TL are:

- the documented `/v1/alerts/active.json` full active-alert snapshot, whose
  records carry `location_uid`, location type, alert type and alert level;
- the officially linked location catalogue containing provider UID, Ukrainian
  name and location type in published source order.

The pinned alerts.in.ua catalogue has no explicit `parent_uid` column, but its
published row order is the approved canonical relationship evidence. Districts
belong to the active oblast block and communities belong to the active district
block. The source is accepted only with the exact byte checksum, record counts,
types and reviewed ordering invariants described below.

The official KATOTTG glossary is useful independent evidence. It is not the
canonical alerts.in.ua hierarchy and must not reparent a provider row.

The official UkraineAlarm API has a separate recursive region catalogue and
separate identifiers. It has not been approved for Time TL production and is
not an automatic fallback.

## Decision

1. `alerts.in.ua` remains the only production alert provider unless a separate
   provider-change decision is approved.
2. The byte-pinned official alerts.in.ua location catalogue is canonical for
   every runtime UID, Ukrainian name, location type, source order and parent
   link. Only UIDs in that catalogue may be persisted or displayed.
3. The exact reviewed initial root run is `31,30,8,29`: Kyiv, Sevastopol and
   Crimea are childless roots in this export, while the district block beginning
   with UID `38` belongs to Volyn UID `8`. Any other multi-root run, changed
   prologue, checksum drift, orphan or type/order anomaly fails closed.
4. KATOTTG validates and enriches a provider row and may supply its external
   KATOTTG code. It cannot create a selectable provider UID or change an
   alerts.in.ua parent link. Missing, ambiguous or disagreeing enrichment stays
   unresolved and non-selectable.
5. Never join two sources by a bare UID. Runtime identity is
   `(provider, uid)`; KATOTTG codes are external enrichment only.
6. Existing oblast-only subscriptions remain compatible during staged
   activation.
7. A missing UID, unknown UID, missing parent, partial catalogue, stale
   catalogue or catalogue revision mismatch is `UNKNOWN`/`STALE`, never
   all-clear.
8. A scope or catalogue change invalidates the previous clear baseline and
   requires fresh reconciliation.
9. Exact, versioned spelling/rename aliases may validate a known provider row
   only when UID, provider label, KATOTTG parent and KATOTTG code/name all match.
   Fuzzy matching is prohibited.

## Canonical catalogue contract

The pinned provider file is `data/alerts_in_ua/locations.csv`, SHA-256
`a5bf1d312ee7258b16caaaf6c06e3df56fca68b0c14a060ec7c5ee7ce40b9620`.
It contains 1,622 unique UIDs: 27 roots, 126 raions and 1,469 hromadas. The
validated tree has exactly 1,595 parent links and no orphan rows.

The deterministic KATOTTG 4.0.0 enrichment input has SHA-256
`ea1d3ba0f1642a6e987d974d917478183dfc1215edfea4f7dfe8fef47af73fbe`.
It validates/enriches 1,620 selectable rows; UIDs `1180` and `1181` remain
hidden and unresolved. These KATOTTG results do not alter the canonical
alerts.in.ua UID set or parent relationships.

The all-territory compact-IoT wire remains disabled because current response
positions `1805` through `1814` are absent from the byte-pinned official
catalogue. Those positions are not invented, persisted or displayed. A
non-space value at an unknown position invalidates that compact snapshot and
produces `UNKNOWN`/`STALE`.

## Compatible persistence and scope

The existing `users.confirmed_region_id`, `users.region_name`,
`alert_scopes.selected_region_id` and `alert_scopes.applicable_region_ids`
remain valid for oblast-only users. The normalized catalogue remains versioned
and atomically activated:

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

The current scope closure contains the selected territory's ancestors followed
by its own complete subtree:

- hromada: its oblast, raion and itself;
- raion: its oblast, itself and all its hromadas;
- oblast: itself and every descendant raion and hromada.

Sibling branches are excluded for hromada and raion selections. Catalogue
activation recomputes persisted closures transactionally, invalidates the
provider poll fence, removes old episodes and resets snapshots to `UNKNOWN`
before delivery may resume. Duplicate oblast/raion/hromada columns are not
added to `users`; selected UID plus audited closure model the hierarchy.

## Live alert and all-clear contract

`https://api.alerts.in.ua/v1/alerts/active.json` is the sole live alert source
for the full territory hierarchy. Every returned row is validated before any
non-air row is filtered. The complete snapshot is invalid if any row has an
unknown provider UID, undocumented or incompatible fields/enums, incompatible
location type, duplicate ID, invalid timestamp or otherwise fails validation.

Both documented `red` and `yellow` `air_raid` records are active. An alert
episode applies when its UID is in the selected scope's canonical
ancestor-plus-subtree closure. Multiple applicable rows produce one effective
scope transition and one deduplicated notification, not one notification per
oblast, raion and hromada row.

All-clear is emitted only after an active scope is absent from two consecutive
successful observations. An observation is either a validated complete `200`
snapshot or a valid `304` backed by a complete `200` baseline parsed by the
same adapter process. Source failure, invalid/partial/empty data, unknown UID,
stale coverage or non-monotonic/reused cache metadata yields `UNKNOWN`/`STALE`
and never confirms clear.

`Last-Modified` is validated and committed atomically with the accepted
snapshot state. `If-Modified-Since` is sent only after this adapter process has
validated a complete `200`. After restart the first request is unconditional;
the persisted marker is only a monotonic floor, not a trusted `304` baseline.
Restart invalidates the prior clear baseline and begins as `UNKNOWN`, or
`STALE` when active episodes must be retained conservatively.

Historical note: earlier designs used KATOTTG as the relationship authority
and the compact all-territory IoT response as the live hierarchy feed. Those
parts of the design are superseded by this decision. The compact adapters stay
only as disabled regression/legacy code and are not permitted as a production
substitute for `active.json`.

## Gate status

- Source/catalogue contract and implementation review: **PASS** for the current
  code-reviewed candidate.
- Alert safety implementation review: **PASS** at code/test-review level only.
- STAGING token preflight and exact-version deployment: **PENDING**.
- STAGING canary with approved accounts, monitoring and rollback evidence:
  **PENDING**.
- PRODUCTION/public launch: **NOT APPROVED**. Local tests and code review do not
  constitute staging or production evidence.

## Official references

- https://devs.alerts.in.ua/
- https://devs.alerts.in.ua/#apialerts_active
- https://docs.google.com/spreadsheets/d/1XnTOzcPHd1LZUrarR1Fk43FUyl8Ae6a6M7pcwDRjNdA/edit?usp=sharing
- https://stat.gov.ua/uk/page-contents/kodyfikator-administratyvno-terytorialnykh-odynyts-ta-terytoriy-terytorialnykh-0
- https://stat.gov.ua/sdmx/workspaces/default:integration/registry/sdmx-plus/structure/glossary/SSSU/CL_KATOTTG/4.0.0?references=children&detail=full
- https://api.ukrainealarm.com/
