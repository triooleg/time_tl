# Time TL roadmap

| Phase | Outcome | Gate |
|---|---|---|
| 1. Project contract | Requirements, architecture, executable agents, governance, safety invariants | G1 |
| 2. Local foundation | Application skeleton, PostgreSQL, migrations, fake provider, Compose | G4 |
| 3. Onboarding and region | `/start`, region confirmation and manual selection, three operating modes and reminder lead time | G4 |
| 4. Schedule profiles and sharing | Three profiles, one active, flexible rules, parity, exceptions, preview, safe export and import | G2 plus sharing gate |
| 5. Decision and delivery engine | Alert state machine, hierarchy, outbox, restart-safe notifications | G3 |
| 6. Simulation E2E | Full multi-user flow using fixtures, template sharing, and restart recovery | G5 |
| 7. Official integration | Current API contract, token, limits, mapping, live read-only evidence | G0 then G3 |
| 8. Portainer readiness and deploy | Backup, rollback, hardened Compose, approved stack deployment | G7 then G8 |
| 9. Canary and public launch | Allowlisted pilot, monitoring, deletion flow, rollback evidence | G9 |
| 10. Admin dashboard | Owner-only monitoring and configuration with audited activation | G7-G9 |
| 11. Manual personal homework | Disabled-by-default manual homework CRUD using the active schedule; no OCR or Moiashkola | G2, G4, G5 |
| 12. Calendar day projection | Default-off My Day reads the existing active schedule; tested pure multi-calendar projection; no persistence migration | G2, G4, G5 |
| 13. Calendar memberships | Additive persistent personal/shared calendars, memberships, permissions and overrides; explicit opt-in, preserved legacy schedules | G1, G2, G4, G5, privacy review |

Critical path: contract -> local foundation -> onboarding and schedule -> decision engine -> simulation -> official API -> approved Portainer deployment -> canary.

Phase 4 acceptance includes:

- no more than three profiles per user;
- exactly one active profile;
- import creates an independent inactive copy;
- preview and confirmation before create or replace;
- deep link, short code, and versioned file round trips;
- prohibited fields absent by default;
- invalid, expired, revoked, oversized, and unsupported templates do not mutate state.

External blockers: Telegram token, approved alerts.in.ua token and current schema and limits, verified administrative polygons and mapping, live mini-server access, and explicit approval for staging and deployment.

## New concept checkpoint — 2026-10-02

RC25 admin/monitor/backups are deployed and accepted by the owner. Historical v1
phase rows above are not current live gate verdicts; see STATE.md and release receipts.
Phase12 is LOCAL implementation of ADR0012, not automatic activation of simultaneous
legacy profiles. Phase13-01 has scoped LOCAL PASS (ADR0013): persistent calendars,
targeted invitations, roles and optional read bridge. Phase13-02 calendar management
UI and private export are LOCAL/lab PASS; RC26 is a signed candidate, not deployed.
Role-management/editing UI and personal overrides remain subsequent slices; shared homework,
digest changes, OCR, Moiashkola and Mini App are not delivered by phase12.
