# Phase13 context

User: "давай продолжай дальше" after phase12 LOCAL completion. Implement first
persistent calendar slice under ADR0013, preserving RC25 and all dirty unrelated work.
Source new-concept document and ADR0012. Phase12 has matching plan/summary and scoped
LOCAL verification; old v1 planning labels are historical, not current live evidence.
gsd-progress routing uses direct artifacts because gsd-sdk is unavailable on PATH.
No commits, production/staging mutations or live Telegram sends.

First slice: durable calendars, explicit targeted invitation/confirmation, role checks,
CAS, hiding/leaving/deleting, bounded read bridge. UI management and personal event
overrides remain further slices; do not claim full phase13 based on this foundation.
