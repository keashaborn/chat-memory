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
- The daily `eval_all_users.sh` cron targeting absent Qdrant `memory_raw` was
  retired on 2026-08-19. The exact before/after crontabs, unchanged evaluator/log
  hashes, authenticated health proof, and rollback receipt are preserved under
  `/var/backups/seebx-cleanup/20260819T141946Z-legacy-qdrant-cron-retirement/`.
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
| `vantage_id` request and PostgreSQL column | Paired candidates now remove the final frontend inspector caller, remove thread/transcript inputs, and stop new conversation/telemetry writes. Historical columns and rows remain read-only pending exact recovery evidence; the security policy retains `vantage_id` only as an explicitly ignored legacy control. Do not silently repurpose it. | `SEEBX_VANTAGE_RETIREMENT_BOUNDARY_V1.md`, exact backup and restore receipt, deployed zero-write soak, non-cascading DB migration and rollback |
| Verbal Sage `/api/identity` call to `/cards/{user_id}` | Removed in paired candidates `e26d296b` and `acec9d10`; Supabase metadata remains account identity authority | Ten frontend tests and ten backend retirement/erasure tests pass; production move remains separately gated |
| Verbal Sage inspector Vantage cookies and `vantage_id` | Candidate `ca2c7fe` deletes the entire uncalled inspector route after proving there is no UI caller and no cleaned-backend `/vantage/query` route | 360 frontend tests and the isolated production build pass; production move remains paired and separately gated |

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
| Qdrant `eval_all_users.sh` cron | exact line absent since 2026-08-19; evaluator/log hashes unchanged; rollback crontab retained | COMPLETE; continue observing API/Zep/chat before any schema deletion |
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


## Executable recovery-evidence gate

`scripts/prepare_legacy_memory_retirement_recovery.py` is the canonical recovery tool for the database containing the two legacy schemas. It is candidate-only and has not been run against production. When separately authorized, it will:

1. require the local `memory` database and `sage` administrative role;
2. export a repeatable-read PostgreSQL snapshot and exact per-table row-count manifest;
3. create a full custom-format database dump so extensions and retained-schema dependencies are recoverable;
4. restore the full dump into a uniquely named empty disposable database;
5. compare all 167 ordinary tables and every exact row count;
6. drop and independently prove removal of the disposable database; and
7. emit content-free SHA-256 bindings for the dump, source manifest, tool, retirement SQL, package, and restore receipt.

The DSN and password never enter a subprocess argument or receipt. A temporary mode-0600 pgpass file is removed in all outcomes. A failure emits only fixed error codes, retains any completed backup artifact, and attempts to remove the disposable database before exiting. Running the tool creates a backup directory and creates/drops a temporary database, so it requires a separate production authorization.

## Executable retirement candidate (2026-08-21)

The retirement package now contains executable transaction-bound SQL, but remains `candidate_not_applied` with `retirement_authorized=false`. It will acquire an advisory lock and abort before either schema drop unless all of the following are true: exact schema/table shape; zero nonterminal ingest/erasure work; no legacy triggers; active Zep-backed clear functions; 190/32/158 attestation classification with all 32 eligible rows reconciled; zero external function, view, materialized-view, trigger, or foreign-key references; exact backup and disposable-restore hashes; and hash-bound reconciliation, encrypted-quarantine, AWS Secrets Manager custody, and dependency-catalog receipts.

The quarantine key contract binds an immutable Secrets Manager ARN/version, retrieval principal, successful recovery time, and SHA-256 fingerprint of the exact 32-byte key. It stores no secret value. No AWS secret, database role, grant, migration, schema drop, service change, or production file was created or changed by this candidate batch.

## Candidate verification evidence (2026-08-21)

- Base candidate commit: `dcf7b2092800b3d875a72fdd9d1c264b5f649c05`; production was not edited.
- Key-custody schema SHA-256: `442ae5d7006e860cd4a5eab999ee268ab8f6536d8625d4d2c52c2fbf3f66c1ce`.
- Reconciliation tool SHA-256: `cf35c1dd0885ae11c05d1e44e347b7f85193758f59e77193b8911a65e8c7028b`.
- Clean-backend preflight SHA-256: `87e8a861de1734aa8adb37a607c61172e66858eec764503f82d32f24e9d21e90`.
- Executable retirement SQL SHA-256: `00a82f89c47c68002cae61546adafcf5264b3507f2f296dd22829d09cf1f19e3`.
- Focused custody/preflight/retirement tests: 17/17 pass.
- Full backend suite in the verified pinned Python 3.12/Pydantic 2.12.3 runtime: 1,235/1,235 pass.
- Disposable PostgreSQL 16 positive execution: exact 160-table `memory`, 7-table `memory_ingest_private`, and 190/32/158 attestation fixture passed every assertion; only the two legacy schemas were dropped; Zep outbox and both chat-clear functions remained.
- Disposable PostgreSQL 16 negative execution: an external `public` view produced `external legacy dependencies remain`; the transaction aborted and both legacy schemas and the outside view remained.
- Package hashes, Python compilation, and `git diff --check` pass. No AWS secret, live database role/grant, migration, schema, service, frontend, or production source was changed.
