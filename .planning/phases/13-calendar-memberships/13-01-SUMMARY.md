# Phase13-01: persistent calendar foundation

Completed bounded LOCAL implementation, 2026-10-02 20:58 UTC / 23:58 Kyiv.
Verdict LOCAL PASS; release REVIEW. Full phase13 and Telegram management UI are not
complete. No deployment, actual migration or live PostgreSQL/Telegram check occurred.

## Implemented

Separate calendars, memberships and targeted invitations ORM plus additive0009.
Repository has independent create/update, owner-scoped list/read, own visibility,
member leave, owner delete, owner-only role grants, invitation issue/preview/accept/
revoke. OWNER is derived, cannot be granted; EDITOR/ADMIN can edit but cannot grant
roles. All mutations resolve existing accounts, check permissions/CAS and serialize
quota/calendar changes transactionally. Limits11 persisted calendars per user and20
active invitations per calendar. Token hash only, bound target, <=24h expiry.

Strict versioned link-free codec retains schedule quotas/Kyiv rules, inactive profiles
and deterministic references on reload. Content changes invalidate stale invitations;
membership/visibility changes do not, via separate snapshot_version. No implicit
backfill, activation or cloning of old personal profiles into subscriptions.

Default-OFF bot-only CALENDARS_ENABLED requires My Day and a port. Runtime constructs
calendar repository only when opted in; My Day combines one legacy active profile
with visible explicit sources. Failure/timeout/invalid-owner/oversize source list
renders whole-view unavailability, never silently omits sources. Privacy and both
account-deletion prompts explain owner-calendar loss for all members. FlagOFF retains
legacy presentation and does not query new tables. Alert/workers/outbox unchanged.

## Checks and review

Root full pytest1264PASS/23SKIP/one existing deprecation warning; Ruff src/tests/scripts/
migrations PASS, mypy81 source files PASS, single static Alembic head0009. Independent
C13-S LOCAL PASS after218 focused tests plus runtime opt-in test; realPG deselected.
Review caught and fixed invitation coexistence, cross-schema/index validation and SQL
literal/grouping/default comparisons. See13-01-REVIEW.md and13-01-VERIFICATION.md.

## Remaining

Management buttons/confirmation flows, invitation retention/data export and personal
event overrides are subsequent slices. Overrides need persistent event identity,
not positional references. Shared homework, digest/notification integration, OCR and
Moiashkola are not delivered here. Real disposable PostgreSQL migration/reflection/
concurrency/cascade checks and backup/restore at head0009 required before release.
RC25 verifier currently pins0008 and was not changed. Exact new release approval and
staging/canary still required. Passwords/keys/server/production RC25 untouched.

gsd-progress routing preserved prior evidence and moved to the additive next slice;
direct artifacts used because SDK unavailable. Dirty unrelated files retained; no Git
commit/push or new signing keys. Next scope in13-02-NEXT.md.
