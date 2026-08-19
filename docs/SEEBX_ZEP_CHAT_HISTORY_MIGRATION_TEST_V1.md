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

1. applies the hash-bound Zep outbox migration;
2. applies the hash-bound chat-history cutover in one transaction;
3. proves the outbox is empty, both clear functions use only the Zep outbox,
   the four legacy triggers are absent, and no pre-existing table row count
   changed;
4. rolls back the chat-history cutover and then the empty Zep outbox;
5. proves the exact baseline function hashes, trigger definitions, table
   inventory, and row counts were restored;
6. drops and independently proves removal of the disposable database; and
7. writes a content-free hash-bound receipt.

The verifier never calls Zep and never reads message content into its receipt.
The password is held only in a temporary mode-0600 pgpass file and never enters
subprocess arguments or output. Failures emit fixed error codes and attempt to
remove the disposable database before exiting.

Running this verifier creates and drops a temporary PostgreSQL database and
therefore requires separate production authorization. Applying either
migration to the live `memory` database remains a distinct later approval.
