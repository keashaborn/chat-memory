# SeeBx current residual ledger v1

Date: 2026-08-21

Status: candidate evidence; no deployment or retirement authority

## Verified checkpoint

- Production SeeBx remains at backend commit
  `49f9e60cf4321c8e42c359845c1a62a8c987614d`; its Git status is empty and
  `brains.service` is active with zero restarts.
- The cleanup integration candidate and GitHub branch began the Training
  logical-aggregate split at `51746302bdce1ece966f9ac2f4c384e002b259da`; the worktree was clean
  and matched its remote before candidate-only changes.
- The candidate has 138 routes and 124 OpenAPI paths. Exact parent/candidate
  route and OpenAPI structures match; route-table SHA-256 is
  `35ed7eb9b2c30688234135d182a2a45ea29111904b6149c747265e25e22450d5` and OpenAPI SHA-256 is `0c98736818a52d53797460c1f207f014d9882af64400363ae1fd69c21f54b46a`.
- The former mixed 1,430-line Training module is a 78-line composition root
  over five explicit aggregate modules. All moved handler ASTs and all 41
  Training routes match the parent; all 103 focused Training tests and the
  complete 1,334-test candidate suite pass.
- All eleven retained LifeSwitch domain route modules now consume verified
  Supabase or owner-bound voice-session identity before owner/RLS access.
  Seventy owner-bound calls, eight owner-independent verified-actor calls,
  and eight database-owner callbacks replace the raw header boundary. Raw-
  header-only requests are rejected before repository access in all four
  domain groups; the full 1,354-test suite passes.
- The separate failed voice-canary unit and active timer remain outside this
  cleanup batch.

## Retired executable paths

| Legacy surface | Current evidence | Disposition |
|---|---|---|
| `rag_engine/**` | Zero tracked files and zero files remain on disk under the candidate path. The only tracked reference is the CI absence guard. | Source retirement complete in candidate; retain Git history and the absence guard |
| Redis application client | Zero tracked candidate runtime references; production retains 203 persistent idle keys in the loopback-only container | No application consumer; reversible production stop remains separately authorized |
| Qdrant runtime | No service, container, listener, client module, capability dependency, or candidate Python setting reader | Runtime retired; remove the still-injected production `QDRANT_URL` only in a separately verified configuration batch |
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

1. No retained LifeSwitch capability handler owns a raw PostgreSQL query or
   request-owned connection lifetime. Nutrition is separated into Log, Meals,
   Foods/Servings/Overrides, and Meal Plans modules behind one 48-line
   composition root. Training is separated into Exercises, Conditioning,
   Sharing, Templates, and Sessions modules behind one 78-line composition
   root. The canonical disposable restore now proves exact database-manifest
   equality, five self-owner boundaries across 33 protected surfaces,
   fail-closed disabled delegation, denied cross-owner writes, and cleanup.
   The code-level Supabase/voice identity boundary and paired frontend bearer
   forwarding are now consistent. A real signed-session frontend/backend/
   disposable-database proof and enabled delegation, if approved, remain
   required before producing the final release bundle.
2. Immutable release control is installed on SeeBx and Verbal Sage at exact
   package release `08322acd1ff738c88cc83c2361ffdb4c35e5b29d`. The current
   cleanup candidate postdates the sealed backend build, so final application
   bundles and manifests must be regenerated only after code consolidation.
3. Production still runs the unused `brains-redis-1` container on loopback
   port 6379 with 203 persistent idle keys. The reversible stop plan is ready;
   deletion is a later decision after observation.
4. Production still injects `QDRANT_URL`, although no Python reader or Qdrant runtime exists. The immutable candidate now explicitly unsets it; production changes only through a separately approved release cutover.
5. `voice-synthetic-canary.service` is failed while its timer remains active.
   Repair belongs to the separate voice worktree and must not be folded into
   backend cleanup.
6. Platform PostgreSQL still contains the legacy `memory` and `memory_ingest_private` schemas and clone databases; the five Vantage schemas are absent. The old retirement package is blocked because 32 attestations require canonical migration and 158 require encrypted quarantine. Recovery and disposable-restore evidence exists, but migrations,
   schema retirement, and database drops remain separate production batches.
7. The production service still runs the mutable checkout with weak systemd
   hardening. Its mutable virtual environment also lacks declared
   `jsonschema==4.25.1`; the sealed candidate runtime contains the exact locked
   dependency. Correct this through immutable cutover, not an ad hoc live install.
8. The candidate has multiple historical worktrees. Remove them only
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

1. Run a real signed-session paired frontend/backend proof against the
   verified disposable database, including Plan-to-Nutrition application
   behavior; only test delegated People access if separately enabled. Plan
   targets remain independent of retired page-level AI helpers.
2. Bind exact database schema, owner/RLS, delegated-access, migration, rollback,
   and paired frontend evidence before regenerating any release package.
3. Run the Forms schema, exact valid/quarantine reconciliation, rollback, and
   cross-owner denial proof only in a separately authorized disposable restore.
4. Prove Zep owner/thread isolation, deletion, export/retention, outage, and
   provenance behavior before declaring the old memory paths fully replaced.
5. Regenerate immutable backend/frontend bundles and release manifests from the
   final candidate commits.
6. Execute the paired frontend/backend cutover with fresh authorization and
   automatic rollback evidence.
7. Observe, then retire Redis, stale Qdrant configuration, and legacy
   schemas/databases in independent rollback-safe production batches.
8. Create encrypted clean-state snapshots, enforce the two-backup policy,
   and only then remove superseded worktrees and older snapshots.

This ledger authorizes no deployment, restart, database change, cache stop,
service change, credential change, AWS publication, or deletion.
