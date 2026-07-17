# Memory V1 preference/project durable apply — 2026-07-14 UTC

## Scope

Server: seebx backend. Branch: `memory_v1_foundation`. Starting commit: `413d6ad0f7d251b3c2ef4ac6d70ac42bad7c825e`.

This authorized phase created the first durable records from the eight previously reviewed specialized-lane candidates: three response/life preferences and five `verbal-sage` project-knowledge records. It stopped before retrieval activation, prompt integration, projection work, or Qdrant writes.

## Authorization and locked inputs

The existing manifest `memory_v1_preference_project_durable_apply_plan_20260714_v1` changed only `apply_authorized` from `false` to `true`. The authorization-enabled manifest SHA-256 is `83f6ccb0fdea3adec11540d1c3d32a83dffc89dc32d8c1edf740546d85de48f6`. The authorization-disabled manifest remains preserved in commit `413d6ad` with SHA-256 `2a46ebe299b705371b726a7514e650fb78ac3ba02dc31ff59c5af9c9322559f5`.

Locked source chain:

| Source | SHA-256 |
|---|---|
| Controlled review manifest | `3c847d0e3321bdfb80416abf4a25c9752d3ae842ddb17bd11b7d17f0c9caa946` |
| Controlled review apply report | `7e3e7aff89cd7e31b5733ff373b8ccd4115c32f27ae10fbd4134b79827393ff7` |
| Controlled review replay report | `fa2385dc171afb11bd4d9ce45c5a2826962e7da1f0a9f543f8a79c242c49070e` |

Full isolated Memory V1 CI passed after the authorization change. The final production preflight then passed all ten controls in a serializable read-only transaction and reported zero writes.

## Production backup

Fresh backup: `/home/ubuntu/brains/snapshots/memory_pre_durable_preference_project_apply_20260714T103201Z.dump`

- Size: `74359967` bytes
- SHA-256: `27a9bafc492540bba1db43b05e17fcbbe9530eda96a3477a37fc8dab1e1c892f`
- `pg_restore --list`: passed

## Transaction result

The apply ran as `brains_app` for owner `1240822d-ac9a-4096-95aa-e2b24d36ef50` in one controlled transaction. It committed exactly 32 rows:

| Table | Rows |
|---|---:|
| `memory.user_preference` | 3 |
| `memory.preference_revision` | 3 |
| `memory.preference_revision_evidence` | 3 |
| `memory.preference_apply_event` | 3 |
| `memory.project_knowledge_head` | 5 |
| `memory.project_knowledge_revision` | 5 |
| `memory.project_knowledge_revision_evidence` | 5 |
| `memory.project_knowledge_apply_event` | 5 |

Evidence, candidates, reviews, claims, lifecycle events, projection outbox, and project relations were unchanged. The apply created no prompt configuration, retrieval activation, or Qdrant data.

The committed event IDs are:

| Candidate ID | Apply event ID |
|---|---|
| `008c6200-72fd-5dfa-9d9d-357b4017e95d` | `f6025354-ec91-466e-a412-2d65e9f5ead3` |
| `5f52649c-2e12-59a4-bfe2-dcc6e36ffff8` | `80c62008-7b06-40ea-8b99-9254ebae38f1` |
| `6d12fe82-b902-5117-bc38-724176aa18f6` | `4ffc37e9-d626-4716-9295-80078a644371` |
| `91cec4a9-a471-55a9-9097-1d45f29c2280` | `a09dd151-e60b-4277-8022-8ccebc5095ba` |
| `b32c57ee-cb63-5099-b707-4510055ff79b` | `a0c7de91-0429-46a0-98ed-cff27a190167` |
| `ca2a91b5-7ceb-5adf-aa71-7a884d8f47ba` | `f25559e9-3e95-4fe2-a37b-78c7eacdeadd` |
| `d2803521-0d9d-559c-a267-104f8bf0c6cc` | `cf3009fb-a6c7-4f0c-a7e6-037feb3a6d8d` |
| `ed627de8-8042-50c1-b53f-698510087168` | `cad0e6be-24df-4b77-ae31-87d92e2e27c8` |

## Replay and independent verification

Exact replay returned `verified_replay`, the same eight event IDs, and `database_writes=0`.

Independent read-only SQL verified:

- Exact durable totals: `3/3/3/3` preference rows and `5/5/5/5` project rows.
- Three complete preference head/revision/review/event/evidence chains.
- Five complete latest project head/revision/review/event/evidence chains.
- Eight unique request IDs and eight unique request hashes.
- Every event has `actor_user_id=owner_user_id` and `invoked_by_role=brains_app`.
- Evidence `173`, claim candidates `7`, claims `6`, projection outbox `6`, and lifecycle events `0` remained unchanged.
- A different owner context saw zero durable preference and project rows or events.
- `brains.service` remained active and running; authenticated `/healthz` and `/readyz` passed with Postgres ready.

Report hashes:

| Report | SHA-256 |
|---|---|
| Final production preflight | `a598df7c97b5da69dfb74b473af86c4d4c6dc9d966413ad1a41425d79dd2a177` |
| Committed apply | `5bd1b1e52eec95f2e9fc9e8ed33a2ffc2025e71774e19d38355b8f530541125d` |
| Zero-write replay | `ba4e9c0b8b07e8025a06d6e3d9fc5d31ff7b076cc047882d6813725a51b60963` |

The committed apply report was preserved from the runner's captured JSON stdout after the transaction. The preflight and replay reports were written directly by their read-only commands.

## Stop boundary

Retrieval remains inactive. No prompt builder, router, persona loader, frontend chat route, projection worker, Qdrant collection, or retrieval policy was changed. The next phase is a separately reviewed retrieval design and shadow-only evaluation for these durable preference/project records; it must not activate production retrieval without a new authorization boundary.
