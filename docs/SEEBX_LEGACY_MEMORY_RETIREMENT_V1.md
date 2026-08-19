# SeeBx legacy memory retirement manifest V1

## Controlling decision

SeeBx retains Zep as the only conversational-memory provider. PostgreSQL
remains authoritative for owner-scoped chat records, audit/provenance records,
Zep synchronization state, and structured LifeSwitch application data. Both
home-built memory attempts are retired:

1. the original card, Vantage, Qdrant, extraction, and RAG-memory chain; and
2. the later governed-memory successor, its dormant installation controller,
   migrations, test harness, and Qdrant/PostgreSQL store design.

Retirement means absent from the deployed source tree, runtime configuration,
services, timers, cron, active databases, caches, and current CI authority. Git
history and a hash-bound offline recovery archive preserve historical evidence.
Historical code must not remain in the active checkout merely as a backup.

## Verified current state

Evidence was collected read-only on SeeBx and Verbal Sage on 2026-08-18.

- Production SeeBx remains at `49f9e60cf4321c8e42c359845c1a62a8c987614d`.
- The cleanup integration candidate is verified through `c840afed`: it moves
  telemetry retention to `ai_operations`, moves chat-history erasure to the
  canonical Zep outbox, and retains exact 149-route/OpenAPI parity.
- No Qdrant process, container, unit, or listener is active.
- The live API has no Redis connection. The Redis container holds 203
  non-expiring legacy keys and is not a Zep dependency.
- Production still has the daily `eval_all_users.sh` cron targeting the absent
  Qdrant `memory_raw` collection. The cleanup candidate removes its source;
  runtime retirement remains a separate rollback-bound production action.
- No `memory-v1-*` systemd service or timer is installed.
- Zep prompt mode is the declared current memory mode.
- The current integration suite passes 989/989. Production and candidate both
  expose exactly 149 routes with route SHA-256
  `7688d82f198fc539db6fb4d2d1c6b4433e9fe9bb84c78da29d786548df671276`
  and OpenAPI SHA-256
  `efc724706c24741b734680767ea8350b57555f59c34b01919bdd844411694362`.
- The exact retirement package refuses to run without hash-bound backup,
  restore-test, and cron-retirement evidence. In a disposable clone it
  preserved both schemas when evidence was absent, then removed only
  `memory` and `memory_ingest_private` while all retained schema bytes,
  telemetry retention, and chat-history clearing remained unchanged.
- The GitHub workflow still invokes the obsolete governed-memory release guard
  and moved file/test paths. It must be replaced by a current SeeBx/Zep lane.

## Retain

| Asset | Current role | Required proof |
| --- | --- | --- |
| `seebx/adapters/zep_cloud.py` | Zep provider, retrieval, sync, and deletion transport | Owner/thread isolation, outage, deletion, and no-content logging tests |
| `seebx/adapters/zep_sync_postgres.py` | Durable Zep synchronization outbox | Restart, ordering, retry, idempotency, and erasure tests |
| `seebx/capabilities/conversation/zep_runtime.py` | One shared Zep runtime/controller | Startup/shutdown and configuration tests |
| `seebx/capabilities/conversation/zep_sync.py` | Synchronization worker contract | Queue and terminal-state tests |
| `ops/sql/20260818_conversation_zep_sync_outbox_v1*.sql` | Current durable synchronization schema and rollback | Exact migration and rollback receipt |
| owner-scoped PostgreSQL chat tables | Canonical chat record, not semantic memory | Row counts, RLS/ACL, trigger, and frontend parity |
| isolated LifeSwitch PostgreSQL | Nutrition, training, plans, measurements, and catalog | Schema, row, RLS/ACL, and route parity |

## Extract before retirement

| Current dependency | Required disposition | Proof before removal |
| --- | --- | --- |
| `rag_engine/zep_memory_provider_v1.py` | Move to the conversation capability/provider boundary and rename its generic `GovernedMemoryAssemblyV1` coupling | 45 Zep tests, conversation suite, exact route/OpenAPI parity |
| accepted block ID `governed_memory_successor_v1` | Remove after proving no current provider emits it; Zep keeps `zep_memory_v1` | Static producer/consumer closure and prompt/provenance tests |
| provenance name `governed_memory.response_provenance.v1` | Replace with a provider-neutral or Zep-specific current contract; version explicitly if bytes change | Hash/version migration tests; no silent approval transfer |
| `vantage_id` request and PostgreSQL column | Remove or rename through a paired frontend/backend migration; do not silently repurpose | Frontend caller audit, API contract version, DB migration and rollback |
| Verbal Sage `/api/identity` call to `/cards/{user_id}` | Replace with current Supabase/profile authority or remove the redundant write | Login, identity, owner isolation, and no-retired-route tests |
| Verbal Sage inspector Vantage cookies and `vantage_id` | Remove from the inspector request after confirming the replacement personalization controls | Inspector parity and permission tests |

The old `rag_engine/governed_memory/runtime/zep_deletion.py` is not an active
Zep dependency. Current conversation erasure uses
`seebx/adapters/zep_cloud.py`; the old file retires with its package.

## Retire from the active repository

