# Memory V1 preference/project candidate apply — 2026-07-14

## Scope

The authorized operation registered project key `verbal-sage` and persisted
only the eight hash-locked candidate rows and their eight evidence links from
the reviewed extraction report. It created no reviews, durable preferences,
durable project knowledge, claims, evidence, projection events, Qdrant points,
or prompt inputs.

Authorization manifest SHA-256:
`c0187b7ba6b92a2b485eb910629451df37daeebe025f2e23ad54a99b58caab5d`

Locked extraction manifest SHA-256:
`25d59e6f777afb4cf37057278876d58af9b11502bded6c52f0a546261d7b2cee`

Locked extraction report SHA-256:
`fce21afc06065b2b99c0fa11bde844da2eee158e21cd8ed90506c99345829728`

## Verification before apply

- Full disposable PostgreSQL 16 CI passed, including commit, exact stored-row
  comparison, zero-write replay, rollback isolation, and cross-owner RLS tests.
- The live read-only preflight returned `preflight_verified`, `ready_for_apply:
  true`, and `database_writes: 0`.
- All eight evidence rows were active, owner-matched, members of evidence batch
  `15064e5d-8cd3-5611-9cbf-db177d24a3a0`, and equal to their append-only
  ledger hashes.
- Forced RLS, candidate insert-only grants, append-only triggers, active-evidence
  guards, the hardened project-registration function, and denial of direct
  review/durable inserts all passed.

## Backup

Fresh serialized production backup:

`/home/ubuntu/brains/snapshots/memory_pre_preference_project_candidates_20260714T035327Z.dump`

- SHA-256: `e9893df4f98231962d5c4ff725f98728d2c23789a2801c642aa2d88287215862`
- Bytes: `73523097`
- `pg_restore --list` passed.

## Committed result

The serializable transaction committed exactly 18 rows:

- 1 `memory.project_space` row for `verbal-sage`;
- 1 append-only `memory.project_space_registration_event`;
- 3 `memory.preference_candidate` rows;
- 3 `memory.preference_candidate_evidence` rows;
- 5 `memory.project_knowledge_candidate` rows;
- 5 `memory.project_knowledge_candidate_evidence` rows.

Project ID: `08cd6a8a-5599-43d5-8d5c-b59401df8ccc`.

Owner totals that were required to remain unchanged did remain unchanged:
evidence `173`, general claim candidates `7`, claims `6`, projection outbox `6`,
and evidence lifecycle events `0`.

All preference/project reviews, replacements, durable heads, revisions, and
apply events remained `0`.

## Replay and isolation proof

The same apply command was run again with the same confirmation. The controlled
project-registration function revalidated its request hash, every stored
candidate and evidence link matched the locked report, all counts were equal
before and after replay, and the result was `verified_replay` with
`database_writes: 0`.

An independent read-only SQL transaction reproduced the exact `3/3/5/5/1/1`
candidate/link/project/registration counts and all-zero review/durable counts.
Changing `app.user_id` to a different Supabase UUID exposed zero rows owned by
the authorized user.

`brains.service`, PostgreSQL, Qdrant, Redis, and the initiator remained running.
The authenticated Brains `/healthz` endpoint returned `status: ok`.

## Retained audit artifacts

- `/home/ubuntu/memory-v1-reviews/MEMORY_V1_PREFERENCE_PROJECT_CANDIDATE_APPLY_20260714.json`
- `/home/ubuntu/memory-v1-reviews/MEMORY_V1_PREFERENCE_PROJECT_CANDIDATE_REPLAY_20260714.json`

The next phase is review design and review persistence for these candidates.
Candidate presence alone does not authorize acceptance or durable-memory apply.
