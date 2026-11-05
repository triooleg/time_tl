# Time TL

Time TL is a safety-first Telegram assistant for Ukrainian online students. It
combines a student-managed lesson schedule with official alerts.in.ua oblast status
and reminds the student to return only after a validated all-clear while a lesson is
still active.

## Current status

Time TL includes lesson schedules, homework, calendars and a day view. It uses
PostgreSQL to store user settings, cached electricity schedules and weather
forecasts, and the notification queue. The owner dashboard manages settings
and exposes operational checks.

See the [operations timeline](docs/operations/TIMELINE.md) for deployed build
identities, production checks and incident recovery. The version recorded in
the source identifies the package under preparation. Deployment status follows
the evidence for the exact image in each environment.

## Versioning and release notes

- Use Semantic Versioning. Increment `PATCH` for compatible fixes, `MINOR`
  for compatible features, and `MAJOR` for incompatible changes to supported
  interfaces or upgrade paths.
- Keep the versions in [pyproject.toml](pyproject.toml) and
  [src/time_tl/__init__.py](src/time_tl/__init__.py) aligned. Record changes under
  **Unreleased** in the [changelog](CHANGELOG.md) as they land.
- Before publishing, move those entries into a dated version section, verify
  the exact commit, and create an annotated `vMAJOR.MINOR.PATCH` tag on it.
  Use that section for the GitHub release notes. Record the exact deployed
  image and verification separately in the operations timeline.

## Documentation

- [Current gates and evidence](.planning/STATE.md)
- [Bot setup and staging requirements](docs/SETUP_BOT.md)
- [Admin panel operation](docs/ADMIN_PANEL.md)
- [Official source register](docs/SOURCES.md)
- [Portainer exact-version runbook](docs/deployment/PORTAINER.md)

Never commit or print Telegram, alerts.in.ua, PostgreSQL, admin, Fernet or SSH
secrets. Missing, stale, malformed or unavailable alert data is `UNKNOWN/STALE`,
never an all-clear.
