# Phase 12 verification

Verdict: PASS for LOCAL read-only slice only. Recorded 2026-10-02 20:33 UTC.
Integrator root; independent reviewer /root/cohost_safety, C12-04, read-only.
This is not a release approval, staging canary or production E2E verdict.

## Root checks

- `.venv/Scripts/python.exe -m pytest -q`: 1086 passed, 22 skipped, 1 warning,
  49.93s. Skips explicitly require Linux permissions/flock semantics or provisioned
  isolated disposable PostgreSQL fixtures; none are counted as passes. Warning:
  existing Starlette TestClient/httpx deprecation. No production DB was used.
- `.venv/Scripts/ruff.exe check src tests scripts`: all checks passed.
- `.venv/Scripts/mypy.exe src/time_tl`: no issues in 79 source files.
- Root inspected actual config/menu/facade/router/runtime diff plus new domain and
  renderer files; unrelated dirty RC25/admin/agent changes preserved.

## Independent checks and findings

Reviewer independently reran domain calendar41 + renderer23 + integration34 =98
PASS, and legacy alerts/workers/schedule/async facade/public simulation133 PASS.
Focused Ruff PASS and mypy six reviewed source files PASS.

Two review findings closed before freeze: disabled My Day must not register handlers
that clear HomeworkFlow:text; enabled My Day repository failure must respond with
explicit unavailability even in ExistingAccountMiddleware. Persistent routed tests
prove /today and /now still reach legacy homework text flow when OFF, narrow enabled
Message/Callback fallback, unchanged other/disabled error paths and deleted-account
handling without recreation. Other tests cover optional homework errors/timeouts/
oversized preview, owner isolation, no writes and default-off bot-only configuration.

Reviewer confirmed no worker/alert/outbox changes and no false all-clear/join claims.
No reviewed fixes were implemented by the reviewer. Frozen files verified by root:

| File | SHA-256 |
| --- | --- |
| src/time_tl/domain/calendars.py | 4cefa0d8a7fe89d65600c3a5bf4478466ec6b1c2b597e8112cca67b0f7e6c457 |
| src/time_tl/bot/my_day.py | 8af294c50322e65160c1fb218a0e1e1849069299b2c53fd6f342f88bcd0c436a |
| src/time_tl/bot/async_service.py | ae6e369a577a6570579f7c07edf23ddffe24db13b7618f377ea5f3aeff0e6cd4 |
| tests/bot/test_my_day_integration.py | db1d503f687d0f75499aadc9cf0d2cac42d87c274c6fdac470ccf82dbacf6a72 |

## Not verified or delivered

Persistent calendars/memberships/classes/invitations, roles, shared homework,
calendar-backed notification delivery, schema migrations and backfill are not
implemented here. Legacy inactive profiles are not subscriptions. First read bridge
uses only the existing active profile. Production flag remains unchanged/default OFF.
No live server check, deployment, Telegram send, preflight or activation this turn.
LOCAL fixtures and an independent LOCAL PASS do not prove G6-G9 or complete rollout.
