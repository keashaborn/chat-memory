# Memory V1 controlled evidence writer

Server: seebx backend. Status: implementation and production-clone testing
only; no production grant revocation is part of this phase.

## Caller audit

The active direct INSERT path is
`scripts/memory_v1_consolidation_worker.py` through
`rag_engine.memory_v1_store.record_evidence`. The consolidation timer currently
allows two owners. Artifact, seed, projection-integration, and historical
promotion writers are manual/offline callers.

The evidence table contains 210 active rows across three owners. No normalized
chat source is duplicated. Thirty-one rows are not yet linked to derived
memory; they are an intake backlog, not deletion candidates. Missing
`public.chat_log` rows are expected because canonical evidence is retained
after source transcript cleanup.

## Controlled writer

`memory.record_owner_evidence_v1` is a private-schema `SECURITY DEFINER`
function owned by the existing `NOLOGIN`, `NOINHERIT`, `NOBYPASSRLS`
`memory_evidence_maintainer` role. It:

- derives the owner only from `app.user_id`;
- requires a `brains_app` session;
- serializes by owner and canonical source key;
- recomputes the content SHA-256 in PostgreSQL;
- inserts one immutable evidence row or returns a zero-write replay;
- rejects tombstoned sources and changed-content replays; and
- exposes no cross-owner lookup.

`rag_engine/memory_v1_store.py` calls only this function for evidence writes.
The existing direct `brains_app` INSERT grant remains unchanged until the
function is installed, the active worker is deployed and probed, and the
manual isolated evidence importer is migrated.

Direct SELECT also remains temporarily because legacy candidate, retrieval,
governance, privacy-gate, and preference/project paths still read
`memory.evidence`. It requires a separate controlled-reader migration before
revocation.

The next boundary after deployment is to revoke only direct evidence INSERT,
then build the read-only V5 intake selector. No evidence rows should be deleted
or deduplicated.
