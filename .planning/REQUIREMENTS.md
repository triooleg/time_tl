# Requirements: Time TL

**Defined:** 2026-09-20
**Core Value:** Safe, timely return to an active online lesson after an confirmed all-clear from validated complete provider snapshots.

## v1 Requirements

### Onboarding and region

- [ ] **ONB-01**: A new user receives a concise safety introduction and completes setup through buttons without memorizing commands.
- [ ] **REG-01**: A user can send Telegram location and receive a proposed supported administrative region.
- [ ] **REG-02**: A user can manually select a supported region when location is unavailable or incorrect.
- [ ] **REG-03**: The proposed region is saved only after confirmation and raw coordinates are not retained.
- [ ] **REG-04**: A user can view and change the saved region.

### Notification preferences

- [ ] **NOT-01**: A user can select exactly one operating mode: lessons only, lessons plus sirens, or sirens only.
- [ ] **NOT-02**: Lessons-only mode sends configurable upcoming and start reminders from the active schedule profile and never sends alert notifications.
- [ ] **NOT-03**: Lessons-plus-sirens mode sends lesson reminders, official alert transitions, safety guidance, and schedule-aware return-to-lesson messages after an confirmed provider all-clear.
- [ ] **NOT-04**: Sirens-only mode sends official alert start and all-clear notifications without lesson reminders or schedule-based join prompts.
- [ ] **NOT-05**: A user can configure how long before an enabled lesson its reminder is sent.
- [ ] **NOT-06**: Repeating an update or restarting a worker does not duplicate a notification.

### Schedule profiles

- [ ] **PRO-01**: A user can store no more than three named schedule profiles.
- [ ] **PRO-02**: Exactly one profile is active when the user has configured schedules.
- [ ] **PRO-03**: Switching the active profile requires confirmation and displays a resolved preview.
- [ ] **PRO-04**: Import creates an inactive independent profile and never silently replaces an existing profile.
- [ ] **PRO-05**: At the three-profile limit, the user must explicitly select a profile to replace or cancel.

### Schedule

- [ ] **SCH-01**: A user can create lessons with arbitrary start and end times, title, enabled state, and optional join link.
- [ ] **SCH-02**: Gaps between lessons are derived and displayed as breaks or free windows with variable duration.
- [ ] **SCH-03**: Any individual lesson can be disabled without deleting later lessons.
- [ ] **SCH-04**: Rules support any weekday, including working Saturday or Sunday and weekday days off.
- [ ] **SCH-05**: Rules support even and odd ISO weeks, even and odd calendar dates, and their combination.
- [ ] **SCH-06**: A specific-date day-off or custom schedule overrides recurring rules.
- [ ] **SCH-07**: The resolver applies the documented specificity order and rejects conflicting equal-specificity intervals.
- [ ] **SCH-08**: The user can copy a day or week and preview the resolved schedule for a selected date.

### Sharing

- [ ] **SHR-01**: A user can export a selected schedule profile as a Telegram deep link, short code, or versioned `.time-tl.json` file.
- [ ] **SHR-02**: A recipient sees template name, creation date, source display label, schema version, included fields, and a 14-day resolved preview before import.
- [ ] **SHR-03**: Opening a link or file never applies a schedule without explicit confirmation.
- [ ] **SHR-04**: Imported profiles are independent snapshots; later source changes do not propagate.
- [ ] **SHR-05**: Telegram identifiers, region, coordinates, alert settings, and secrets are never exported.
- [ ] **SHR-06**: Lesson join links are excluded by default and require a separate explicit include-and-confirm action.
- [ ] **SHR-07**: Expired, revoked, malformed, oversized, or unsupported template versions fail safely without modifying profiles.
- [ ] **SHR-08**: Share codes are unguessable, revocable, rate-limited, and expire according to policy.

### Alert safety and lesson decisions

