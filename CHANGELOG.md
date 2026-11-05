# Changelog

Time TL records user-visible changes and fixes by package version. The
[operations timeline](docs/operations/TIMELINE.md) records build identities,
deployments and their verification.

## Unreleased

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