The exact file list must be generated from the authorized retirement commit
and bound in the execution receipt. These are the controlling path families.
Counts are informative and can overlap where a test imports more than one
legacy family.

| Family | Verified size | Disposition |
| --- | ---: | --- |
| `rag_engine/memory_v1*` | 45 files, 26,764 lines | Delete after zero-import proof |
| `rag_engine/governed_memory/**` | 59 files, 29,715 lines | Delete after Zep provider extraction |
| `tools/governed_memory*` | 65 files, 68,262 lines | Archive receipt, then delete from active tree |
| `ops/governed_memory/**` | 57 files, 9,249 lines | Preserve one hash-bound offline archive, then delete from active tree |
| `governed-memory-migrations/**` | 27 files, 27,731 lines | Archive, then delete; never apply |
| `governed-migration-ci/**` | 36 files, 6,689 lines | Delete with retired CI lane |
| `tests/memory/**` | 98 files, 64,214 lines | Archive test inventory, then delete from current test tree |
| `tests/test_memory_v1*` | 151 files, 36,853 lines | Delete with the original memory chain |
| `scripts/memory_v1*` and Qdrant/Vantage scripts | exact list required in receipt | Delete after service/cron/import closure |
| card/fact/evaluation/initiator root programs | `card_jobs.py`, `fact_jobs.py`, `create_persona_cards.py`, `create_test_memory_card.py`, `eval_all_users.sh`, `eval_user_memory.py`, `initiator_daemon.py` | Delete after caller closure |
| Vantage/admin-memory/preference modules | exact list required in receipt | Delete after paired frontend route/permission cleanup |
| retired memory documents, evals, manifests, and backups | exact list and byte hash required | Move to offline archive; exclude from deployed checkout |

## Retire from SeeBx runtime and data plane

Each row is a separate rollback-bound batch. Code deployment does not authorize
database, cache, service, security-group, or backup deletion.

| Asset | Current evidence | Retirement gate |
| --- | --- | --- |
| legacy Qdrant configuration and AWS port 6333 rule | no process/listener/caller | Current source deployed; zero callers reconfirmed |
| legacy Redis container and AWS port 6379 rule | no live API client; 203 legacy keys | Export key-name/hash manifest, stop, observation window, then delete |
| Qdrant `eval_all_users.sh` cron | broken against absent `memory_raw` | Disable first; verify API/Zep/chat; remove later |
| `memory_extraction_v2_20260714_test` | 1,380 MB; 117 tables; zero connections/refs | Encrypted snapshot/export manifest, then drop |
| `memory_v1_pet_core_clone_20260724` | 1,405 MB; 229 tables; zero connections/refs | Encrypted snapshot/export manifest, then drop |
| `lifeswitch_training_family_stage_20260727` | 15 MB; 29 tables; zero connections/refs | Confirm isolated LifeSwitch parity, then drop |
| legacy Memory/Vantage schemas in active `memory` database | large and mixed with current platform schemas | Table/function/caller ledger; migrate current platform schemas first |
| static credentials in Compose and old backup | plaintext configuration debt | Rotate and externalize in a separate credential batch |
| stale AWS 5432/6379/6333 and unused 8080 rules | old source IP/SG references | Identify owners, remove exact rules, verify SSM/front-end access |

## Current CI authority

The replacement SeeBx guard must test only current runtime authority while
keeping historical retirement evidence immutable and non-gating:

1. compile `app.py`, every `seebx/**/*.py` file, and only the still-mounted
   transitional `rag_engine` modules;
2. run current Zep, conversation, identity/thread, search, voice, and
   LifeSwitch-domain suites;
3. reject imports or dynamic loads from retired memory/Qdrant/Vantage roots;
4. verify exact route and OpenAPI parity for refactors;
5. verify no provider credentials are required for offline tests; and
6. keep provider/live-data tests in a separately authorized lane.

The obsolete governed-memory release guard, package verifier, runtime manifest,
and 1,380-test historical suite do not become green by editing their frozen
hashes. They are archived and removed from the active workflow.

## Execution order

1. Move the live Zep prompt provider into `seebx` without behavior change.
2. Remove old block/provenance acceptance after producer/consumer proof.
3. Prepare the paired Verbal Sage identity/inspector cleanup in its own clean
   worktree; do not touch the current dirty production checkout.
4. Replace CI with the current SeeBx validation authority and run it from a
   clean, credential-free environment.
5. Generate an exact path/hash retirement receipt and offline recovery archive.
6. Delete both legacy memory chains from the candidate source tree.
7. Re-run current tests, static import closure, route/OpenAPI parity, and a
   production-equivalent private candidate.
8. Deploy through a separately authorized rollback-bound batch.
9. Disable and observe cron/Redis before deletion; remove stale AWS rules.
10. Migrate retained platform schemas, then archive/drop legacy schemas and
    clone databases in separately authorized data batches.
11. Create the encrypted clean-state snapshot and enforce the two-snapshot
    retention policy.

## Non-authority

This manifest authorizes no deployment, restart, deletion, database mutation,
credential rotation, AWS security-group change, or backup removal. Each action
requires an exact generated target list, current lease, recovery evidence, and
separate user authorization.
