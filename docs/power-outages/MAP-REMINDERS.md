# Cached electricity-schedule reminders

Requested by the owner on2026-11-04 after confirming the corrected graph.
Implementation uses the existing centrally refreshed Alerts Energy API cache;
no per-user API polling or extra source requests are introduced.

## User behavior

- Explicit opt-in under electricity notification settings. Existing selections
  stay disabled; opening settings or editing a lead does not enable delivery.
- Default10minutes before a planned outage and restoration, separately editable
  with presets or custom0–180minutes. Existing saved choices are preserved.
- 🔴⏰ Planned outage time, ⏳ duration, 🟢 planned restoration when its boundary
  is actually supported. Unknown end times are not invented.
- 🟢 Restoration reminder includes the next known outage, if continuous
  validated coverage supports it, and 🔋📱 advice to charge phones/powerbanks
  after electricity returns. Wording always describes the schedule.
- 📅 A changed graph for today or tomorrow includes its explicit date and the
  full current day graph. First observation seeds a baseline without a blast;
  unchanged fetch timestamps or changes to another group do not notify.
- No source service names, URLs, raw addresses, secret values or internal
  synchronization status appear in these notifications.

Positive leads have a60-second scheduling tolerance starting at the requested
send time and always expire before the transition. Zero lead means a
schedule-qualified notification at the boundary, with a maximum60-second
tolerance afterwards. Missed windows are dropped. Missing historical data at
midnight can suppress a reminder rather than manufacture a transition.

## Persistence and dispatch

Schema0009 tables only: version2 actor-owned `power_api_user_pref:<UUID>`,
private `power_api_reminder_state:<UUID>` cursor and existing transactional
outbox. Version1 preferences remain disabled. Dormant reminder defaults for
legacy users are shown only in an explicitly capable settings preview; no
save/activation happens until confirmation. Deliberate version2 exclusions
are retained. Account deletion removes the exact user's cursor, preferences
and all `power_map` outbox states atomically.

New `POWER_MAP_*` events use aggregate `power_map`, separate from the unchanged
official-provider notification policy. Unique keys deduplicate transitions;
monotonic change generations distinguish repeated A→B→A→B graph edits.
The sender revalidates current recipient, opt-in revision, credential revision,
exact claimed intervals, scoped evidence hashes and fresh source/cache checks under locks before
bounded delivery. It checks the clock again after locking. Invalid, stale,
missing, emergency or conflicting data cannot promise restoration. Permanent
recipient failures disable map reminders only. Uncertain Telegram outcomes
remain terminal and are not automatically replayed; exactly-once delivery is
not claimed.

Reminder evidence covers the time intervals and known boundaries actually
asserted by that message, rather than unrelated whole-day snapshots. Publishing
tomorrow must not cancel an unchanged due reminder for today. Newly available
next-outage information may leave an older conservative omission valid;
previously asserted times must still match. Claimed outbox payloads are never
rewritten while an in-memory message could already be rendered for sending.

## Enablement boundary

`POWER_MAP_REMINDERS_ENABLED` defaults tofalse and requires the power feature
and API flags. Planning is a separate cache-only role with a singleton lease;
delivery retains the existing sender's owner revision, allowlist, rate limit,
cooldown and durable dispatch marker. A bot-only canary may save preferences
only if its verified signed scope explicitly enables that capability. Such a
preview composes no background planner or sender and proves no delivery.

LOCAL tests and the previous graph-only canary do not establish production
notification readiness. Required next evidence: exact head0009 image backport,
isolated Linux/PostgreSQL planning/dispatch races and restart checks,
independent safety review, then a separately scoped allowlisted notification
check before permanent rollout. Current production delivery is not enabled by
this document.
