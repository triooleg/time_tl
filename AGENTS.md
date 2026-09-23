# Time TL agent policy

## Product mission

Build a public Telegram bot that helps Ukrainian online students react to official alerts and return to an active lesson after an explicitly confirmed all-clear. The safety-critical decision path is deterministic: official alert state + the user's confirmed region + the resolved schedule.

## Source-of-truth order

1. Explicit user instructions and approvals.
2. `.planning/REQUIREMENTS.md` and approved ADRs.
3. `.planning/ARCHITECTURE.md` and domain contracts.
4. Application code and tests.
5. Agent summaries. A summary is never stronger evidence than the diff, test output, or live check it describes.

## Safety invariants

1. Use only the user-approved alerts.in.ua API in production. Do not scrape the map or silently substitute an unapproved alert provider.
2. A timeout, HTTP error, empty or partial response, missing region, expired TTL, stale cache, or process restart never means all-clear.
3. On source loss, move the affected state to `UNKNOWN` or `STALE`; never synthesize `CLEAR_CONFIRMED`.
4. Send an all-clear only after an confirmed provider transition from validated complete snapshots for the relevant region hierarchy and only when no applicable parent or child alert remains active.
5. The fake provider is restricted to local and test environments and must fail closed if selected in production.
6. Do not send messages to real users, enable mass delivery, or run a live Telegram E2E without explicit user approval and an allowlist of test chat IDs.
7. Do not deploy, push, mutate Portainer, edit production secrets, run production migrations, or change DNS, reverse proxy, or firewall without explicit approval for that action.

## Privacy and secrets

- Persist only the Telegram user ID, confirmed provider region ID, preferences, and schedule required for the service.
- Delete raw coordinates after region suggestion and confirmation; never log them.
- Never commit or print Telegram tokens, alerts.in.ua tokens, database passwords, SSH secrets, lesson links, or production `.env` values.
- PostgreSQL stays internal to the Docker network.
- Treat lesson join URLs as sensitive; omit them from logs and fixtures.

## Environment evidence

- `LOCAL`: code and tests on the workstation.
- `SIMULATION`: fake alert provider and test Telegram adapter.
- `STAGING`: separately approved test bot and allowlisted chat IDs.
- `PRODUCTION`: approved alerts.in.ua provider, real bot, deployed stack, and live checks.

Passing one level does not prove the next. Never describe mock, local, container-start, or health-only evidence as production success.

## Orchestrator protocol

- The primary agent is the integrator and final decision owner.
- Use at most three parallel subagents so one thread remains available for orchestration.
- Give each worker a task packet with a single owner and explicit allowed paths.
- Parallel write ownership must not overlap.
- Workers may not spawn children unless the orchestrator explicitly delegates that authority.
- The orchestrator verifies the actual diff, required tests, migrations, and unresolved risks before accepting work.
- No agent may approve its own safety-sensitive change.

## Role routing

| Role | Primary ownership | Required independent review |
|---|---|---|
| `time_tl_orchestrator` | planning, task routing, integration, gate status | `safety_reviewer` for safety and release claims |
| `schedule_engineer` | schedule domain and tests | `safety_reviewer` for notification-facing effects |
| `telegram_ux_builder` | bot UX and tests | orchestrator |
| `alert_integration_engineer` | alert domain, providers, alert tests | `safety_reviewer` mandatory |
| `backend_data_engineer` | persistence, migrations, workers, outbox | `safety_reviewer` |
| `portainer_operator` | Compose, healthchecks, deployment runbooks | orchestrator + `safety_reviewer` |
| `safety_reviewer` | read-only independent review and gate verdicts | cannot self-implement reviewed fixes |
| `docs_release_curator` | docs, runbooks, evidence matrix | orchestrator |

## Required task packet

Every delegated task states: task ID, goal, owner, allowed paths, read-only dependencies, acceptance criteria, commands and tests, forbidden actions, expected evidence, and handoff target. Use `.agents/task-packet-template.md`.

## Gates

- `G0 SOURCE`: official API access, current schema, terms, region hierarchy, and limits confirmed.
- `G1 SPEC`: user scenarios, notification modes, schedule terminology, and privacy rules approved.
- `G2 SCHEDULE`: parity, weekends, overrides, gaps, and conflict cases pass decision-table tests.
- `G3 ALERT SAFETY`: outage, stale data, dedupe, restart, ordering, and hierarchy tests prove no false all-clear.
- `G4 LOCAL QUALITY`: lint and type checks, migrations, unit and integration tests pass.
- `G5 SIMULATION E2E`: onboarding through alert and all-clear is verified with fixtures and restart recovery.
- `G6 TELEGRAM STAGING`: approved test bot and allowlist only.
- `G7 SERVER READINESS`: live server state, backup, rollback, resources, volumes, secrets, and healthchecks verified.
- `G8 DEPLOY APPROVAL`: explicit approval for the exact version and stack.
- `G9 CANARY`: approved accounts, monitoring, rollback, and evidence before public launch.

Allowed verdicts are `READY`, `IN_PROGRESS`, `BLOCKED`, `REVIEW`, `PASS`, and `REJECTED`. A release is ready only when all applicable gates have independent evidence.
