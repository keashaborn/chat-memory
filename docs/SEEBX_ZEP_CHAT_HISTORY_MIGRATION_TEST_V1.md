# Disposable Zep chat-history migration verification

`scripts/verify_zep_chat_history_migration.py` is the executable gate for the
Zep outbox and chat-history cutover. It is candidate-only and does not modify
the production database.

The verifier requires the exact full-database backup and successful restore
receipt produced by the legacy-memory recovery gate. It creates one uniquely
named disposable database on the local SeeBx PostgreSQL cluster, restores that
backup, and captures a baseline of every ordinary table row count, both
chat-clear function definitions, and the four legacy erasure triggers.

It then:

1. moves the telemetry-retention function to its canonical owner without
   executing retention or deleting telemetry rows;
2. applies the hash-bound Zep outbox migration;
3. applies the hash-bound chat-history cutover in one transaction;
4. proves the outbox is empty, both clear functions use only the Zep outbox,
   the four legacy triggers are absent, and no pre-existing table row count
   changed;
5. runs the complete content-free platform consumer and governance audit
   against that forward-state database, builds the exact clean-platform
   disposition, and requires zero `memory_ingest_private` reachable
   dependencies and zero baseline blockers;
6. rolls back the chat-history cutover, the empty Zep outbox, and the
   telemetry owner move in reverse order;
7. proves the exact baseline function hashes, trigger definitions, telemetry
   owner, grants, table inventory, and row counts were restored;
8. drops and independently proves removal of the disposable database; and
9. writes a content-free hash-bound receipt.

The verifier never calls Zep and never reads message content into its receipt.
The password is held only in a temporary mode-0600 pgpass file and never enters
subprocess arguments or output. Failures emit fixed error codes and attempt to
remove the disposable database before exiting.

Running this verifier creates and drops a temporary PostgreSQL database and
therefore requires separate production authorization. Applying either
migration to the live `memory` database remains a distinct later approval.

## Verified disposable execution

Candidate commit `0ee2b9fc07ebc63ef7acfa919ff63545296847fb` passed the
complete gate in run `zep-audit-20260822t1058z`:

- output: `/var/backups/seebx-cleanup/zep-chat-history-migration-v1/zep-audit-20260822t1058z`;
- migration receipt SHA-256:
  `2a00eb75e63b8d9cf63c2a028469c4be0ef6e3633f3aee3bda4f77e65c8a1e13`;
- forward platform audit SHA-256:
  `8ba29409fcaf5acb8a686d4fbd892a796c50399ba0314e36a4d18984d40bf2eb`;
- forward disposition SHA-256:
  `0a810208224f56f369462d0faf73c642766acb8723763a5a2b607cbf0ad14042`;
- all three receipts are `root:root` mode `0600`;
- 794 platform objects were dispositioned, with zero reachable
  `memory_ingest_private` dependencies and zero baseline blockers;
- the forward Zep outbox contained zero rows and all four legacy triggers were
  absent;
- rollback restored the exact 210-table baseline hash
  `8cd000e7f540587a85bf4e49401ba18064e5bad9792fdf0629a24c023d8884ef`;
- the temporary database was absent after execution, `brains.service` remained
  active, and `/openapi.json` returned HTTP 200.

This is candidate evidence only. It proves that the migration can remove the
legacy-ingest dependency and roll back cleanly; it does not authorize applying
the migration to production.
