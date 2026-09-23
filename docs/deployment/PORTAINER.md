# Portainer deployment runbook

This runbook records the verified exact-version **SIMULATION** deployment and the
additional gates for any future Telegram staging or production release. Simulation
success is not staging or production approval.

## Verified simulation deployment

- source commit: `4ea3b1bedfc31142ae2c53d32e550e4e8a20dcff`;
- image digest:
  `sha256:08eaa3d06e7ca5d97f11d760c44e67bc085ac395cba196f04a05839d2658da21`;
- server directory: `/home/trio/time-tl-bot`;
- Alembic revision: `0003`;
- `admin-web`, `bot`, `alert-worker`, `schedule-worker`, `sender-worker` and
  `postgres` healthy; `node-exporter` running;
- PostgreSQL 16 fault/restart E2E: `1 passed`;
- isolated backup/restore drill: passed;
- restart persistence: passed;
- admin listener: `127.0.0.1:8081` only.

The deployed configuration uses `ALERT_PROVIDER=fake`, disabled Telegram transport,
no real tokens and no real users. Schedule and sender are inert. Do not change those
properties in place and then describe the resulting stack as reviewed staging.

## Current release boundary

G2-G5 pass for SIMULATION. G6-G9 remain blocked for Telegram staging/production.
These runtime capabilities remain false and must not be patched directly on the
server:

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

The simulation commit and image above are immutable evidence, but they are not an
approved staging or production release.

## Stack shape

- stack name: `time-tl-bot`;
- server directory: `/home/trio/time-tl-bot`;
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
| `OWNER_CONFIG_SOURCE` | `database` only after an owner configuration revision is operator-activated |
| `OWNER_TELEGRAM_FERNET_KEY` | stable Fernet key only for the Telegram token; back it up and never expose it in the panel |
| `OWNER_ALERTS_FERNET_KEY` | separate stable Fernet key only for the alerts.in.ua token; never reuse the Telegram key |
| `ALERTS_IN_UA_TOKEN` | legacy env mode only; database-managed mode accepts it write-only in the owner panel |
| `ALERT_POLL_INTERVAL_SECONDS` | at least 15 |
| `ALERT_STALE_AFTER_SECONDS` | greater than poll; release default 120 |
| `ALERT_RATE_LIMIT_PER_MINUTE` | at most 5 |
| `ALERT_RATE_LIMIT_BURST` | exactly 1 |
| `TELEGRAM_TRANSPORT` | `live` only in approved staging/production |
| `TELEGRAM_BOT_TOKEN` | legacy env mode only; database-managed mode accepts it write-only in the owner panel |
| `TEST_CHAT_ALLOWLIST` | legacy env mode only; database-managed mode accepts it write-only in the owner panel |
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

## Access to the simulation admin panel

The panel is intentionally not published on the LAN or Internet. From a trusted
operator workstation that can reach the mini-server, open a local SSH tunnel:

```bash
ssh -N -L 8081:127.0.0.1:8081 trio@<MINI_SERVER_HOST>
```

While that session remains open, visit `http://127.0.0.1:8081`. Use the fixed login
`admin` and obtain the generated one-time password only from the protected bootstrap
artifact on the server. Never paste the password into this document, chat, command
history, logs or screenshots. This HTTP URL is acceptable only through the local SSH
tunnel; staging/production browser access requires separately verified HTTPS and
trusted-proxy settings.

## Rollback

Stop outbound delivery before rollback when code/schema compatibility is uncertain.
Preserve the failed database volume. Restore into a new verified volume rather than
overwriting the only copy. Use the previous immutable image only after schema
compatibility is confirmed. Restart, rollback or missing data leaves alerts
`UNKNOWN/STALE`; it never proves all-clear.

## Still unverified for staging/production

- dedicated Telegram and alerts.in.ua credentials plus allowlisted test chat IDs;
- official-provider and live Telegram paths;
- privacy-reviewed coordinate resolver (manual oblast confirmation is the current
  boundary);
- reverse proxy, TLS and trusted-proxy mapping;
- staging rollback rehearsal and exact-release approval;
- allowlisted Telegram staging and canary.
