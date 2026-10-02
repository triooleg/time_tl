# Phase13-01 verification

Recorded2026-10-02 20:58 UTC. LOCAL foundation PASS; release REVIEW.
Root integrator; C13-D domain/C13-B persistence owners; independent C13-S reviewer
/root/backup_admin_ui assigned read-only (did not implement reviewed calendar code).

## Actual root checks

- `.venv/Scripts/python.exe -m pytest -q`:1264 passed,23 skipped,1 warning,39.32s.
  Skips are Linux-only permissions/flock plus explicitly isolated disposable PG
  fixtures, including the new calendar fixture. None counted as passes. Existing
  Starlette TestClient/httpx warning is not a new feature failure.
- `.venv/Scripts/ruff.exe check src tests scripts migrations`:PASS.
- `.venv/Scripts/mypy.exe src/time_tl`:PASS,81 source files.
- `.venv/Scripts/python.exe -m alembic heads`:single0009_calendar_foundation head.
  Static migration graph only: NOT an actual upgrade/head of any live database.
- Focused final schema/repository62PASS/1SKIP; earlier domain93PASS and root integration/
  runtime regressions pass. Actual source imports resolved inside current workspace.
- Root inspected actual domain/ORM/repository/migration/config/facade/runtime diff.
  Legacy account deletion, worker/provider/outbox sources unchanged.

Independent C13-S reran218 focused tests with real-PG deliberately deselected,
separately ran runtime opt-in constructor test and Ruff PASS. Findings were fixed by
implementation owners, rechecked and closed; see13-01-REVIEW.md. LOCAL PASS does not
prove PostgreSQL row-lock semantics, real reflection, FK cascades or full migration.

## Frozen SHA-256 (verified by root after implementation freeze)

| File | SHA-256 |
| --- | --- |
| domain/calendar_memberships.py | 1983cedc1cd24815b896ee2f94200cc59afd962044c4d7a65dd116b32eb19d0e |
| infrastructure/db/calendars.py | d1bcce42a89a57f011871226f2f1166701da59d506ec96de47344b43e7aea202 |
| infrastructure/db/models.py | 746ae256ee79b2ed680b8586aa2a597891da3deee105368589057864ecb4f1f0 |
| migrations/versions/0009_calendar_foundation.py | 5bfc4dc32b3f900b664b00ad22feba5cfd84421e0033e495de37510a54980166 |
| bot/async_service.py | 794f111e733067dc3c852af24ce672caf557f4a3dccc0f4e1bf66e5d6cf4f33e |
| config.py | 4ac4eaff379e6a5fe994d9254c5d8940c938efc4620a4a3e3ebbbd136133c10a |
| runtime/services.py | 3d48ecf988b71ea98dd9333430631e44895544a1f1bab6de8c0fce31dd8b1cf2 |
| tests/infrastructure/test_calendar_repository.py | 3c4bc96765ff084468f8e0a8676190a3fb349af976b25057bd57de862dc3f0ce |
| tests/persistence/test_calendar_schema.py | 27bbf35dab0573d2c68aad2abb9dfc6e1933b8d77d13fb14750b48c15371864e |
| tests/bot/test_calendar_read_integration.py | 83baaec3832d0ba9d2fe4d84782f8141e9fbb10f84275cf06e80c59e087a3b3b |

Source paths above are relative to src/time_tl except migrations/tests. This is a
development freeze, not a signed release archive, commit or image attestation.

## Evidence limits / required before release

1. Real isolated disposable PostgreSQL: fresh bootstrap and0008→0009 upgrade,
   reflection/parity, incompatible-schema rejection, concurrent quota/CAS/invitation
   confirmation and actual owner/member user-delete cascades. Local Docker/psql/
   postgres not found; no server used as substitute. Existing optional test was skipped.
2. Actual backup isolated restore including new three tables/head0009, verifier/admin
   compatibility and safe flagsOFF rollback. RC25 backup verifier still pins0008.
3. User-facing management UI/confirmation, export/retention, targeted staging tests
   and pinned exact-release approval. New calendars not usable publicly yet.

CALENDARS_ENABLED and MY_DAY_ENABLED default false; deployed configuration unchanged.
No server/Portainer/DNS/SSH/password/key mutation, production migration, preflight,
activation, Telegram send, signed package generation, Git commit or push this turn.
