# Time TL architecture

## Components

One application image exposes separate commands:

- `bot`: Telegram long polling, commands, inline menus, and FSM.
- `alert-worker`: official or fake provider polling, validation, reconciliation, and state transitions.
- `schedule-worker`: lesson-boundary events and active schedule evaluation.
- `sender-worker`: transactional outbox delivery with retry and backoff.
- `postgres`: durable source of users, schedule profiles, provider state, episodes, and delivery evidence.

Redis and Celery are intentionally omitted from v1. PostgreSQL advisory locks, `FOR UPDATE SKIP LOCKED`, and a transactional outbox are sufficient for single-server operation.



## Alerts.in.ua polling and load control

Only one singleton `alert-worker` contacts `https://api.alerts.in.ua`. User count never changes provider request count.

- Endpoint: `GET /v1/alerts/active.json` for the complete active-alert list.
- Authentication: server-side `Authorization: Bearer`; token never reaches Telegram clients.
- Normal cadence: one non-overlapping request every 15 seconds, or 4 requests per minute.
- Local limiter: minimum 15-second spacing and a hard token-bucket ceiling of 5 requests per minute with no catch-up burst after downtime.
- Cache: persist `Last-Modified`; send `If-Modified-Since`; a successful `304` confirms that the cached snapshot is unchanged and refreshes source freshness.
- Singleton: PostgreSQL advisory lock and lease prevent two replicas from polling simultaneously.
- Startup: randomized 0-to-15-second delay avoids restart bursts.
- `429`: honor `Retry-After`, otherwise back off 60, 120, 240 seconds up to 10 minutes.
- `401/403`: open the circuit and require operator action; never retry-loop.
- network, timeout, `5xx`, invalid JSON, partial schema, or oversized body: mark source `STALE`, keep the last known active alerts, and send no all-clear.
- History endpoints are not used for real-time polling; their documented limit is only two requests per minute.

A successful validated `200` is treated as a complete snapshot. Active episodes are keyed by provider alert `id`, location UID, and alert type. Disappearance creates a candidate all-clear; notification is allowed only after the next successful `304` or complete `200` still confirms the episode absent. This adds one polling interval of safety and prevents a single malformed or transient response from clearing an alert.

After snapshot persistence, affected users are selected from PostgreSQL by region UID and notified through the local outbox. No Telegram command or user session calls the provider.

## Operating modes

| Mode | Lesson reminders | Alert start and all-clear | Return-to-lesson prompt |
|---|---:|---:|---:|
| `LESSONS_ONLY` | Yes, from the active profile | No | No |
| `LESSONS_AND_ALERTS` | Yes, from the active profile | Yes | Yes, after confirmed all-clear from validated complete provider snapshots |
| `ALERTS_ONLY` | No | Yes | No |

Only the active schedule profile can produce lesson reminders or join prompts. Alert-only mode does not require a schedule and does not consult saved inactive profiles. Switching mode or active profile is an audited, confirmed user action. Reminder lead time is a per-user setting.

## Alert state machine

```text
UNKNOWN -> ACTIVE          explicit official active state
ACTIVE  -> CLEAR_CONFIRMED confirmed all-clear from validated complete provider snapshots, fresh and hierarchy-safe
ACTIVE  -> STALE           freshness threshold exceeded or source unavailable
STALE   -> ACTIVE          explicit official active state
STALE   -> CLEAR_CONFIRMED explicit fresh confirmed provider all-clear after reconciliation
```

There is no timeout, empty-response, restart, cache-expiry, or exception path to `CLEAR_CONFIRMED`.

For a selected community, effective alert state includes applicable community, district, and region ancestors. One child all-clear cannot clear an active ancestor event.

## Schedule profiles and resolution

Each user may own zero to three named schedule profiles. A partial unique constraint enforces at most one active profile per user. Import creates an inactive profile; activation is a separate confirmed action.

Recurring rules contain weekday, week parity (`ANY/ODD/EVEN`), date parity (`ANY/ODD/EVEN`), validity range, and segments. Resolution for a local date uses:

1. exact-date override;
2. combined week and date parity;
3. date parity;
4. ISO week parity;
5. base weekday.

Equal-specificity conflicting rules are rejected. Times are authored as local `Europe/Kyiv` wall time and materialized occurrences use timezone-aware timestamps.

## Sharing contract

A share is a versioned immutable snapshot of one profile, not a live reference.

Delivery forms:

- Telegram deep link containing an opaque unguessable share token;
- short code resolved server-side;
- portable `.time-tl.json` document with schema version and integrity metadata.

Import pipeline:

1. Decode and validate schema, size, expiry, revocation, and integrity.
2. Reject unknown major schema versions.
3. Remove or reject prohibited fields.
4. Resolve and show a 14-day preview.
5. Ask for explicit confirmation.
6. Create a new inactive independent profile.
7. If the three-profile limit is reached, ask the user which profile to replace or cancel.
8. Activation remains a separate confirmed action.

Default exports include only profile name, timezone contract, rules, segments, date overrides, and non-sensitive lesson labels. They exclude Telegram identifiers, region, coordinates, notification settings, author private data, secrets, and lesson join links. Join links require a separate opt-in warning and must never appear in public logs or share analytics.

Suggested persistence: `schedule_shares(id, owner_user_id, profile_id, token_hash, schema_version, expires_at, revoked_at, include_join_links, snapshot, created_at)`. Store only a token hash. Rate-limit creation and redemption.

## Persistence

Core relations:

- `users`
- `regions`, `region_closure`, `user_regions`
- `notification_settings`
- `schedule_profiles`
- `schedule_rules`, `schedule_segments`, `date_overrides`
- `schedule_shares`, `schedule_imports`
- `alert_states`, `alert_episodes`
- `notification_outbox`, `delivery_attempts`
- `processed_telegram_updates`

An outbox idempotency key includes user, official episode or transition, active profile occurrence, and lesson context. Provider updates and outbox creation occur in one transaction.

## Location flow

1. Receive Telegram coordinates transiently.
2. Run local point-in-polygon against a separately verified administrative dataset.
3. Map the candidate to an approved alerts.in.ua provider region ID.
4. Ask the user to confirm.
5. Store only the provider region ID and discard coordinates.

Until the polygon dataset and mapping are verified, location may only suggest a region; manual selection is always available.

## Deployment boundary

The planned Portainer stack is `time-tl-bot` under `/home/trio/time-tl-bot`. Historical LAN and server data must be verified immediately before deployment. Long polling avoids a public Telegram webhook and reverse-proxy changes in v1. Secrets live only in Portainer or a server `.env` with restricted permissions; the database has no published host port.

## Observability

Track provider last-success time, confirmed-state age, changed-action index if supported, regions in `UNKNOWN/STALE`, outbox depth and oldest age, worker lag, Telegram 429, 5xx, and blocked responses, rejected all-clear transitions, share creation and redemption rate-limit violations, invalid imports, and active-profile changes.
