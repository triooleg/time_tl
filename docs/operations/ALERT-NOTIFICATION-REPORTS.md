# Alert notifications and 22:00 statistics

Status: isolated 0.2.5 release source prepared; production switch is not proven here.

Weather notification bodies contain forecast facts only. Source metadata stays
in PostgreSQL/admin and the existing Weather → Sources and licences view.
Escaped weather strings use Telegram HTML parse mode. Collection, source
priorities, scheduling and weather deduplication remain unchanged.

Licence references: [MET](https://api.met.no/doc/License),
[OSM](https://osmfoundation.org/wiki/Licence/Attribution_Guidelines).

Alert-start messages contain the heading and confirmed territory. Every confirmed
clear additionally contains its individual duration, or explicitly states that
complete data is unavailable. Neither message directs users to enter or leave a
shelter. Official provider validation, hierarchy and all-clear decisions stay in
the existing state machine.

Durations are observed aggregate intervals, marked approximate. Missing starts,
source gaps, restart and territory changes never fabricate an exact duration.
The first valid empty snapshot awaiting confirmation differs from source loss;
only the actual confirmed state-machine transition closes an alert.

## Period and consent

Report time: 22:00 Europe/Kyiv, bounded delivery window until 22:10.
Reporting interval: previous local 22:00 → current local 22:00, including DST.
Retries do not move this boundary. Count new confirmed aggregate starts inside
the interval. Parent and child alerts are not added together. An alert crossing
22:00 has its duration split between reports; its start is not counted again.
Carryover, unknown starts, active-at-cutoff and incomplete coverage are separate.
The first report cannot recover trustworthy pre-installation history.

Only ALERTS_ONLY and LESSONS_AND_ALERTS recipients are eligible. Planning uses the
existing outbox; final delivery rechecks recipient, consent, territory and expiry.
Indeterminate sends retain their durable no-replay boundary.

## Release boundary

Migration 0015_alert_history adds cursor, period and segment tables.
ALERT_REPORTS_ENABLED defaults false. Enabled alert ingestion, report planning and
sender composition check the required migration/schema before starting. History
gap tolerance uses the configured official-source freshness limit. Invalid
derived reports are isolated from lesson planning and other recipients.

Production still uses head 0014. Enabling reports requires an independently
qualified head-0015 build, signed manifest, compatible rollback and release
approval. No production migration or live test send was performed here.

Do not publish the dirty root tree wholesale. Port the reviewed feature delta
onto actual production commit 461b764023815fe656923ea8781dd12dffad6c97, preserving
its weather 07:00/12:00/18:00 slot headings and datetime deadline fix.

## Verified local evidence

- Independent native Linux/PostgreSQL: 12 tests passed, including actual
  two-phase clear confirmation and migration-0015 DDL. The standalone DDL test
  advances the version row itself; this is not a complete Alembic runner proof.
- Final selected application, sender, FSM and persistence regressions: 166 passed.
- Ruff passed for changed source/tests; three new modules passed scoped strict
  Mypy with imported modules skipped (not a whole-project type-check claim).
- Independent safety review: PASS for local implementation, not deployment.

During final checks models.py was found to contain 55,559 NUL bytes; other 418
scanned code/document files were intact. Restored the frozen baseline plus three
new history classes. All 47 class semantic bytecodes matched the surviving
pre-corruption Python cache; the 44 existing classes and module imports also
matched the baseline. Evidence is in
`.release/time-tl-0.2.5/repair-models-20261107/`.
The 166-test run and lint/type checks above were repeated after recovery.

An internal source checkpoint (not a deployable release) is saved at
`.release/time-tl-0.2.5/alert-notification-feature-local-checkpoint.zip`, SHA-256
`b53b9a8b942267bd260de5c01315ab68100f16fcae2c3f0a49d008826fcf5cbf`.


## Isolated release source

The release is ported onto production commit
`461b764023815fe656923ea8781dd12dffad6c97`, branch
`codex/alerts-report-release-20261107`. Production lesson/scope binding checks,
weather 07:00/12:00/18:00 helpers and the typed delivery-deadline fix are retained.
Root-only unrelated modifications are excluded. Only the release worktree compiles
schema head0015; previous approved source remains head0014.

Initial release qualification: 351 selected tests passed (one unrelated database
storage test skipped without its explicit DSN); new modules passed scoped strict
Mypy with imported modules skipped. Native Linux and full migration runner
results are recorded by the release orchestrator before deployment.


Release-source native qualification passed: 13 PostgreSQL tests on isolated
Linux, including the real Alembic command runner upgrading a complete head0014
schema to0015, repeating upgrade, downgrading to0014, and upgrading again.
User rows remained intact and no historic alert data was backfilled. A separate
native Linux run passed all41 encrypted-backup unit tests, including permission
and locking checks skipped on Windows. Initial backup tests used an undersized
128MiB tmpfs and correctly hit the free-space guard; rerunning with768MiB passed
without changing production logic. These tests do not replace restoration of
the real production encrypted backup or the server canary.

AST preservation checks confirmed unchanged production weather slot helpers,
planner, schedule-scope binding helpers, schedule repository, sender run loop,
provider and alert FSM. The weather database policy file is byte-equivalent to
the production base, preserving commit461b's datetime deadline fix.
