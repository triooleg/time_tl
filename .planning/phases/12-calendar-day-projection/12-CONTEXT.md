# Phase 12: Calendar foundation and My Day

User approved continuing after RC25 UI acceptance. Source: pasted new-concept document
ed177394-586b-4444-a3d7-ae685891bc13 and subsequent staged plan in conversation.
Goal of this first slice: usable read-only My Day from the existing active schedule,
plus tested pure multi-calendar projection; no silent schedule migration.

Confirmed boundaries: preserve production bot/monitor/backups; no server mutations,
credentials or key changes; OCR and Moiashkola excluded. No live Telegram tests.
Existing compact menu evolves incrementally, not by removing working features.
Full subscriptions/shared calendars require the next persistence slice.

Current roadmap and PROJECT.md retain historical v1 assumptions/statuses. Actual
RC24/RC25 operational evidence and the user's acceptance outrank those stale labels.
gsd-progress routing used direct project artifacts because gsd-sdk was not available
on PATH; no invented structured progress percentage or phase summary was produced.

Architecture contract: ../../adr/0012-calendar-day-projection.md.
