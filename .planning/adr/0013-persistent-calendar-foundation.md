# ADR0013: explicit persistent calendars, isolated from delivery

Date: 2026-10-02. Status: accepted for LOCAL implementation by continued user
approval following phase12; no server migration or activation approval.

## Bounded phase13 first slice

Add separate calendars, memberships and targeted single-use invitations. Existing
personal schedule profiles and snapshot imports remain unchanged; no automatic
backfill, conversion or subscription. Calendar content is an independently created,
link-free validated schedule snapshot. Store one shared snapshot per calendar, not
copies per member. Calendar update increments version and appears in read-only My
Day for explicitly joined members; it cannot change reminder/all-clear delivery.
Personal event overrides and shared homework are subsequent phase13 slices.

Owner is derived from calendar.owner_user_id (not a grantable membership role).
Members have MEMBER/EDITOR/ADMIN roles. OWNER manages permissions/deletes/invites;
EDITOR/ADMIN may update schedule, but neither can change ownership or grant roles.
No ownership transfer in this slice. Users can hide/show their own calendar view and
leave; an owner must explicitly delete rather than orphan a calendar. Owner deletion
removes the calendar and associated memberships/invitations by FK cascade. Member
deletion removes only their membership/invitations, never the shared calendar.

Invitations bind an existing target Telegram user, expire within 24h, store only
SHA-256 of an unguessable token and require separate preview/explicit confirmation.
Accept creates MEMBER only; wrong user, expired/revoked/consumed/stale-calendar
invitation rejects without writes. Recipient preview shows calendar only, not member
IDs or owner private preferences. No invitations sent through Telegram in this stage.

Persist at most11 calendars owned/joined per user, reserving one of12 projection
sources for the unchanged legacy active profile. Hidden calendars still count. Every
mutation resolves an existing user, serializes that user's quota and calendar row,
checks permissions and caller CAS version within one transaction. No account creation.
Repository returns bounded owner/member DTOs; membership lists/IDs are never exposed.

Calendar CAS version covers metadata/membership mutations. A separate snapshot_version
changes only when shared schedule/name content changes. Invitations bind snapshot_version
so inviting/accepting another participant or hiding a calendar does not invalidate all
outstanding tokens. Confirmation still uses the preview's current CAS version and rejects
concurrent changes until refreshed. An actual content change invalidates older invitations.

Snapshot codec is strict, versioned, size-bounded, Kyiv-only, rejects unknown keys and
join links/credentials, retains deterministic occurrence identity on read. Existing
domain quotas/resolver validation remain authoritative. No private join link is copied
to a shared JSON payload. Calendar/invitation DTO repr hides payload/token.

## Wiring and rollout

CALENDARS_ENABLED default false, bot-only, requires MY_DAY_ENABLED. Only when ON does
bot instantiate the repository or query new tables. My Day combines legacy active
profile with explicit visible persistent sources. Any persistent-source read error is
unavailable for the whole view, not a silently incomplete/empty day. Notifications,
workers, alert state, outbox, legacy profile activation and homework stay unchanged.

Additive0009 migration must fail offline or on partial/mismatched schema; historical
0001 current-metadata bootstrap requires validating already-present tables rather than
blindly claiming head. Downgrade deletes calendar data and is not an operational
rollback plan. Deployment requires disposable PostgreSQL migration/concurrency/cascade
evidence plus backup/new schema restore compatibility and exact-release approval.

No deploy, production migration, secrets/keys/password changes, Telegram delivery,
OCR, Moiashkola, AI, shared homework or new notification generation authorized here.

## Next acceptance boundaries

Current occurrence references are deterministic on reload of the same snapshot,
not promised stable after insertion/reordering. Personal overrides must use explicit
persistent event identity and revision/conflict handling before being enabled; they
must never silently retarget a different lesson after a shared schedule update.
The strict codec checks shape/model quotas; equal-specificity recurring ambiguity
remains a fail-closed resolver error on affected dates, not a write-time global scan.

Management UI must explain subscription to a live read-only calendar, roles, explicit
confirmation, stale previews and owner deletion affecting all members. User data export
and invitation retention/cleanup need coverage before broad rollout. The RC25 backup
verifier pins head0008; a0009 release must separately update and rehearse that backup
contract with the new three tables. Existing deployed verifier is not modified here.
