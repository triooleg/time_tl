# ADR 0012: additive calendars and My Day projection

Date: 2026-10-02. Status: accepted for LOCAL implementation by the user's
"проверил все работает. давай дальше", following the proposed calendars/My Day stage.

## Context and authority

RC25 owner reports login/catalogue/settings working. Operational deployment already
passed independent review. This closes the pending owner UI acceptance, not a test
of production database restoration. Existing application has three personal schedule
profiles, one active, independent snapshot sharing and a deterministic safety path.
The new user concept requests multiple subscribed calendars and a unified day.

## Decision: first vertical slice, no destructive migration

Introduce pure Calendar / CalendarMembership / CalendarSource / TimelineEvent / MyDay
types. Calendar occurrence resolution reuses the existing parity, override, disabled
lesson and Kyiv timezone resolver. Memberships filter by user and visibility; source
identities are unique. Repeated lessons are retained. All simultaneous events and
overlap pairs are explicit; no automatic primary/join target is chosen.

The first Telegram view uses ONLY the user's existing active profile as a personal
calendar. Inactive profiles remain alternatives, not implicit subscriptions. The
bridge reads existing data without cloning, activating, rewriting or backfilling it.
No schema changes, new persistent memberships, class invitations or group editing
are claimed by this slice. Those require the separate additive persistence stage.

MY_DAY_ENABLED is bot-only, default false. Enabled view has current lesson(s), time
remaining, next start group and paginated day events. It contains no join links or
assertion of all-clear. A schedule view is not permission to leave shelter or join.
An unavailable/conflicting schedule is displayed as unavailable, not as an empty day.
Optional homework read failure remains explicit and cannot break schedule/alerts.
Homework preview counts are labelled as the current bounded list, not a complete
history or proof that nothing was assigned. No implicit assignment-to-event match.

## Safety and privacy

Alert provider, hierarchy, state machine, workers, outbox, active-profile selection,
notification preferences and delivery dedupe stay unchanged. Calendar projection is
NOT an input to return-to-lesson decisions in this stage. Datetimes must be aware;
ambiguous/nonexistent Kyiv lesson wall times fail closed. Occurrence IDs are scoped
to calendar/date and hashed persistent lesson reference; joins are absent from view
models, identifiers and repr. No new identifiers or student completion data exposed
to another member. All current schedule ownership checks remain authoritative.

## Next stage boundaries

Persistent calendars/memberships, roles and personal overrides need additive schema,
owner-scoped repositories, explicit invitations, CAS/version checks, privacy deletion
and export, simulation and independent review. Existing snapshot imports remain
snapshots unless explicitly joined to a calendar. Worker integration is a separate
safety change, not implicit in displaying combined events.
OCR, Moiashkola, voice/AI, Mini App, gamification, new automatic notifications and
production migration/deployment are excluded from this implementation approval.

## Verification

Pure resolution decision tables, membership privacy, limits, deterministic ordering,
all overlap pairs, adjacent boundaries, date/parity overrides, Kyiv midnight/DST,
link-free output, pagination and UTF-16 message limits. Async facade/router tests
must prove flag-off compatibility, no account writes, owner isolation, stale/malformed
callback handling and optional read failure. Existing worker and alert tests remain
regression gates. LOCAL/SIMULATION success is not staging or production evidence.
