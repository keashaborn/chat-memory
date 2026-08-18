# SeeBx refactor batch 02 v1

## Extract the core actor identity boundary

Status: isolated candidate implemented; not pushed or deployed

Production authority: `49f9e60cf4321c8e42c359845c1a62a8c987614d`

Candidate base: Batch 01 commit `10b5cb4a`

Candidate commit: `fb2521496869aeb4e5b870443ee8d954fd76b1da`

Branch: `codex/seebx-core-identity-20260817`

Risk: low-to-moderate authentication refactor; no data mutation

Rollback: Git revert of the candidate commit; no database rollback required

## Purpose

The live application and response router depended on actor types located inside
the retired governed-memory architecture. That made a retired package part of
the owner-authority chain even though its optional live-authority verifier had
no production caller.

This batch creates the neutral `seebx.core.identity` boundary while preserving
the existing Supabase and voice-session authority behavior. A temporary
compatibility module re-exports the old names for dormant callers; it contains
no independent implementation.

## Exact scope

- add `seebx/core/identity.py` with the generic `ActorContext`, Supabase actor
  verification, voice owner/session verification, and authority constants;
- add package exports under `seebx` and `seebx.core`;
- update `app.py` and `rag_engine/resse_response_router.py` to import the new
  core boundary directly;
- replace `rag_engine/memory_actor_auth_v1.py` with a compatibility re-export;
- remove the unused optional governed-memory live-authority verifier;
- rename the actor-auth test to `tests/test_core_identity.py` and update focused
  callers/tests.

The exact voice context schema literal remains
`response-memory-actor-context-v1`. Changing it would alter a provenance hash
and is outside this extraction batch.

## Verification evidence

- exact diff: 11 files, 265 insertions, 216 deletions;
- focused tests: 28/28 pass using `/opt/chat-memory/venv/bin/python`;
- compatibility smoke test proved the old actor names are aliases of the new
  core objects;
- clean candidate import proved `app` and the response router use
  `seebx.core.identity` directly;
- the legacy actor module and governed-memory HTTP auth module were absent from
  the clean candidate import;
- Qdrant module count remained zero and readiness remained true;
- candidate branch is clean;
- production remains clean at `49f9e60c` and `brains.service` remains active;
- the exact-path commit lease was released after commit.

The system Python test attempt was rejected as invalid evidence because it has
Pydantic 1.x and lacks the OpenAI package. The production service uses
`/opt/chat-memory/venv`; the production-matching rerun passed.

## Residual dependency

The response graph still imports `rag_engine.governed_memory.auth` indirectly
through response-provider/provenance contracts. This batch does not claim that
governed-memory has left the live graph. The next extraction must trace and move
those generic response contracts before any package retirement.

## Explicitly out of scope

- no production checkout, service, environment, route, database, or frontend
  change;
- no push, deployment, restart, schema migration, or deletion;
- no response composition, prompt, Zep, search, voice, or domain behavior
  change;
- no immediate deletion of the compatibility module or dormant callers;
- no change to the voice manifest/provenance hash.

## Activation gate

Deployment requires separate authorization and deployment authority. A
production-equivalent candidate process must pass Supabase actor, cross-owner
denial, voice lease, response, chat persistence, Zep, search, nutrition,
training, and readiness checks before any service restart. The rollback commit
and pre-deployment state must be recorded first.
