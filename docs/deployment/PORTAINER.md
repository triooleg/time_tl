# Portainer deployment runbook

This runbook describes a future exact-version deployment. It is not approval and
does not record a deployment. As of 2026-09-23 no Time TL stack, migration, admin
bootstrap or Telegram delivery has been run on the mini-server.

## Current release boundary

Local G2, G3 and G4 evidence passes. G5 is blocked pending real PostgreSQL
fault/restart evidence; G6 and later gates are blocked. These runtime capabilities
remain false and must not be patched at deployment time:

- `production_oblast_scope_release`;
- `reviewed_region_resolver`;
- `schedule_worker`;
- `sender_worker`.

Implementations exist behind these gates. A production stack must fail closed until
an independently reviewed exact version intentionally changes the corresponding
capabilities. Manual oblast selection works; coordinate resolution is not released.

## Exact-version evidence required before G8

Record all of the following in one approval packet:

1. Git commit or immutable source archive checksum;
2. built image tag and registry digest, never `latest`;
3. exact `docker-compose.yml` checksum;
4. Alembic current and target revision;
5. independent G3/G5/G6/G7 verdicts;
6. approved staging bot and allowlisted chat IDs;
7. backup checksum plus successful isolated restore evidence;
8. previous image/schema compatibility and rollback procedure.

The project Git repository has not yet been prepared as a release source, so there
is currently no exact version eligible for approval.

## Stack shape

- planned stack name: `time-tl-bot`;
- planned server directory: `/home/trio/time-tl-bot`;
- eight Compose services: bot, alert-worker, schedule-worker, sender-worker,
  admin-web, migrate, node-exporter and postgres;
- PostgreSQL remains on the internal `database` network and has no published port;
- node-exporter remains on the internal `metrics` network;
- admin binds to `127.0.0.1:8081` by default and requires an approved HTTPS reverse
  proxy for non-local access;
- alert-worker must have exactly one replica.

Historical server addresses or credentials are not deployment evidence. Immediately
before an approved change, recheck Docker/Compose/Portainer versions, free CPU/RAM/
disk, directory ownership, existing stacks, volumes, reverse proxy and backup path.
Do not touch unrelated stacks.

## Required variables

Secrets belong in the protected Portainer environment/secrets facility and never in
Git or build arguments.

| Variable | Exact release contract |
|---|---|
| `TIME_TL_ENV` | `staging` first; `production` only after separate approval |
| `TIME_TL_IMAGE_TAG` | approved immutable tag/digest, never `latest` |
| `DATABASE_URL` | internal `postgresql+asyncpg://` URL |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | matching private database credentials |
| `ALERT_PROVIDER` | `alerts_in_ua` |
| `ALERTS_IN_UA_TOKEN` | dedicated Time TL token |
| `ALERT_POLL_INTERVAL_SECONDS` | at least 15 |
| `ALERT_STALE_AFTER_SECONDS` | greater than poll; release default 120 |
| `ALERT_RATE_LIMIT_PER_MINUTE` | at most 5 |
| `ALERT_RATE_LIMIT_BURST` | exactly 1 |
| `TELEGRAM_TRANSPORT` | `live` only in approved staging/production |
| `TELEGRAM_BOT_TOKEN` | dedicated bot token |
| `TEST_CHAT_ALLOWLIST` | mandatory numeric IDs in staging |
| `LESSON_LINK_FERNET_KEY` | stable 32-byte Fernet key encoded for the application |
| `SENDER_TRANSPORT_TIMEOUT_SECONDS` | 8 seconds; allowed range >0 through 10 |
| `ADMIN_BACKEND` | `postgres` |
| `ADMIN_IP_HASH_KEY` | stable secret of at least 32 bytes |
| `HOST_METRICS_URL` | internal `http://node-exporter:9100/metrics` |
| `ADMIN_COOKIE_SECURE` | `true` behind verified HTTPS |
| `ADMIN_TRUSTED_PROXY_CIDRS` | only actual reverse-proxy CIDRs |
| `ALLOW_SCAFFOLD_SERVICES` | `false` |

Compose explicitly forwards `SENDER_TRANSPORT_TIMEOUT_SECONDS` through its common
environment. The release default is 8 seconds; any override remains part of the
exact reviewed Compose/environment evidence.

## Provider and delivery safety

- The official endpoint returns a validated 27-character oblast string; `A` and
  `P` are ACTIVE and `N` is not trusted from partial or malformed data.
- One global worker polls for all students at no more than the configured rate.
- Provider HTTP redirects and environment proxy inheritance are disabled.
- The response limit is 4096 bytes for both declared and actual body size.
- Snapshot/baseline/outbox and fetch revision/`Last-Modified` commit atomically.
- Stale, timeout, invalid, restart and auth failures never synthesize all-clear.
- Sender performs a final transactional mode/state/profile/freshness authorization
  immediately around Telegram delivery and uses a bounded transport timeout.

## Pre-deployment read-only checks

1. Re-run pytest, unittest, Ruff, strict mypy, compileall, Alembic offline, Compose
   parse and JavaScript syntax checks on the exact source.
2. Build and scan the exact image; inspect the final digest and build context.
3. Verify the image contains migrations, `data/` and `docs/`.
4. Verify Compose resolves exactly eight expected services and no secret values are
   embedded in the rendered file.
5. Verify PostgreSQL and admin ports are not unintentionally public.
6. Confirm only approved staging chat IDs can pass every inbound/outbound route.
7. Obtain explicit G8 approval after these checks, not before.

## Controlled migration and update after approval

1. Stop or keep disabled outbound delivery.
2. Take and checksum an encrypted PostgreSQL logical backup.
3. Restore it into an isolated database and run integrity/row-count checks.
4. Record existing containers, volumes, image digest and migration revision.
5. Run one controlled migration job:

   ```bash
   docker compose --profile ops run --rm migrate
   ```

6. Start/update the exact approved stack.
7. Check DB health, migration revision and each process liveness endpoint.
8. Check provider freshness/circuit, singleton lease, worker heartbeats, outbox depth
   and age, Telegram errors and node-exporter mapping.
9. Run only allowlisted staging/canary accounts.

Container health is liveness only. It does not prove fresh alerts, correct all-clear,
successful Telegram delivery, backup usability or whole-server health.

## Admin bootstrap

Run `bootstrap-admin` only after migrations and protected artifact storage are
verified. The fixed username is `admin`; no password exists in this repository.
Never place the one-time password in Stack variables or logs. Verify mandatory
password change and artifact deletion/expiry before handing the URL to the owner.

## Rollback

Stop outbound delivery before rollback when code/schema compatibility is uncertain.
Preserve the failed database volume. Restore into a new verified volume rather than
overwriting the only copy. Use the previous immutable image only after schema
compatibility is confirmed. Restart, rollback or missing data leaves alerts
`UNKNOWN/STALE`; it never proves all-clear.

## Still unverified

- live container build and healthchecks on the mini-server;
- PostgreSQL process-kill/commit fault injection and restart recovery;
- Portainer volumes, backup/restore and rollback rehearsal;
- reverse proxy, TLS and trusted-proxy mapping;
- admin bootstrap/password and live node-exporter metrics;
- allowlisted Telegram staging and canary.
