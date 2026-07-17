# Memory V1 observation entailment V5.1 persistence

Status: database migration and production-clone test only. Production is
unchanged. Runtime retrieval remains inactive.

Server: seebx backend.

## Purpose

The deterministic V5.1 code gate decides whether source evidence entails a
proposed observation. This migration makes that decision durable and prevents a
stale or bypassed projection packet from using an observation without an
accepted decision.

## Enforcement

`memory.observation_entailment_v5` is append-only, owner-scoped, force-RLS, and
owned by the no-login `memory_v5_writer` role. Each row binds:

- owner, observation ID, observation hash;
- evidence ID and immutable evidence-content hash;
- exact UTF-8 source-span hashes;
- fixed policy version, decision, reason, and authorization manifest;
- assessor identity, invoking session, and timestamp.

The controlled APIs are:

- `preflight_observation_entailment_v5`: validates owner, active evidence,
  content hash, span hashes, coverage of the observation's original source
  spans, and decision/reason policy; returns a deterministic authorization
  manifest.
- `record_observation_entailment_v5`: uses an advisory transaction lock and an
  append-only request audit. Exact request replay writes zero rows. A different
  request cannot reuse an existing decision.
- `observation_entailment_allows_projection_v5`: returns true only for an
  owner-matched accepted decision whose observation and active evidence hashes
  still match.

The `projection_plan_observation_entailment_guard` trigger is fail-closed:
every new projection-observation link requires an accepted
`memory_v1_predicate_entailment_v5_1` decision. Deferred observations remain as
immutable evidence and observations but cannot enter projection.

`brains_app` has execute access only to the controlled APIs. It has no direct
read or write grant on the ledger. Cross-owner access resolves as not found.

## Clone proof

The disposable production-clone test uses the exact V5-04 occupation fixture
and a known valid correction fixture. It proves:

- the contradicted occupation observation records `deferred` and is blocked
  from projection;
- the correction observation records `accepted`;
- exact replay writes zero rows and a different request ID is rejected;
- tampered source-span hashes fail;
- missing actor and cross-owner access fail;
- direct `brains_app` ledger access fails;
- all test rows roll back and the guarded rollback removes the schema cleanly.

No Qdrant, model, prompt, frontend, or production write path is called.
