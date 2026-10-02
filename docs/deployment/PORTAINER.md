# Portainer deployment runbook

This runbook records the verified exact-version **SIMULATION** deployment and the
additional gates for any future Telegram staging or production release. Simulation
success is not staging or production approval.

## Detached production attestation workflow (prepared, not deployed)

`docker-compose.production.yml` is a standalone production contract; do not merge
it with the simulation/staging file. It requires digest-pinned references for the
application, PostgreSQL and node-exporter images. It also mounts one
operator-controlled host directory read-only at `/run/time-tl-release` in every
application role and operator job. That directory must contain exactly the reviewed
`release-manifest.json` bytes and their detached
`release-manifest.ed25519` signature at the runtime's fixed paths.

The signing private key must remain in an external protected signing system. Never
generate it in this repository, put it in Portainer, copy it to the server, include
it in an image/build argument, or store it beside the detached signature. Only the
reviewed Ed25519 public key may be compiled into an exact release image.

The mandatory order for an approved production candidate is:

1. Build, test and scan the exact source first. Push the immutable image and resolve
   its registry digest. Pin PostgreSQL and node-exporter by digest as well.
2. Create the manifest from the final tag, commit, tree, source-archive SHA-256,
   application image digest, configuration revision, catalogue SHA-256, Alembic
   head, provider, territory mode, singleton count and the exact boolean
   `homework_enabled` capability. For the homework production candidate the signed
   values must be `migration_head=0008_homework_foundation` and
   `homework_enabled=true`. Do not rewrite or normalize the manifest after signing.
3. Have the external signing system sign those exact manifest bytes with the
   protected Ed25519 private key. Transfer only the fixed manifest and base64
   detached signature into a root/operator-owned directory. Make files read-only
   to the container UID and make the directory non-writable by that UID.
4. Populate protected environment values from `.env.production.example`. Confirm
   `TEST_CHAT_ALLOWLIST` renders as the empty string, `TIME_TL_ENV=production`,
   `OWNER_CONFIG_SOURCE=database`, `ADMIN_BACKEND=postgres`,
   `ADMIN_COOKIE_SECURE=true`, and `ALLOW_SCAFFOLD_SERVICES=false`. Before invoking
   Compose, run the fail-closed input verifier. It rejects blank, tag-only,
   `latest`, uppercase, short and otherwise non-canonical image references:

   ```bash
   python scripts/deployment/verify_production_images.py \
     --env-file /protected/time-tl-production.env
   ```

   A `PASS (environment)` result is mandatory. Never replace this verifier with a
   visual check or `${VAR:?}`; Compose only proves that a variable is non-empty.
5. Render the standalone file to protected JSON before any change, then run the
   verifier again against the resolved Compose semantics:

   ```bash
   umask 077
   docker compose --env-file /protected/time-tl-production.env \
     -f docker-compose.production.yml --profile ops config --format json \
     > /tmp/time-tl-production.rendered.json
   python scripts/deployment/verify_production_images.py \
     --env-file /protected/time-tl-production.env \
     --compose-config-json /tmp/time-tl-production.rendered.json
   ```

   A `PASS (environment+compose)` result is mandatory. The protected JSON contains
   resolved secrets: do not print or attach it to shared logs, and remove it after
   the gate. Separately inspect its structure in the protected operator session.
   Reject missing read-only attestation mounts, excess per-role secrets/networks, a
   non-empty staging allowlist, public PostgreSQL/node-exporter ports, or more than
   one replica for bot/alert/schedule/sender. `HOMEWORK_ENABLED` must be the literal
   `true` only on `bot`; it must not be an operator-overridable environment input or
   appear on another role.
   The published admin port also requires `admin-web` to retain the non-internal
   `egress` network; Docker does not install that host binding when every attached
   network is internal. The host bind address and reverse proxy remain the access
   boundary.
6. Pull the exact references, then compare the external deployed identity with the
   signed manifest before preflight. For each container, verify `docker inspect`
   `.Config.Image` is the approved `repository@sha256:...`, its `.Image` equals the
   ID returned by `docker image inspect` for that reference, and the approved
   digest is present in `RepoDigests`. Runtime attestation does not replace this
   external comparison.
