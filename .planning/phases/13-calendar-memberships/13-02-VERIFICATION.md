# Phase 13-02 verification

## Independent and root evidence

- Root full pytest: 1354 PASS / 23 SKIP / 1 existing warning, 46.38s.
- Ruff src/tests/scripts/migrations PASS; mypy82 source files PASS.
- Independent C13-S2: 437 focused PASS; 12 Linux-specific SKIP and one real-PG
  test deselected. Read-only reviewer did not implement reviewed fixes.
- Real frozen disposable PostgreSQL: 14 checks PASS, head0009, 31 exact tables.
  Fresh actual Alembic bootstrap and0008→0009; real reflection/mismatch rejection;
  owned/joined hidden quotas under concurrency; CAS; target/single-use invitations;
  flushed-content and acceptance rollback; self-export; expiry cleanup; deletion
  cascades. Source and backup hashes independently recomputed from actual files.
- Old retained encrypted0008 copy: actual isolated network-none tmpfs restore,
  28tables/3305rows/validated constraints PASS. New synthetic encrypted0009 copy:
  persisted protected readback and isolated restore31tables/73rows PASS. Original
  TLBK1/HKDF/AAD/key preserved; no freshness guard publication or production restore.
- Lab containers/network removed after each completed run; final inventory empty.
  One earlier failed lab cleanup was identity-guarded, diagnosed and removed using
  its exact recorded IDs/labels, never a production or broad filesystem target.

## Frozen candidate, not deployed

Release: time-tl-production-20261003-rc26-calendars-candidate.

Source archive SHA256:
`ae0a715a6eeddd0e54bbcf45e3bb23b0ba2277e75087eb46a0f508a4a7da9e37`.

Immutable local-registry image digest:
`sha256:5d0c094d90bf8c0940f94a5ca10b357ef2322cdfc9c2f339f51a7c63dcca4728`.

Image ID:
`sha256:b5d57d21a91dd6653b7bf88a6e917e3488ce91adfc136a091d8fd927af2ed216`.

Public candidate attestation SHA256:
`779c78f01268c3b9434d086b7d62eedb1f860fce5db794920fc31ba68bc942d2`.

Installed94 source/migration/config/harness files verified in an isolated exact
image. Candidate provenance is explicitly an uncommitted snapshot, NOT a new Git
commit and NOT production runtime attestation. Existing external Ed25519 key signed
the public candidate provenance; private key stayed external and was not transferred.

Evidence: .release/time-tl-production-20261003-rc26-calendars/
SERVER_INVENTORY.json, LAB_ae0a715a6eeddd0e54bbcf45e3bb23b0ba2277e75087eb46a0f508a4a7da9e37.json,
CANDIDATE_IMAGE.json, calendar-candidate.json/.ed25519, source .tar.gz and
rc26-backup-contract.tar. No Git commit/push or production mutation claimed.

## Remaining production gates

Exact G8 approval of candidate and host backup packet, permission to freeze a local
release commit without unrelated dirty changes/push, then truthful runtime manifest
and signature. Fresh server-state/backup verification; approved maintenance under
the SAME backup flock; migration0009; fresh real production0009 dump/restore;
exact monitor/baseline/attestation handoff; exactly one approved preflight for the
actual pending revision; activation only on complete PASS and independent review;
existing operator-allowlist canary before normal public operation, no mass messages.
Suggested next revision15 is not created or activated. FlagsOFF alone cannot run
the old RC24 signed runtime image on head0009: see release rollback plan.
