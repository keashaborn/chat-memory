# SeeBx current residual ledger v1

Date: 2026-08-20

Status: candidate evidence; no deployment or retirement authority

## Verified checkpoint

- Production SeeBx remains at backend commit
  `49f9e60cf4321c8e42c359845c1a62a8c987614d`; its Git status is empty and
  `brains.service` is active with zero restarts.
- The cleanup integration candidate and GitHub branch are both
  `be653e03c403c43492f243bac198b538ee4a6f07`; the worktree is clean before this candidate-only safety batch.
- The candidate has 143 routes and 129 OpenAPI paths. Route SHA-256 remains
  `c8d1df9cf48611e6c849614a521979b4a6109b0b4ef14f99ae55f4e85915d7d6`
  and OpenAPI SHA-256 remains
  `17aaa3a4a18bfe3a3d3671c1fee02654270524d0b5d764cd4c6eea6627429a8f`.
- The complete candidate suite passes 1,204/1,204 after the candidate-only retirement safety batch.
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

1. Ten capability modules still contain direct SQL execution or transaction
   ownership. Conversation, search, preferences, and voice now have zero direct
   query/transaction effects in their capability packages. The remaining files
   are exactly:

   - forms: `seebx/capabilities/forms/routes.py`;
   - LifeSwitch domain: `measurements/routes.py`, `nutrition/logs.py`,
     `nutrition/meals.py`, `nutrition/routes.py`, `plans/routes.py`,
     `training/logs.py`, and `training/routes.py`;
   - operations/observability: `operations/ai_operations.py` and
     `observability/telemetry.py`.

   Owner-scoped connection acquisition in an HTTP capability is not counted as
   a query effect; raw fetch/execute/transaction ownership is. Each remaining
   aggregate must move behind a named adapter without preserving duplicate
   catalog or write authority.
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

1. Apply the catalog/domain decision in
   `docs/SEEBX_CATALOG_AND_DOMAIN_AUTHORITY_MATRIX_V1.md`: isolated
   `catalog_dev` is canonical; first separate active catalog reads/providers
   without changing security policy or production.
2. Continue candidate-only database-effect extraction one coherent aggregate
   at a time, with focused and full-suite parity.
3. Prove Zep owner/thread isolation, deletion, export/retention, outage, and
   provenance behavior before declaring the old memory paths fully replaced.
4. Regenerate immutable backend/frontend bundles and release manifests from the
   final candidate commits.
5. Execute the paired frontend/backend cutover with fresh authorization and
   automatic rollback evidence.
6. Observe, then retire Redis, stale Qdrant configuration, and legacy
   schemas/databases in independent rollback-safe production batches.
7. Create encrypted clean-state snapshots, enforce the two-backup policy,
   and only then remove superseded worktrees and older snapshots.

This ledger authorizes no deployment, restart, database change, cache stop,
service change, credential change, AWS publication, or deletion.
