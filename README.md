# Time TL

Time TL is a safety-first Telegram assistant for Ukrainian online students. It
combines a student-managed lesson schedule with official alerts.in.ua oblast status
and reminds the student to return only after a validated all-clear while a lesson is
still active.

## Current status

The schedule engine, Telegram menus, PostgreSQL persistence, one global official
alert poller, transactional outbox, schedule/sender workers and owner admin dashboard
are implemented and locally tested.

This is not a deployed production release. Coordinate resolution is deliberately
closed; users can only confirm an oblast manually. Production/provider and worker
capability gates remain false until PostgreSQL restart/fault evidence, allowlisted
Telegram staging, server backup/restore and exact-version approval are complete.

Verified LOCAL evidence recorded 2026-09-23: 241 pytest tests and one opt-in
PostgreSQL E2E skip; unittest ran 233 tests with the same one skip,
Ruff, strict mypy for 66 source files, compileall, Alembic offline SQL, an eight-
service Compose parse and admin JavaScript syntax all passed. No Portainer deployment,
live admin password or real-user Telegram delivery has been performed.

## Documentation

- [Current gates and evidence](.planning/STATE.md)
- [Bot setup and staging requirements](docs/SETUP_BOT.md)
- [Admin panel operation](docs/ADMIN_PANEL.md)
- [Official source register](docs/SOURCES.md)
- [Portainer exact-version runbook](docs/deployment/PORTAINER.md)

Never commit or print Telegram, alerts.in.ua, PostgreSQL, admin, Fernet or SSH
secrets. Missing, stale, malformed or unavailable alert data is `UNKNOWN/STALE`,
never an all-clear.
