# Time TL release evidence — 2026-09-27

## Scope

This record covers the current release candidate's local verification and an isolated Docker/PostgreSQL check on the mini-server. It is not evidence of a staging update, production deployment, public launch, or live Telegram end-to-end test.

## Verified evidence

| Environment | Check | Result |
|---|---|---|
| LOCAL | Full automated test suite before the server run | `460 passed, 5 skipped` |
| LOCAL | Independent safety remediation review | `PASS` |
| TRANSFER | Exact candidate archive SHA-256 | `CC936E3A47C7CDBD76A869D6E29B6FE811034DF8649EEF0CE7165EE77CC7785A` |
| ISOLATED SERVER | Docker Compose render | `PASS` |
| ISOLATED SERVER | Candidate image build | `PASS`; image ID `sha256:39ebb5b917c7f94dc2074155fcd04ccd47f6298d5bbc5d58a52443d34645f9b` |
| ISOLATED SERVER | PostgreSQL E2E database | `time_tl_e2e_cc936e3a` |
| ISOLATED SERVER | Opt-in PostgreSQL E2E run | `5 passed, 7 warnings`; exit code `0` |
| ISOLATED SERVER | Isolation cleanup | Temporary container, volume, network, image, and transferred files removed |
| EXISTING SERVER | Existing Time TL services during and after the isolated run | Remained healthy |
| RELEASE CONTRACT | `PRODUCTION_PUBLIC_RELEASE_APPROVED` | `False` |

The seven warnings were present in the successful isolated PostgreSQL test run; they are not recorded here as failures, but this document does not reinterpret or suppress them.

## Gate status

| Gate | Status | Evidence or remaining work |
|---|---|---|
| G4 LOCAL QUALITY | `PASS` | Local suite passed with five environment-gated PostgreSQL tests skipped; those five tests subsequently passed in the isolated server environment. |
| G7 SERVER READINESS | `REVIEW` | Compose rendering, image build, isolated PostgreSQL tests, cleanup, and non-impact to existing services were verified. Backup/restore and rollback readiness for the working STAGING stack remain to be demonstrated. |
| G8 DEPLOY APPROVAL | `BLOCKED` | The candidate does not yet have an exact commit and immutable release tag. A controlled STAGING migration/update and explicit approval for that exact version and stack are still required. |
| G9 CANARY | `BLOCKED` | An approved STAGING canary with monitoring and rollback evidence has not been run. Separate explicit approval is required before any public launch. |

## Required next evidence

1. Create and identify the exact release commit and immutable tag.
2. Verify backup and rollback for the working STAGING stack.
3. Obtain approval for, then perform, the controlled STAGING migration and update.
4. Run the approved STAGING canary and capture monitoring and rollback evidence.
5. Obtain exact G8/G9 approval before enabling any production or public delivery.

No production setting, Portainer stack, DNS, reverse proxy, firewall, or live-user delivery was changed by the isolated verification described above.
