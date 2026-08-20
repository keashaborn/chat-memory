# SeeBx current residual ledger v1

Date: 2026-08-20

Status: candidate evidence; no deployment or retirement authority

## Verified checkpoint

- Production SeeBx remains at backend commit
  `49f9e60cf4321c8e42c359845c1a62a8c987614d` with `brains.service` active.
- The cleanup integration parent is
  `0ff15dc7fb5e3a289726c7165090c3671d0ceb19`; this ledger and batch 05 are
  carried by its next candidate commit.
- The candidate has 143 routes and 129 OpenAPI paths, unchanged by batch 05.
- The complete candidate suite passes 1,177/1,177 in the sealed runtime.
- All registered Git worktrees were clean at inventory time. The separate
  failed voice-canary worktree and unit remain outside this cleanup batch.

## Retired executable paths

| Legacy surface | Current evidence | Disposition |
|---|---|---|
| `rag_engine/**` | Zero tracked files. The only tracked `rag_engine` reference is the CI absence guard. Ignored bytecode directories are candidate residue, not source. | Source retirement complete in candidate; remove ignored cache residue before commit |
| Redis application client | Zero tracked runtime references and zero current connections or commands | No consumer; reversible production stop remains separately authorized |
| Qdrant runtime | No service, container, listener, client module, or capability dependency | Runtime retired; remaining text is vocabulary, frozen specifications, and migration descriptions |
| Vantage executable code | No executable Python reference | Retain only retirement SQL, historical telemetry column, and versioned policy/prompt compatibility until the database batch |
| RESSE engine | No engine/router package remains | Retain centralized legacy provenance constants and historical SQL source values until a versioned data-contract migration |
| `memory_api` | No tracked candidate reference | Retire dormant external checkout with the later Redis removal batch |

## Intentional compatibility residue

| Residue | Why it remains | Removal gate |
|---|---|---|
| `successor_memory_context_block` and `successor_memory_context_manifest_sha256` | Hash-bound internal model fields currently carry the sole accepted `zep_memory_v1` block | Introduce a versioned provider-neutral contract and migrate all producers, consumers, fixtures, and stored hashes together |
| `governed_memory_successor_answer_provenance_v1` and `governed_memory.response_provenance.v1` | Stored provenance compatibility | Versioned provenance migration with exact old/new byte and replay tests |
| `backend/resse:assistant:v1` and `resse_response_*` values | Historical transcript and response provenance | Preserve read compatibility until migrated rows and views prove parity and rollback |
| `vantage_id` in telemetry/retirement SQL | Historical column and exact retirement target | Bound backup/restore, zero-write soak, forward migration, and rollback receipt |
| `chat_memory_still_processing` | External error code compatibility for conversation erasure | Versioned client/server error migration or explicit compatibility-retention decision |

## Remaining architectural debt

1. Sixteen capability modules still contain direct database calls. They must be
   moved behind named repository/adapters in bounded behavior-preserving
   batches. Highest-value next groups are LifeSwitch nutrition/training,
   conversation provenance, forms, operations/telemetry, and search monitoring.
2. The old `LifeSwitch-AtomicReleaseActionV1` AWS document points to an obsolete
   script path. The corrected immutable node-control package and versioned SSM
   documents are built, tested, encrypted in S3, and intentionally not installed
   without separate authorization.
3. Production still runs the unused `brains-redis-1` container on loopback
   port 6379 with 203 persistent idle keys. The reversible stop plan is ready;
   deletion is a later decision after observation.
4. `voice-synthetic-canary.service` is failed while its timer remains active.
   Repair belongs to the separate voice worktree and must not be folded into
   backend cleanup.
5. Platform PostgreSQL still contains legacy memory/Vantage schemas and clone
   databases. Recovery and disposable-restore evidence exists, but migrations,
   schema retirement, and database drops remain separate production batches.
6. The production service still runs the mutable checkout with weak systemd
   hardening. The immutable release template is ready but not activated.
7. The candidate has multiple clean historical worktrees. Remove them only
   after the integrated branch, Git bundle, release artifacts, and rollback
   evidence are independently recoverable.

## Current canonical owners

- Identity and ownership: `seebx.core` plus `seebx.adapters.supabase`.
- Conversation/transcripts: `seebx.capabilities.conversation` with PostgreSQL,
  Zep, and OpenAI effects behind adapters.
- Conversational memory: Zep only; PostgreSQL remains transcript, outbox,
  audit, and structured-data authority.
- Search: `seebx.capabilities.search` plus named provider/audit/cache adapters.
- Voice: `seebx.capabilities.voice` plus OpenAI and PostgreSQL adapters.
- LifeSwitch domain data: isolated PostgreSQL on loopback port 55433.
- Platform data: PostgreSQL on loopback port 5432.
- Work execution: isolated Work Runner with rootless Podman and job intake
  still disabled.

## Next cleanup order

1. Install the immutable release-control package only after explicit approval.
2. Continue candidate-only database-effect extraction, one capability group at
   a time, with focused and full-suite parity.
3. Regenerate the current release package after the final candidate commit.
4. Execute the paired frontend/backend cutover with fresh authorization and
   automatic rollback evidence.
5. Observe, then retire Redis and legacy schemas/databases in independent
   rollback-safe production batches.
6. Create the encrypted clean-state snapshots, enforce the two-backup policy,
   and only then remove superseded worktrees and older snapshots.

This ledger authorizes no deployment, restart, database change, cache stop,
service change, credential change, AWS publication, or deletion.