7. Take an encrypted logical backup, checksum it, and restore it into an isolated
   PostgreSQL instance. The encrypted copy must also exist on storage independent
   from the server filesystem. Verify integrity, row counts and the rollback
   artifact; a checksum of an unencrypted same-disk dump is not restore evidence.
   Start only PostgreSQL and internal node-exporter. Verify the source head is
   `0007_outbox_indeterminate`, then run the one migration job and require the exact
   target `0008_homework_foundation` before preflight:

   ```bash
   docker compose --env-file /protected/time-tl-production.env \
     -f docker-compose.production.yml --profile ops run --rm migrate
   ```

8. Install the external monitoring rules and exercise their notification route.
   The external off-host collector owns the protected Ed25519 private key; the
   server, repository, image and Portainer must never contain it. Export a
   secret-free evidence JSON no more than 15 minutes before the release gate and
   sign its exact raw bytes. Transfer the JSON and detached base64 signature, then
   verify them together with the protected thresholds and one-use receipt ledger:

   ```bash
   python scripts/deployment/verify_production_monitoring.py \
     --env-file /protected/time-tl-production.env \
     --evidence-json /protected/time-tl-monitoring-evidence.json \
     --signature-file /protected/time-tl-monitoring-evidence.ed25519 \
     --receipt-state-dir /protected/time-tl-monitoring-receipts
   ```

   `PASS` is mandatory. This verifier validates the operator contract and fresh
   signed evidence; it does not replace the external collector or notification
   system. The verifier uses only the compiled public key
   `1a52a9d96acb88d89be3de992efb75d968620f642c1240dd1121b96ca74f320c`.
   It atomically claims the opaque notification receipt, so the same evidence
   cannot pass twice. Never put webhook URLs, recipients, credentials, chat IDs or
   raw metric payloads in the evidence JSON.
9. Run exactly one detached production preflight. It verifies the fixed signature,
   manifest/revision, actual database migration head, exact pending-payload hash,
   official provider, Telegram `getMe`, and the internal metrics endpoint. It sends
   no message and does not activate the revision. On success it stores one
   secret-free receipt in PostgreSQL, bound to the revision, manifest fingerprint,
   pending-payload SHA-256 and migration head, with a ten-minute expiry:

   ```bash
   docker compose --env-file /protected/time-tl-production.env \
     -f docker-compose.production.yml --profile ops run --rm production-preflight
   ```

   Record the sanitized PASS summary and receipt audit row. Do not run this command
   again. Any failed, missing or expired receipt requires a newly authorized
   preflight; it must never be replaced with a handwritten evidence string.
10. Within the receipt's ten-minute lifetime, and only after independent
   safety-review `PASS`, exact-version G8 approval and a verified G9 plan, run the
   separate activation job once:

   ```bash
   docker compose --env-file /protected/time-tl-production.env \
     -f docker-compose.production.yml --profile ops run --rm production-activate
   ```

   Activation makes no Telegram or provider request. In one database transaction it
   locks the owner state and receipt, recomputes the pending-payload hash, rechecks
   the migration head, manifest fingerprint, revision, PASS status, expiry and
   unused status, activates that exact revision, and marks the receipt consumed.
   Any mismatch fails closed without partial activation. This separation is what
   makes the external production preflight exact-once while retaining an independent
   review boundary.

11. Start `admin-web`, then the singleton `alert-worker`. Require a fresh complete
    official snapshot, closed circuit and current heartbeat before starting `bot`
    and `schedule-worker`. Start `sender-worker` last. Observe outbox depth/age,
    Telegram errors, leases, restart counters and resource metrics throughout the
    bounded canary. Container health alone is not release evidence.

### Production monitoring and HOLD contract

The public stack requires an external off-host collector and a tested notification
route; `node-exporter` alone is not monitoring. The protected environment must
contain the thresholds from `.env.production.example`. The signed evidence has an
exact schema: `schema_version=1`, timezone-aware `generated_at` and
`notification_route_exercised_at`, an opaque lowercase SHA-256
`notification_route_receipt_sha256`, an `observations` object, and non-secret
`stop_authority` and `rollback_authority` labels. Unexpected or duplicate fields,
invalid signatures, evidence or route exercises older than 15 minutes, future
timestamps, non-finite values and previously consumed receipts are rejected.

The observations contain the actual numeric values, not self-asserted PASS flags:
provider freshness and maximum heartbeat ages, restart-count increase, oldest/open/
dead/retrying outbox values, database health, disk-free and memory-available
percentages, verified encrypted-backup age and TLS days remaining. The verifier
checks every value against the protected thresholds before atomically recording the
notification receipt in the root/operator-controlled receipt-state directory.

Move the release to `HOLD` and stop `sender-worker` first when any threshold trips:

