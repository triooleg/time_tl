# PostgreSQL restart/fault E2E

`test_postgres_restart_fault_harness.py` is destructive and is skipped by default. It never
contacts Telegram or the alerts provider. The transport records messages in memory.

Use only a disposable, isolated PostgreSQL database whose actual `current_database()` name
contains `time_tl_e2e`. The harness refuses cleanup otherwise, even if the connection URL points
somewhere unexpected.

Set both environment variables before an explicit run:

```powershell
$env:TIME_TL_E2E_DATABASE_URL = 'postgresql+asyncpg://USER:PASSWORD@HOST/time_tl_e2e_test'
$env:TIME_TL_E2E_ALLOW_DESTRUCTIVE = 'YES_DELETE_TIME_TL_E2E_DATA'
.venv\Scripts\python.exe -m pytest tests/e2e/test_postgres_restart_fault_harness.py -q
```

The test validates the database name before applying Alembic migrations, truncating application
tables, or terminating a backend. At the snapshot/outbox/poll-cursor commit boundary it flushes the
real transaction, reads that transaction's `pg_backend_pid()`, and uses a separate connection to
call `pg_terminate_backend(pid)`. A recreated engine must observe that PostgreSQL rolled back all
three writes. The test also verifies restart persistence, fail-closed stale state, two-snapshot
all-clear, return-to-lesson planning, final sender authorization, and outbox deduplication.

The PostgreSQL test role must be permitted to terminate its own backend. Once explicitly opted in,
a missing permission is reported as a failed E2E prerequisite; it is not silently skipped.

Never point this harness at production, staging, or any database containing data that must be kept.
