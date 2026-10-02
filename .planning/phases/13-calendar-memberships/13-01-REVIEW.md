# C13 independent review record

Environment LOCAL only. Integrator root, implementation owners C13-D/C13-B/C13-I.
Independent read-only reviewer /root/backup_admin_ui assigned C13-S (did not implement
calendar code). Reviewer has no write paths; fixes belong to implementation owners.
This record is root's transcription of findings, not production evidence.

## Findings and disposition

1. Invitation coexistence: global CAS version originally invalidated earlier tokens
   when creating/accepting another invitation or hiding a calendar. Root identified;
   backend added separate snapshot_version, content-only increments and regressions.
   Reviewer read-reviewed and independently reran coexistence simulation. CLOSED LOCAL.
2. Schema attestation: FK validation omitted referred_schema and index validation
   omitted predicate/include/expression/options, potentially accepting incompatible
   pre-existing schema. Reviewer identified; backend tightened validation and tests.
   Reviewer confirmed source fix and independently reran final schema regressions.
   CLOSED LOCAL; actual PostgreSQL reflection remains unverified.
3. Constraint normalization: quoted literals and NOT/AND grouping must retain SQL
   semantics. Conservative token canonicalizer now preserves both; independent four
   pure semantic probes passed. A new default-literal test then found unquoted String
   server_default.arg interpreted as a lowercased identifier, accepting 'member' for
   'MEMBER'. Root and reviewer independently reproduced; backend fixed expected
   String defaults as quoted/escaped SQL literals without removing literal quotes
   or case. Regression passed in root62 and independent218 focused tests. CLOSED LOCAL.

Additional root privacy change explains owner-account deletion removes shared calendars
for all members; member deletion preserves others' calendars. Flag-OFF warning/menu
behavior unchanged and no deletion logic modified. Reviewer inspected this branch.

Local query simulation serializes all transactions with a test-double lock. It checks
API behavior/SQL predicates/rollback but cannot prove PostgreSQL row-lock concurrency.
Actual migration/reflection/concurrency/deletion/backup verification remains required
before any release, regardless of subsequent LOCAL PASS.

Final C13-S verdict: LOCAL PASS / release REVIEW. Reviewer independently ran218
focused tests, deliberately deselected one real-PG fixture, separately passed runtime
opt-in constructor test and Ruff. Root fullsuite1264PASS/23SKIP, Ruffall and mypy81PASS.
