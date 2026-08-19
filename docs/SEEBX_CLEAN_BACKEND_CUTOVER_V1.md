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
