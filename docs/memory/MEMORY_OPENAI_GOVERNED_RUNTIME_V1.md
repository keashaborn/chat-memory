# Memory OpenAI Governed Runtime V1

## Purpose

This runtime uses a bounded OpenAI Responses extraction lane while preserving
PostgreSQL as the canonical authority. Provider output is an untrusted proposal.
Qdrant is rebuildable derived data for governed claims and is not an authority.

The runtime is not one complete active Chat A to Chat B pipeline yet. Capture and
contextual intake are active. The OpenAI extractor and PostgreSQL-only packet
router are installed but disabled and inactive. A governed review-to-claim
admission handoff and user-facing claim lifecycle are still missing. The local
filesystem review/staging lane and raw `memory_raw` routes remain explicit
compatibility paths, not governed claim memory.

## Exact first vertical slice

```text
owner-scoped Chat A transcript
  -> active PostgreSQL chat capture
  -> active contextual evidence intake
  -> exact owner/evidence/job/content-hash binding
  -> five-outcome deterministic exchange/window gate
       -> skip_zero_call: durable terminal skip, zero provider reservation
       -> review_context: durable review-required disposition, zero reservation
       -> route_internal: durable internal disposition, zero reservation
       -> block_local: durable protected disposition, zero reservation
       -> send_external: continue to exact provider reservation
  -> at most one OpenAI structured-output call
  -> content-free request and completion receipts
  -> immutable PostgreSQL packet
  -> PostgreSQL-only packet review route
  -> one explicitly reviewed proposition admission (currently missing)
  -> one canonical claim/revision and held outbox item
  -> one derived Qdrant governed-claim point
  -> Chat B candidate discovery and PostgreSQL claim revalidation
  -> final-answer memory binding and bounded provenance
  -> inspect/correct/retract/delete operations (currently missing)
```

The first pilot permits one exact owner, one exact evidence job and content hash,
at most one provider call, one packet, one reviewed proposition, one canonical
claim, one projection item, and one later Chat B. Recurring OpenAI timers,
automatic claim promotion, and backlog drain remain forbidden.

## Current installed topology

`ops/systemd/memory-v1-active-runtime-manifest-v1.json` records the complete
target installed-inactive checkpoint rather than pretending the OpenAI units are
absent. It declares:

- both OpenAI service/timer pairs installed, disabled, and inactive;
- every installed `memory-v1-*` unit and its exact source/installed hash;
- active capture, contextual intake, local compatibility routing/staging,
  local resolution, manual claim planning, projection, and reconciliation timers;
- elapsed historical timers and disabled/inactive GPU, entailment, entity, and
  reintake components as superseded but not retired;
- `brains.service` and the installed-only daily Git-sync service/timer hashes;
- loaded backend route and worker source hashes;
- exact critical PostgreSQL function hashes and forced-RLS relations;
- the missing review-to-claim and user lifecycle handoffs;
- raw `/log`, `/cards`, `/vantage`, filesystem review, and legacy router
  compatibility boundaries;
- one authoritative consumer per declared job type.

An unlisted installed Memory unit, unexpected active claimer/router, changed
source or installed unit byte, catalog function drift, lost forced RLS, stale
Git identity, changed runtime configuration hash, present OpenAI timer sentinel,
or stale release binding makes verification fail closed.

## Authority boundaries

- PostgreSQL owns transcript evidence, jobs, eligibility dispositions,
  reservations, receipts, packets, review state, canonical claims and revisions,
  outbox authority, final-answer bindings, corrections, retractions, and deletes.
- The provider cannot create a claim. A validated packet is still only a proposal.
- Eligibility and bounded exchange/window classification occur before provider
  reservation. Every non-send outcome is durable and consumes no provider call.
- Qdrant may propose governed claim IDs, but the response path must reload and
  owner-check them through PostgreSQL before prompt inclusion.
- The `/log` raw-Qdrant side effect and `/cards` API are compatibility surfaces.
  Their data must never be treated as PostgreSQL-governed claim memory.
- Filesystem review artifacts are compatibility inputs only. The intended OpenAI
  packet router writes its review state to PostgreSQL.
- The active local claim-projection timer produces manual-review plans with zero
  claims. It is not an automatic claim-admission service.

## Content-free verifier and release binding

The external root-owned release binding remains:

```text
/etc/chat-memory/memory-v1-active-runtime-release-binding-v1.json
```

The binding contains no provider key, prompt, response, owner text, or other
private value. It binds the exact repository commit/tree, manifest hash, all
declared source hashes, all installed unit hashes, critical catalog function
hashes, root-owned runtime configuration hash, Python executable hash, OpenAI SDK
version, and phase.

After the eligibility package, source candidate, reviewed unit bytes, and a new
binding are installed—but while both OpenAI timers remain disabled—run in a
separately authorized quiescent window:

```text
sudo /opt/chat-memory/venv/bin/python \
  /opt/chat-memory/scripts/memory_v1_active_runtime_verifier_v1.py \
  --phase installed_inactive \
  --binding /etc/chat-memory/memory-v1-active-runtime-release-binding-v1.json
```

The verifier reads only content-free runtime/config hashes and PostgreSQL catalog
metadata. It uses `POSTGRES_DSN` internally from the root-owned environment file
but never prints the DSN or configuration values. It reads no application row and
makes no provider or Qdrant call. Because recurrent timers can make oneshot
services temporarily active, the verifier is a quiescent release gate; a worker
running during the snapshot correctly causes a failure.

The former `preactivation` phase is retired. It required the OpenAI units and
configuration to be absent, which is no longer true. Current source must not use
that historical phase to claim runtime validity.

## Remaining activation gates

Before one provider-backed pilot:

1. Install and validate the eligibility-disposition package and this combined
   source candidate without starting either OpenAI timer.
2. Generate and install the exact full-path release binding; run the verifier in
   a quiescent window.
3. Add one PostgreSQL-only, owner-scoped, append-only manual review-to-claim
   admission operation and inspect/correct/retract/delete lifecycle.
4. Isolate raw `memory_raw` compatibility data from governed retrieval without
   deleting existing vectors or artifacts.
5. Obtain separate authority for one exact application item, at most one provider
   call, one manual service invocation, one proposition admission, one outbox
   projection, and authenticated Chat B browser verification.

Any provider call, database installation or application-row access, Qdrant write,
service/timer action, configuration/binding installation, source deployment, or
frontend activation remains a separate production authorization. This document
and manifest do not grant it.