- [ ] **ALT-01**: Production alert state comes only from the user-approved alerts.in.ua API.
- [ ] **ALT-02**: Region, district, and community hierarchy contributes to the user's effective alert state.
- [ ] **ALT-03**: Timeout, error, missing or empty data, stale state, or restart cannot produce an all-clear.
- [ ] **ALT-04**: An all-clear notification requires an confirmed provider transition from validated complete snapshots and no other applicable active alert.
- [ ] **ALT-05**: When an alert begins during a relevant lesson window, the bot instructs the user to seek shelter rather than join.
- [ ] **ALT-06**: When an alert remains active at lesson start, the bot does not suggest joining.
- [ ] **ALT-07**: After an confirmed provider all-clear during an enabled lesson, the bot identifies the active lesson and offers its join link when configured.
- [ ] **ALT-08**: After an confirmed provider all-clear before the next lesson, the bot reports the next relevant start time.

### Operations and privacy

- [ ] **OPS-01**: Local and automated tests can use a fake provider without internet access.
- [ ] **OPS-02**: Production configuration cannot start with the fake provider.
- [ ] **OPS-03**: The stack runs as separate bot, worker, and PostgreSQL services with healthchecks and persistent storage.
- [ ] **OPS-04**: Secrets are supplied outside Git and PostgreSQL is not exposed publicly.
- [ ] **OPS-05**: Backup, restore, rollback, freshness, queue age, and provider failure procedures are documented and tested before public launch.
- [ ] **PRV-01**: Logs exclude tokens, coordinates, lesson links, and sensitive payload fields.
- [ ] **PRV-02**: A user can stop notifications and request deletion of stored personal data.

## v2 Requirements

### Approved calendar concept: incremental LOCAL implementation

The user's new-concept document and continuation approvals supersede future product
scope, not historical v1 delivery rules. Existing personal profiles/snapshot imports
remain compatible. These rows are not deployment or public messaging approvals.

- **CAL-01**: Combine explicit visible calendars in read-only My Day; retain repeated
  and simultaneous events and show conflicts, with no implicit join decision.
- **CAL-02**: Persist independently created calendars and memberships; never convert
  inactive old profiles into subscriptions or change legacy delivery input silently.
- **CAL-03**: Enter shared calendars only after targeted invitation preview and
  confirmation; expiry/revocation/consumption and quota gates are atomic.
- **CAL-04**: Enforce OWNER/ADMIN/EDITOR/MEMBER permissions and CAS conflicts;
  non-owners cannot grant roles or ownership. Hide/leave affects the actor only.
- **CAL-05**: Preserve owner/member isolation and deletion cascades; no join links,
  coordinates, secrets or other members' completion state in shared payloads.
- **CAL-06**: Personal event overrides must not change the shared schedule;
  shared homework and per-user completion are separate future slices.

CAL-01 has phase12/13 LOCAL evidence. CAL-02..05 have phase13-01 foundation and
phase13-02 Telegram management/actor-only export, with independent LOCAL and actual
disposable PostgreSQL migration/concurrency/cascade evidence. Production deployment
and allowlisted canary remain separate gates; role-management/editing UI is a later
slice. CAL-06 remains planned, not implemented.

- **IMP-01**: Import a timetable from free-form text or an image with mandatory user confirmation.
- **MINI-01**: Optional Telegram Mini App schedule editor.
- **SYNC-01**: Optional subscription to future versions of a trusted shared class template with explicit update review.
- **LOC-01**: Multiple saved regions per user.
- **LANG-01**: Full user-selectable Ukrainian and Russian localization.

## Out of Scope

| Feature | Reason |
|---|---|
| AI alert decisions | Cannot be the authority for safety state |
| Unofficial fallback alert feeds | Could conflict with the approved source |
| Autonomous production deployment | Requires explicit approval and live verification |
| Guaranteed critical delivery | Telegram delivery and device notification settings are outside the bot's control |
| Silent remote schedule updates | Could alter a student's active schedule without informed confirmation |

## Traceability

| Requirements | Phase | Status |
|---|---|---|
| ONB-01, REG-01..04, NOT-01..05 | 3 | Pending |
| PRO-01..05, SCH-01..08, SHR-01..08 | 4 | Pending |
| NOT-06, ALT-03..08 | 5 | Pending |
| OPS-01, OPS-02 | 2 | Pending |
| ALT-01, ALT-02 | 7 | Blocked on official access |
| OPS-03, OPS-04 | 2 and 8 | Pending |
| OPS-05, PRV-01, PRV-02 | 8 and 9 | Pending |

**Coverage:** 44 v1 requirements; all mapped; official integration remains externally blocked.

---
*Last updated: 2026-09-20 after schedule profile and sharing requirements.*
