# Forward recovery

Rollback drops only `memory.release_owner_projection_outbox_v1`. Apply the schema
authority package first, then reapply the exact function creation package in one
short governed transaction. Before rollback, verify there is no in-flight call.
Release receipts and outbox transitions are append-only production history and
are not deleted by recovery.
