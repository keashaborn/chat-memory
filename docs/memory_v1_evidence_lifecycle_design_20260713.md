# Memory V1 evidence lifecycle design

Status: isolated implementation; not applied to production
Server: seebx backend
Schema: `memory`
Migration: `ops/sql/20260713_memory_v1_evidence_lifecycle.sql`

## Scope

This design makes `memory.evidence` insert-only for the runtime role. It provides
one owner-scoped, audited path that can clear evidence content and advance its
status to `redacted` or `deleted`. It does not physically delete an evidence row
online.

This lifecycle is intentionally narrower than account erasure. Evidence created
from `public.chat_log` still has a source copy. Artifact content and sections may
also contain the same text. Claims and Qdrant projections may contain normalized
derivatives. The audit event records dependency counts and whether a chat source
copy is expected so an operator cannot mistake an evidence tombstone for complete
erasure.

## Invariants

1. `brains_app` has `SELECT, INSERT` on `memory.evidence`; it has no `UPDATE` or
   `DELETE` grant.
2. A trigger rejects physical `DELETE` for every normal execution path, including
   the table owner. It rejects every `UPDATE` except a lifecycle transition made
   as the no-login `memory_evidence_maintainer` role.
3. Evidence identity, owner, provenance, hash, timestamps, scores, sensitivity,
   and metadata never change. A lifecycle transition may only set `content=NULL`
   and advance the status one way.
4. New evidence must start `active` or `quarantined`. A deleted external identity
   cannot be reinserted or resurrected.
5. Lifecycle audit events are append-only, contain no raw evidence text, and are
   unique by `(owner_user_id, request_id)`.
6. The lifecycle function derives the owner from
   `memory.current_actor_user_id()`. It accepts no owner argument. Forced RLS
   remains active because its owner is a no-login, non-superuser, `NOBYPASSRLS`
   role.
7. A candidate can be created, approved, or applied only while its evidence is
   active. The database trigger takes a key-share lock on that evidence, making
   candidate promotion and lifecycle transition mutually serialized.
8. Retrieval includes only active evidence references. A claim with no active
   evidence receives `no_active_evidence` and cannot enter the memory packet.

## Status transitions

| Current | `redact_content` | `delete_tombstone` |
|---|---|---|
| `active` | clear content, `redacted` | clear content, `deleted` |
| `quarantined` | clear content, `redacted` | clear content, `deleted` |
| `redacted` | audited no-op | `deleted` |
| `deleted` | audited no-op | audited no-op |

All actions preserve `content_sha256`, `evidence_id`, source identity, and audit
provenance. A new request against an already-terminal row creates a distinct
`already_redacted` or `already_deleted` audit event. Replaying the same request ID
with identical inputs returns the original event. Reusing that request ID with
different inputs fails.

## Role and function boundary

`memory_evidence_maintainer` is `NOLOGIN`, `NOSUPERUSER`, `NOINHERIT`, and
`NOBYPASSRLS`. It owns only the two security-definer functions that require
elevated table access. Its grants are limited to:

- `SELECT, UPDATE` on `memory.evidence`;
- `SELECT, INSERT` on `memory.evidence_lifecycle_event`;
- read-only access to evidence dependency tables;
- execution of `memory.current_actor_user_id()`.

`brains_app` is not a member of this role. It receives `EXECUTE` only on:

```sql
memory.transition_evidence_lifecycle(
  evidence_id uuid,
  request_id uuid,
  action text,
  reason_code text,
  actor_type text DEFAULT 'user',
  actor_ref text DEFAULT NULL,
  metadata jsonb DEFAULT '{}'
)
```

Both security-definer functions fix `search_path=pg_catalog` and fully qualify
schema objects. The migration revokes public execution.

## Audit event

`memory.evidence_lifecycle_event` retains:

- owner, evidence ID, request ID, action, outcome, and reason;
- prior and resulting status;
- retained SHA-256 and whether content existed before the action;
- actor context and database session role;
- claim, candidate, and artifact dependency counts at transition time;
- whether `public.chat_log` is expected to retain a source copy;
- bounded JSON metadata with no raw content supplied by this API.

There is deliberately no foreign key from the event to `memory.evidence`. This
allows a future offline physical-purge process to remove a tombstone without
destroying or blocking its audit record.

## Immediate downstream behavior

- Redaction/deletion blocks new candidate creation and later approval/application.
- Existing claims remain in the relational fact field for reassessment and audit,
  but retrieval refuses them when they have no active evidence.
- Qdrant may still return an obsolete claim ID as a semantic candidate. The
  authoritative Postgres packet builder rejects it before prompt construction.
- Artifact and source copies are not changed by this function.

## Separate coordinated-erasure phase

Complete user-data erasure requires a later owner-scoped workflow that inventories
and acts on all copies in one request ledger:

1. `public.chat_log` / `chat_messages` source rows;
2. `memory.artifact` and `memory.artifact_section` raw content;
3. claims whose remaining support becomes empty;
4. `memory_claim_v1` Qdrant projections and pending outbox work;
5. backups, retention windows, and completion attestations.

That workflow must not reuse the evidence-only function as proof of complete
erasure.

## Validation gate

Before production deployment:

1. Apply the migration twice to a disposable PostgreSQL 16 database.
2. Run `tests/memory_v1_rls.sql` and
   `tests/memory_v1_evidence_lifecycle.sql`.
3. Run the full Memory V1 Python integration suite.
4. Verify the runtime role cannot update/delete evidence or mutate audit events.
5. Verify an actor cannot transition another owner's evidence.
6. Verify tombstoned evidence cannot be reinserted, promoted, or retrieved.
7. Take a production backup and record pre/post schema and row-count checks.

No production migration is authorized by this design step.