| Signal | Default production HOLD threshold |
|---|---:|
| Provider last successful complete snapshot age | 120 seconds |
| Any required worker heartbeat age | 90 seconds |
| Container restart-count increase | 1 |
| Oldest open outbox item age | 60 seconds |
| Open outbox depth | 100 |
| New dead outbox rows | 1 |
| New retrying outbox rows | 1 |
| PostgreSQL health | any failure |
| Host disk free | below 15 percent |
| Host memory available | below 10 percent |
| Last verified encrypted off-host backup age | over 24 hours |
| TLS certificate remaining validity | below 14 days |

Provider freshness/circuit and heartbeat failures also prohibit interpreting the
stored state as all-clear. After stopping delivery, preserve logs and the failed
database volume, identify whether rollback is schema-compatible, and use only the
recorded stop/rollback authorities. A dashboard without a tested notification route,
or evidence older than 15 minutes at the gate, is `REJECTED`.

The candidate production capability switch is `True`, but it is only one input to
the layered gate. The exact independently reviewed image still fails closed without
its compiled release-verification key, valid signed release manifest, matching
database revision, one-use production preflight receipt and all G7-G9 evidence.
Therefore this Compose contract, the switch and a passing render do not themselves
enable or approve production.

### Production rollback

Stop `sender-worker`, then bot/schedule/alert delivery paths before rollback when
code/schema compatibility or alert freshness is uncertain. Preserve the failed
volume and logs. Compare the previous image digest, signed manifest, migration head
and Compose checksum with the recorded rollback packet. Restore the verified backup
into a new volume; never overwrite the only database copy. Use the prior immutable
image only when its code is compatible with the restored schema, remount its matching
signed attestation, run the same external digest checks, and start in the same order.
Any restart, restore, missing snapshot or ambiguous provider state remains
`UNKNOWN/STALE`; it never authorizes an all-clear.

### Residual live gates

This repository work is local preparation only:

- **G7 SERVER READINESS remains unverified** until a fresh read-only server check
  confirms Docker/Compose versions, resources, ownership, protected secrets,
  existing volumes/stacks, internal networks, TLS/proxy mapping, backup destination,
  restore capacity and the actual digest-pinned render.
- **G8 DEPLOY APPROVAL remains blocked** until the final compiled public key/switch,
  exact tag/commit/tree/archive checksum, signed manifest bytes/signature, three
  image references, Compose checksum, migration evidence, restore evidence and
  independent safety verdict are approved as one immutable packet.
- **G9 CANARY remains blocked** until the approved production accounts, monitoring
  thresholds, stop/rollback authority and an observed inbound-to-outbound canary
  are recorded. No public message or all-clear is authorized by these files.

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

G2-G5 pass for SIMULATION. G6-G9 remain blocked until exact STAGING evidence is
recorded. The reviewed code now contains separate STAGING-only capabilities for
the manual-region bot, schedule worker and sender worker. They open only when one
ACTIVE database revision has supplied alerts.in.ua, Live Telegram and a non-empty
allowlist. These production capabilities remain false and must not be patched
directly on the server:

- `production_oblast_scope_release`;
- `reviewed_region_resolver`;
- `schedule_worker`;
- `sender_worker`.

The STAGING variants are `staging_manual_region_bot`,
`staging_schedule_worker` and `staging_sender_worker`. A production stack still
fails closed. Manual oblast selection works; coordinate resolution is not released.

## Pending-revision operator preflight

Run this against the exact candidate image before activation. Substitute the
separately confirmed numeric test chat ID; do not paste tokens into the command:

```bash
docker compose run --rm --no-deps \
  -e TIME_TL_ENV=staging \
  admin-web preflight-owner-settings \
  --revision 5 \
  --approved-chat-id <APPROVED_NUMERIC_CHAT_ID>
```

Expected output is sanitized and ends with `persisted=no`. The command performs
only Telegram `getMe` and one unconditional official 27-oblast FULL_200 probe.
It never calls sendMessage, mutates webhooks, persists the provider snapshot or
marks the revision ACTIVE. Any mismatch, timeout, 401/403/429, malformed/partial
body or missing Last-Modified fails the preflight.

Only after a passing result, independent safety review and exact-version approval
may the operator run `activate-owner-settings`. Activation and controlled worker
restart are separate steps.

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
- Alert, schedule and sender startup each commit the same restart fence before
  polling, planning or constructing the live Telegram client; stale pre-restart
  freshness therefore cannot authorize an all-clear or return message.
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
