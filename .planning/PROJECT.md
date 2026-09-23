# Time TL

## What This Is

Time TL is a public Telegram bot for Ukrainian online students. Each user selects a region and configures up to three reusable schedule profiles; exactly one profile is active. The bot reports official alert transitions and, after an explicitly confirmed all-clear, tells the student whether an active lesson should be joined.

Users can share a versioned schedule snapshot by Telegram link, short code, or portable `.time-tl.json` file. A recipient previews it and explicitly confirms import into an independent personal profile.

## Core Value

Never let a student miss the safe return to an active online lesson after an confirmed provider all-clear, without ever inventing an all-clear when the source is uncertain.

## Requirements

### Validated

(None yet — ship to validate.)

### Active

- [ ] Public self-service Telegram onboarding and menu.
- [ ] Region setup by Telegram location suggestion or manual selection, followed by user confirmation.
- [ ] Exact coordinates deleted after region confirmation.
- [ ] Three operating modes: lessons only, lessons plus sirens, or sirens only.
- [ ] Up to three saved schedule profiles per user with exactly one active profile.
- [ ] Flexible schedule with arbitrary lesson times, variable breaks, disabled individual lessons, even and odd dates and ISO weeks, working weekends, days off, and date exceptions.
- [ ] Safe schedule sharing and import with preview, confirmation, independent copies, and sensitive fields excluded by default.
- [ ] Deterministic alert and lesson decision engine with idempotent delivery.
- [ ] Alerts.in.ua API in production with fail-closed outage behavior.
- [ ] Local simulation before any live Telegram or production work.
- [ ] Containerized Portainer deployment on the user's mini-server after explicit approval and fresh server verification.

### Out of Scope

- AI or LLM decisions in the alert path — safety behavior must be deterministic and testable.
- Scraping the official alert map — production uses the approved API only.
- Replacing official sirens, DSNS, or the official application — the bot is an additional channel.
- Autonomous deployment or public broadcast — each live step requires explicit authorization and evidence.
- Automatic synchronization after a classmate imports a template — v1 imports an independent snapshot.

## Context

- Users may have different times on different days and parity rules for dates, weeks, or both.
- Lessons 3 and 4 may be absent while 5 and 6 remain active; breaks can have different lengths.
- Saturday or Sunday may be working, while a weekday may be a one-off day off.
- Students should be able to reuse and exchange schedule configurations instead of entering the same class timetable repeatedly.
- Official API access requires an application and token. Current endpoints, limits, and region identifiers must be verified after access is granted.
- Existing project documentation identifies a mini-server convention under `/home/trio`, Docker Compose and Portainer, and LAN host `192.168.31.219`; these details are historical inputs and must be verified live before use.

## Constraints

- **Safety:** No synthetic all-clear; source uncertainty is `UNKNOWN` or `STALE`.
- **Privacy:** Minimize personal data and do not retain exact location coordinates.
- **Sharing:** Imported data is inert until preview and explicit confirmation; user identity, region, alert preferences, and lesson links are excluded by default.
- **Profiles:** Maximum three saved profiles and exactly one active profile per user.
- **Time:** Resolve dates in `Europe/Kyiv`; use ISO week parity and calendar date parity.
- **Operations:** Secrets stay outside Git; PostgreSQL is not exposed.
- **Evidence:** Local, simulation, staging, and production status are reported separately.

## Key Decisions

| Decision | Rationale | Outcome |
|---|---|---|
| Deterministic safety core | Predictable, testable, auditable behavior | Pending validation |
| Alerts.in.ua API only | Required provenance and region hierarchy | Blocked on token and schema verification |
| Telegram long polling for v1 | No public ingress or reverse proxy change required | Pending implementation |
| PostgreSQL transactional outbox | Restart-safe dedupe and delivery evidence | Pending implementation |
| Local point-in-polygon plus confirmation | Avoid permanent coordinate storage and third-party leakage | Dataset still to be selected |
| Three notification modes | Separates lesson reminders, combined safety flow, and alert-only use | Approved |
| Three schedule profiles, one active | Supports alternate timetables without ambiguous concurrent schedules | Approved |
| Versioned snapshot sharing | Easy class reuse without hidden future changes | Approved |
| Three workers plus orchestrator | Parallelism without surrendering integration control | Configured |

## Evolution

After each phase, move only implemented and independently verified requirements to Validated, record changed decisions, and keep environment-level evidence explicit. At milestone boundaries re-check the core value, out-of-scope boundaries, live external dependencies, and server state.

---
*Last updated: 2026-09-20 after initialization, agent governance, and schedule sharing decisions.*
