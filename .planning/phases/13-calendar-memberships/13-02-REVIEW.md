# Independent C13-S2/S3 review

Reviewer: separate read-only /root/backup_admin_ui, reused for safety review; no
implementation, production writes or private-key access by reviewer.

Final verdict LOCAL PASS; disposable PostgreSQL lab PASS; candidate public
provenance PASS; production release REVIEW (not deployed/activated).

Findings implemented by other owners and rechecked: reserve global/issuer/target
invitation capacity across awaits; bound operations and release cancellation
reservations; clear only calendar-owned FSM on navigation away (/start,/menu/home);
preserve foreign/legacy homework state behavior. No remaining local blocker found.

Independent tests: 437 PASS, 12 Linux SKIP and one real-PG test deselected; scoped
Ruff PASS. Final real lab receipt ae0a715a independently compared to a recomputed
archive:14checksPASS,0009/31tables. Actual encrypted restore old0008 28/3305 and
newsynthetic0009 31/73 PASS. All backup-code hashes match frozen receipt.

C13-S3 independently verified exact-byte Ed25519 signature using only the compiled
public anchor, source/host archive SHA and each file, lab/image/publication receipts,
imageIDb5d57d21/digest5d0c094d and hash of94installed source checks. Candidate
honestly claims new_source_commit=null and runtime_attestation=false; signing is
not G8 authorization. Public manifest/signature are separate from private key.

Remaining gates: exact approval, local release provenance/commit and runtime
attestation, coordinated backup-lock maintenance/migration, a newly verified
production0009 backup, independent preflight acceptance and approved allowlisted
canary. Old0008 signed runtime cannot be used unchanged after0009; retain additive
schema and use a compatible reviewed rollback artifact or stop writers. See
docs/operations/RC26_RELEASE_PLAN.md; no production release PASS claimed.
