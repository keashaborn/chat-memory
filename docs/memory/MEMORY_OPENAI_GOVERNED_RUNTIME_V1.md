# Memory OpenAI Governed Runtime V1

## Purpose

This candidate replaces the inactive local-GPU extraction dependency with a
bounded OpenAI Responses extraction lane while preserving PostgreSQL as the
canonical authority. It does not activate automatic memory, drain historical
jobs, promote claims, write Qdrant, or change chat retrieval.

The first production milestone is deliberately one exact owner, one exact
evidence-extraction job, one exact content hash, at most one provider attempt,
one immutable request receipt, one validated packet, and one PostgreSQL review
artifact. Promotion remains a separate governed decision.

## First vertical slice

```text
owner-scoped chat evidence
  -> exact job and content-hash probe
  -> deterministic personal-evidence gate
       -> terminal skip, zero reservation, zero provider calls; or
       -> eligible request
  -> PostgreSQL provider reservation and canonical content-free request receipt
  -> one-use privacy and budget grants derived from that reservation
  -> at most one OpenAI Responses structured-output call
  -> deterministic schema and provenance validation
  -> atomic PostgreSQL packet persistence
  -> existing call completion plus request-receipt finalization
  -> in-process review artifact persisted in forced-RLS PostgreSQL
  -> manual-review route only
```

Qdrant is not part of this slice. It remains a rebuildable projection and may
only receive governed canonical claim revisions through the existing projection
outbox after a later activation gate.

## Authority boundaries

- PostgreSQL owns evidence, jobs, provider reservations, request receipts,
  packets, review artifacts, governed decisions, claims, corrections,
  retractions, deletions, and projection authority.
- Provider output is an untrusted proposal. It cannot become a claim merely
  because extraction succeeded.
- The deterministic prefilter executes before provider reservation. General
  questions and non-personal text end as audited skips and consume no provider
  reservation.
- The outbound request receipt contains hashes, policy identities, token and
  cost ceilings, and exact owner/job/evidence bindings. It contains no raw
  private text.
- Human-readable review files are not an authority. Review and stage JSON are
  bounded PostgreSQL values protected by forced RLS.
- The new worker imports provider-neutral job-store functions, not the legacy
  worker command.

## Runtime state

`ops/systemd/memory-v1-active-runtime-manifest-v1.json` is the machine-readable
candidate manifest. Both new services and timers are source-only and inactive.
The timer definitions also require root-owned enable sentinels that are outside
this candidate. An activation release must bind the final commit/tree, install
the reviewed units and configuration, prove legacy extraction exclusivity, and
start only the exact one-job pilot.

The local GPU scheduler and tunnel must remain inactive. Existing local packet
and claim-processing components are not silently retired by this candidate;
their eventual retirement requires an inventory showing that no accepted
packet, claim, correction, retraction, deletion, or projection behavior depends
on them.

## Independent audit dispositions

The 2026-08-06 independent audit was advisory. Its material recommendations are
resolved as follows:

1. **Accept now — eligibility before reservation.** The exact job is probed
   without claim/reservation, and ineligible content is terminally skipped.
2. **Accept now — one canonical outbound request receipt.** The actual transport
   request has one deterministic, content-free PostgreSQL receipt linked to the
   provider completion audit.
3. **Accept now — remove filesystem review authority.** Review artifacts are
   built in process and stored only in owner-scoped PostgreSQL.
4. **Accept now — remove the legacy-worker import.** The new entrypoint uses the
   provider-neutral `memory_v1_extraction_job_store_v1` boundary.
5. **Accept now — first slice versus follow-on.** One exact job through manual
   review is the initial slice. Automatic claim promotion, backlog drain,
   Qdrant projection, and broad continuous extraction are follow-on work.
6. **Accept now — three validation tiers.** Unit/static tests are the edit loop;
   one synthetic PostgreSQL fixture is the candidate checkpoint; one production-
   schema forward/rollback/reapply clone is the release gate.
7. **Accept now — active runtime manifest.** The JSON manifest names the new
   units, entrypoints, packages, state transitions, and exclusivity boundary.
8. **Defer — full legacy retirement.** The candidate proves the old extraction
   worker is not imported and the GPU lane must remain inactive. Deleting old
   code or disabling other proven production consumers is a separate release.
9. **Reject — rewrite or weakened governance.** The governed function framework,
   exact hashes, forced RLS, owner isolation, rollback/reapply proof, and manual
   promotion boundary remain required.

## Activation gates still required

Before a provider-backed pilot can run, the final candidate must pass focused,
compatibility, governance, manifest, repository, security, and disposable
PostgreSQL forward/rollback/reapply validation. Production activation then
requires a fresh database backup/restore proof, exact migration lease, exact
source/deploy lease, reviewed root-only runtime configuration, verification that
the local GPU scheduler/tunnel cannot claim the same job, and one exact synthetic
or user-approved pilot job.

After the pilot packet reaches manual review, later releases still have to wire
governed claim admission, projection-outbox dispatch, Qdrant rebuild/cutover,
PostgreSQL-revalidated retrieval, and the Chat A to Chat B acceptance test with
correction, retraction, deletion, provenance, and owner-isolation evidence.
