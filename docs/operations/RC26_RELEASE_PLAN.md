# RC26 calendars: production deployment approval packet

Status REVIEW, not deployed. Current observed production RC25: revision14,
head0008, all six application/PostgreSQL containers healthy/restarts0. Passwords,
keys, SSH configuration, alert source/catalogue, notification modes and recipients
must be preserved. No bulk broadcast is authorized.

Candidate: `time-tl-production-20261003-rc26-calendars-candidate`.
ArchiveSHA256 `ae0a715a6eeddd0e54bbcf45e3bb23b0ba2277e75087eb46a0f508a4a7da9e37`.
Application digest `sha256:5d0c094d90bf8c0940f94a5ca10b357ef2322cdfc9c2f339f51a7c63dcca4728`.
Host backup archive SHA256 `135109a50e02076b5c4d4b74333f9787fbdd102e7b82f725d1746b817d9161aa`.
Public candidate provenance/signature and host backup-contract archive live together
under `.release/time-tl-production-20261003-rc26-calendars`. New source commit is
currently NONE, honestly labelled; the source-file table is the frozen identity.

## Approval scope requested

Approve this exact candidate and attached backup contract, a LOCAL release commit
of reviewed required files only (no unrelated agent/settings changes and no push),
and runtime manifest/signature using the existing external key. No key generation,
rotation, passwords or secret replacement. Resolve actual pending revision freshly;
15 is only the expected next value after observed active14/no pending revision.

Authorize pre-migration verified backup, coordinated short maintenance, production
additive0009 migration, admin-web/bot/workers replacement at the exact digest,
backup/monitor handoff, one read-only production preflight of the actual pending
revision, activation only after full PASS and independent safety-review, existing
operator-allowlist canary and resumption of ordinary public operation. No new test
recipients, unsolicited invitations or mass messages. New calendar UI sends only
responses/documents requested by the same private-chat user.

## Mandatory operator sequence after G8

1. Recompute source/host-archive/image/public signature identities, reject drift.
   Freeze provenance into a truthful release commit and runtime attestation; verify
   that every packaged source path equals that commit's bytes. Preserve RC25 admin
   backup UI and existing env/Fernet/Telegram/provider secrets exactly.
2. Refresh live inventory and owner configuration, monitor timers/backup locks,
   existing canary allowlist, disk/memory and source freshness. Do not silently
   overwrite a new pending/active revision or reuse an old receipt.
3. Run and actually restore a current encrypted0008 pre-migration backup. Acquire
   the SAME backup/control flock used by existing cron jobs; coordinate monitor
   maintenance/HOLD through a bounded reviewed handoff. Do not let watchdog falsely
   declare downtime healthy or clear HOLD automatically. Stop/drain all application
   writers before migration, including owner/admin write routes.
4. Run additive0009 using the exact candidate. No downgrade or automatic legacy
   conversion. Verify actual reflected schema/head and retained legacy data.
5. Publish approved schema0009-aware backup host code, retaining original TLBK1
   decryption, all rolling7day copies and current configurable retention. Produce
   a NEW consistent production0009 encrypted copy, persist/readback and actual
   isolated31table restore before candidate workers resume. Old copies stay listed.
6. Publish candidate runtime attestation and clone unchanged owner config into the
   actual next revision. Enable MY_DAY_ENABLED/CALENDARS_ENABLED only for bot;
   workers/admin retain those flagsOFF. Keep source/provider/catalogue and sending
   policies unchanged. Run exactly ONE approved pending-revision preflight; failure
   means no activation/start, not repeated probes until a desired result appears.
7. Independent reviewer checks concrete preflight/backup/migration/provenance
   evidence. Only complete PASS permits activation. Configure exact monitor images,
   revision and externally observed baseline under reviewed handoff, never learn
   an arbitrary drifted image. Retain backup and resource freshness guards.
8. Controlled canary uses only the existing operator allowlist. Confirm menu/MyDay,
   calendar creation/visibility/export and existing homework through real human
   interactions, with no automatic invitation or broadcast. Restart-safe alert
   baseline remains UNKNOWN/STALE until validated fresh snapshots, never all-clear.
9. Resume ordinary approved public bot/workers in the established order; verify
   applied revision, health, provider freshness, outbox/heartbeat/monitor and fresh
   cron pulse. A health-only check is not Telegram E2E or release completion.

## Rollback constraints

Before0009, restore prior images/config/revision only after identity/guard checks.
After0009, NEVER blindly start the old RC24/RC25 runtime manifest: it pins0008 and
must fail closed on0009. A functional rollback should retain the additive tables,
disable bot-only calendar/MyDay flags and use an independently reviewed compatible
signed rollback image/manifest. Keep writers stopped if such artifact is unavailable.
Destructive downgrade or replacing production with an old backup requires separate
explicit maintenance/data-loss approval; web restore intent is not that approval.

This packet describes required checks, not an executed transport or release PASS.
