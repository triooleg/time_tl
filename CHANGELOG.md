# Changelog

Time TL records user-visible changes and fixes by package version. The
[operations timeline](docs/operations/TIMELINE.md) records build identities,
deployments and their verification.

## Unreleased

## 0.2.5.post3 — 2026-11-25

- Notify opted-in users when the cached outage schedule status changes for their operator, group and day.
- Show the published schedule for the requested day when the provider omits its publication timestamp and the cache was checked recently.

## 0.2.5.post2

- Show the alerts.in.ua level on alert-start messages when the provider supplies a recognized level.

## 0.2.5.post1 — 2026-11-12

- Notify opted-in users when fresh cached Alerts Energy data shows emergency
  restrictions starting or a valid schedule returning for their operator and group.

## 0.2.5 - 2026-11-07

- Keep weather notification bodies compact and source-free; preserve the
  existing source/licence view and 07:00, 12:00 and 18:00 bulletin schedule.
- Show an observed, approximate duration on each confirmed all-clear, with an
  explicit missing-history label when a trustworthy duration is unavailable.
- Remove shelter instructions from alert-start and all-clear notification text.
- Add consent-bound reports around 22:00 for the previous local 22:00-to-22:00
  period: new alerts, observed total duration, carryover and incomplete history.
- Persist aggregate alert history in migration 0015 and retain delivery
  tombstones across territory changes. Report/history processing is disabled
  by default until the signed production release enables it.
- Validate encrypted backup restores for schema 0015 without changing retained
  backup keys, encryption format or earlier schema inspectors.

## 0.2.4 - 2026-11-06

- Show the Kyiv date next to the start time in the My Day summary.

## 0.2.3 - 2026-11-06

- Compact Telegram weather cards with rounded temperatures, local precipitation times,
  significant wind gusts, and brief recommendations.
- Bind scheduled lesson reminders to the confirmed alert territory in combined mode.
- Retain the full operator schedule needed to validate opted-in electricity reminders.
- Send opted-in weather bulletins at 07:00, 12:00, and 18:00 Kyiv time.

## 0.2.2 - 2026-11-05

- Explain the enabled bot features in the Ukrainian Help menu.

## 0.2.1 - 2026-11-05

- Show temperatures and expected precipitation for night, morning, midday and evening in Telegram weather forecasts.

## 0.2.0 — 2026-11-05

### Included features

- Telegram includes the day view, lesson schedules, homework and calendars.
- Electricity schedules use a shared Alerts Energy API cache. Weather forecasts
  use a shared PostgreSQL cache for the user's selected locality.
- Weather cards show precipitation by date and distinguish the current
  temperature from the daily range. Weather settings show individual
  notification types and local time.

### Changes

- Bind queued alert, all-clear and return-to-lesson messages to the verified
  alert transition so recovery or a later transition cannot revive an old
  notification.
- Distinguish expected cancellations from delivery failures. The new monitor
  records the first HOLD reason and which services were confirmed stopped.
- Show active electricity, weather and calendar notifications in the Telegram
  home menu.
- Add opt-in reminders 10 minutes before timed events in personal calendars.
  Calendar reminders require a signed capability on the bot, planner and sender.
- Add migrations for alert transition binding and calendar consent. Verify both
  additions during backup restoration.

## 0.1.0

- Initial package version. Included the local schedule engine, official-alert
  integration, Telegram menus, PostgreSQL persistence and outbox, background
  workers, and the owner dashboard.
