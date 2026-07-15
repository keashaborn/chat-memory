# Memory V1 relational writer V5

Status: designed and clone-verified; not installed or runtime-active. Server: seebx backend.

## Security boundary

`memory_v5_writer` is a `NOLOGIN`, `NOSUPERUSER`, `NOCREATEDB`, `NOCREATEROLE`, `NOINHERIT`, `NOBYPASSRLS` role. `brains_app` is not a member. The role owns the five controlled APIs and receives only the table and helper-function privileges required inside those functions.

Every controlled API is `SECURITY DEFINER`, has `search_path=''`, uses schema-qualified relations, requires `session_user='brains_app'`, and requires a transaction-local `app.user_id`. Owner tables use forced RLS with policies scoped only to `memory_v5_writer`. `brains_app` has no direct access to V5 owner tables or the two writer audit tables.

Function execution is revoked from `PUBLIC`. `brains_app` can execute only:

1. `memory.stage_relational_packet_v5(...)`
2. `memory.preflight_entity_resolution_review_v5(...)`
3. `memory.review_entity_resolution_v5(...)`
4. `memory.preflight_entity_resolution_apply_v5(...)`
5. `memory.apply_entity_resolution_v5(...)`

The application supplies no owner argument. Ownership comes only from the authenticated actor setting established by the backend request transaction.

## Append-only audit state

`memory.relational_stage_batch` retains the exact raw extraction and resolver packet text, verifies each stored text value against its SHA-256 at the table boundary, and records the evidence ID, extractor identity, inserted row counts, and deterministic result map. This preserves comparison hints, deferrals, and findings even when they do not yet have specialized relational tables. Its primary key is `(owner_user_id, batch_id)`.

`memory.relational_operation_request` records the owner-scoped request ID, operation, target, authorization manifest, and result. Its primary key is `(owner_user_id, request_id)`. A repeated request ID with different content fails. A repeated manifest returns the original result with zero new rows.

Both tables use forced RLS and append-only triggers. The rollback refuses to remove the writer schema if either table contains records.

## Stage transaction

`stage_relational_packet_v5` accepts the exact raw JSON text plus the caller's SHA-256 for both packets. In one transaction it:

1. verifies raw-text hashes, exact top-level fields, contract versions, size/count limits, and source-envelope equality;
2. locks and verifies active evidence owned by the actor and bound to `public.chat_log` external ID and content hash;
3. requires exactly one resolver decision for each mention;
4. inserts mentions, resolution plans, ordered candidates, observations, and typed temporal rows;
5. records one stage batch and one operation request;
6. returns deterministic ID maps and counts.

No partial packet can commit. Replaying the same request or manifest returns `outcome='replayed'` and all insert counts as zero. Reusing the same raw packets with a different extractor identity is rejected.

## Review transaction

The review preflight derives an authorization manifest from owner, active evidence content hash, mention hash, candidate-set hash, resolver decision hash, target/action, decision, and normalized review reason.

`review_entity_resolution_v5` locks the plan and evidence, recomputes the preflight manifest inside the transaction, compares it with the supplied manifest, inserts one immutable review, and records the operation request. Stale or changed authorization fails before insert.

## Apply transaction

The apply preflight binds the manifest to active evidence, mention, plan, latest approved review when required, selected entity state, and the proposed named entity when creating.

`apply_entity_resolution_v5` takes a transaction advisory lock for the owner/mention, locks the plan/evidence/review, recomputes the manifest, and then:

- auto-links `self` only to the owner's active `self` entity;
- auto-links a named non-self mention only when normalized name and type still match and exactly one active owner entity matches;
- creates only a manually approved named entity, using an opaque backend-generated entity key;
- rejects role-only and anonymous creation until the durable entity model represents unresolved identity safely;
- writes an alias observation for named mentions;
- creates an observation/entity binding only after every required mention has an applied resolution;
- records one apply row and one operation request.

Concurrent attempts for the same mention serialize. A changed request payload, changed authorization manifest, changed selected entity, same-name ambiguity, cross-owner target, inactive evidence, non-latest review, or second different applied plan fails closed.

## Verified tests

`tools/memory_v1_relational_writer_v5_ci.sh` reconstructs Memory V1 on PostgreSQL 16, runs the staging suite, applies both migrations twice, runs writer tests, verifies schema dumpability, proves `brains_app` cannot run the migration, rolls back writer then staging, and verifies the original predicate count and zero remaining writer objects.

`tools/memory_v1_relational_writer_v5_production_clone.sh` copies only the production schema into an ephemeral PostgreSQL 16 container and runs the same migration, transaction, isolation, replay, and rollback path. No production rows or Qdrant data enter the clone.

The transaction suite proves missing-actor denial, direct-write denial, owner isolation, exact stage counts, zero-write replay, request mismatch rejection, automatic entity linking, automatic observation binding, reviewed named creation, role-only creation rejection, same-name ambiguity rejection, rollback to zero rows, and restricted function grants.

## Activation prerequisites

This phase does not install production schema, wire an extractor, activate V5 projection/retrieval, change Qdrant, or start a job.

The legacy foundation still grants `brains_app` direct mutation rights on some pre-V5 durable tables, including `memory.entity`. Before the controlled writer is activated, every current caller of those grants must be audited and migrated, then those legacy grants must be revoked in a separate cutover. Installing this writer without that revocation would protect the new staging path but would not establish one complete durable-memory write boundary.

The next design phase is the projection contract from applied observation/entity bindings into governed claims, life preferences, response preferences, and project knowledge. It must preserve provenance, temporal validity, contradiction state, surface policy, owner scope, and zero-write replay without collapsing those semantic lanes into one table.
