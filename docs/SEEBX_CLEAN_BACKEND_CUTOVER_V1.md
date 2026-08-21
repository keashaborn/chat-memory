# SeeBx clean backend cutover preflight v1

This is the single read-only readiness report for moving the running SeeBx backend from the historical memory stack to the canonical Zep-backed candidate. It does not install migrations, alter Postgres, edit cron, restart services, deploy code, or delete schemas.

## Gates

The report combines:

- exact Python and package pins from `pyproject.toml`;
- absence of the retired `eval_all_users.sh` cron command;
- presence of `conversation_sync_private.zep_turn_outbox`;
- both chat-clear functions bound to the Zep outbox and free of legacy memory references;
- absence of the four legacy source-erasure triggers;
- zero nonterminal legacy ingest and erasure jobs;
- presence of the canonical telemetry-retention function;
- exact legacy schema table shape; and
- SHA-256 bindings for a fresh scoped backup and its disposable restore receipt.

It reports two separate decisions. `candidate_deploy_ready` covers the safe application cutover. `legacy_schema_retirement_ready` additionally requires the exact historical schema shape plus backup and restore evidence. Application deployment does not authorize schema deletion.

## Execution

Run it on SeeBx using the candidate release environment with `POSTGRES_DSN` loaded from the protected service environment:

```sh
python scripts/verify_clean_backend_cutover.py \
  --backup-sha256 "$BACKUP_SHA256" \
  --restore-receipt-sha256 "$RESTORE_RECEIPT_SHA256"
```

Exit code `0` means the candidate deployment gates pass, `2` means the system is not ready, and `3` means the preflight itself could not complete. The JSON report never emits the DSN, cron contents, database rows, message text, or credentials.

## Privileged inspection boundary (candidate v2)

The preflight now requires two distinct database identities. `POSTGRES_DSN` remains the normal `brains_app` application identity and must continue to have no read or write authority over the private legacy queues. `LEGACY_MEMORY_INSPECTION_DSN` must resolve to the dedicated `lifeswitch_retirement_auditor` identity, which is read-only, has no direct evidence-table authority, and can execute only a count-returning retirement evidence function. The verifier rejects a shared DSN, a shared role, write-capable inspection, or widened application access. The candidate security package `ops/security/20260821_legacy_memory_retirement_security_boundary_v1` provisions the inspector with no base-table grants and exposes only a content-free, count-only `SECURITY DEFINER` evidence function. It also freezes an AWS Secrets Manager recovery role and policy contract; neither the database package nor AWS resources have been applied.

The auditor role does not yet exist and no grants were changed by this candidate. Creating its credential and exact read-only grants is a separate production database/security operation.

Retirement readiness additionally binds the attestation reconciliation receipt, encrypted-quarantine hash, AWS Secrets Manager key-custody receipt, dependency-catalog hash, and the exact backup/restore hashes. Missing evidence keeps deployment and retirement decisions separate and fail-closed.

## Candidate verification evidence (2026-08-21)

- Base candidate commit: `dcf7b2092800b3d875a72fdd9d1c264b5f649c05`; production was not edited.
- Key-custody schema SHA-256: `442ae5d7006e860cd4a5eab999ee268ab8f6536d8625d4d2c52c2fbf3f66c1ce`.
- Reconciliation tool SHA-256: `cf35c1dd0885ae11c05d1e44e347b7f85193758f59e77193b8911a65e8c7028b`.
- Clean-backend preflight SHA-256: `87e8a861de1734aa8adb37a607c61172e66858eec764503f82d32f24e9d21e90`.
- Executable retirement SQL SHA-256: `00a82f89c47c68002cae61546adafcf5264b3507f2f296dd22829d09cf1f19e3`.
- Focused custody/preflight/retirement tests: 17/17 pass.
- Full backend suite in the verified pinned Python 3.12/Pydantic 2.12.3 runtime: 1,235/1,235 pass.
- Disposable PostgreSQL 16 positive execution: exact 160-table `memory`, 7-table `memory_ingest_private`, and 190/32/158 attestation fixture passed every assertion; only the two legacy schemas were dropped; Zep outbox and both chat-clear functions remained.
- Disposable PostgreSQL 16 negative execution: an external `public` view produced `external legacy dependencies remain`; the transaction aborted and both legacy schemas and the outside view remained.
- Package hashes, Python compilation, and `git diff --check` pass. No AWS secret, live database role/grant, migration, schema, service, frontend, or production source was changed.
