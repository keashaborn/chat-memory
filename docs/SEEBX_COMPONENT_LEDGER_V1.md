# SeeBx component ledger v1

Status: cleanup control ledger; no retirement authority

Evidence date: 2026-08-17 America/Chicago (2026-08-18 UTC)

Source commit: `49f9e60cf4321c8e42c359845c1a62a8c987614d`

## Dispositions

- **KEEP**: correct capability and ownership boundary; tighten as needed.
- **CONSOLIDATE**: necessary behavior split among overlapping paths.
- **EXTRACT**: retain generic logic while removing retired-package coupling.
- **REBUILD**: useful product capability whose implementation is unsafe or
  obsolete.
- **DECIDE**: product requirement or build-versus-buy decision is unresolved.
- **ARCHIVE**: preserve immutable evidence outside the active repository.
- **RETIRE**: proven unused, but deletion still requires a separate manifest,
  backup, authorization, and verification.

## Runtime and platform

| Component | Verified current state | Target owner | Disposition | Gate |
|---|---|---|---|---|
| `app.py` composition/root routes | Live; 1,985 lines; 36 direct routes | `main.py` plus capability routers | CONSOLIDATE | Move SQL and product behavior out; preserve API contracts until frontend cutover |
| Supabase actor verification | Candidate `a20e9634` separates request identity/owner binding into `seebx.core.identity` and `seebx.core.ownership`, and issuer/JWKS/JWT verification into `seebx.adapters.supabase`; the legacy `rag_engine` authority modules are removed from the candidate | core identity/ownership plus provider adapter | EXTRACTED; PRODUCTION MOVE PENDING | 159 directly affected tests, 298 search/trusted-web tests, compilation, and exact 149-route/OpenAPI parity pass; seven failures in `tests.memory.test_exclusive_cutover` are identical at pre-batch commit `f7af5433` and assert already-retired memory surfaces |
| `memory_actor_auth_v1.py` | Batch 02 candidate reduces it to a compatibility re-export; live callers use `seebx.core.identity` directly | `seebx.core.identity` | EXTRACTED; compatibility RETIRE pending | Candidate `fb252149`; prove dormant callers migrated before deleting wrapper |
| Platform PostgreSQL connection | Live on `127.0.0.1:5432`, database `memory` | clean platform PostgreSQL | CONSOLIDATE | Migrate only required schemas/functions and prove exact row/ACL parity |
| LifeSwitch PostgreSQL connection | Live on `127.0.0.1:55433`, database `lifeswitch` | LifeSwitch domain PostgreSQL | KEEP | Remove fallback to `POSTGRES_DSN`; retain isolated credentials |
| Redis container | Running; no `REDIS_URL` in the live service, no live process connection, 203 keys/~1.07 MB | none unless a scheduled consumer is proven | RETIRE CANDIDATE | Inventory key types/TTL and every timer/script before a rollback-safe stop; retain only if a named capability needs it |
| Qdrant runtime surface in `app.py` | Imported/configured but no live caller; no service/container | none | RETIRE | Batch 01 removes code/health surface; later remove dependency/config after dormant artifacts are archived |
| Shared OpenAI SDK client | Candidate moves the OpenAI-only client, model allowlist, embeddings, and compatibility helpers from `rag_engine` to `seebx.adapters.openai`; live runtime has no `VANTAGE_MODEL` setting | provider adapter | EXTRACTED; PRODUCTION MOVE PENDING | Candidate `0a1a4ba2`; key fingerprinting permits in-process credential rotation without plaintext cache keys; 139 affected live tests, 154 search tests, compilation, and exact route/OpenAPI parity pass |
| `requirements-ci.txt` and `pyproject.toml` | CI dependencies include retired systems; package metadata describes governed-memory successor | SeeBx application package | REBUILD | Inventory real runtime/test dependency sets before replacement |
| Git daily sync | Active timer/service root | operations | KEEP | Verify remote/branch policy and avoid competing writer paths |
| Legacy `eval_all_users.sh` cron | Runs daily at 03:00; reads absent Qdrant `memory_raw`; recent runs do no useful work | none | RETIRE CANDIDATE | Back up the exact crontab and logs, remove only the one line, then prove no replacement scheduler depends on it |
| Voice synthetic canary | Active hourly but failed because its request used a field intentionally rejected by the live TTS route | voice operations | KEEP + REPAIR | Candidate `e7005538` aligns it with server-owned `conversation_style`; deploy and run one bounded canary before clearing historical failure state |

## Conversation and memory

| Component | Verified current state | Target owner | Disposition | Gate |
|---|---|---|---|---|
| Conversation response router | Candidate `b56d0e84` moves the mounted route from `rag_engine/resse_response_router.py` to `seebx/capabilities/conversation/router.py`, moves its test module, and leaves no compatibility wrapper; the external route, request schema, operation ID, and summary remain byte-for-byte OpenAPI compatible | `capabilities.conversation` | CANONICAL PLACEMENT COMPLETE; ORCHESTRATOR CONSOLIDATION CONTINUES | 117 bounded tests have exactly the same two pre-existing legacy failures before and after; compilation plus exact 149-route and OpenAPI hash parity pass; next split provider/composition selection by declared role without changing the public API |
| Response composition v0.2/v0.4 chains | Both are live: the LifeSwitch v0.4 layer wraps the required v0.2 base in the deployed mode | conversation service | CONSOLIDATE + LOGICAL RENAME | Freeze exact live behavior; name the generic base and LifeSwitch wrapper by role; remove only branches proven outside the mounted closure rather than deleting v0.2 |
| Zep provider/runtime | Production prompt mode is on; candidate `25be0d0a` gives `app.py` and the response router one `seebx.capabilities.conversation.zep_runtime` singleton/settings owner | conversation capability with a future narrow `adapters.zep` provider boundary | KEEP; SHARED RUNTIME EXTRACTED; PRODUCTION MOVE PENDING | 66 focused conversation/Zep/erasure/response tests, all 52 `test_zep*.py` tests, compilation, and exact 149-route/OpenAPI parity pass; three broader stale tests fail identically at pre-batch `dd701e14`; still prove authenticated owner/thread binding, outage, export/retention, deletion compensation, and provenance before deployment |
| Old successor-memory adapter | Live only for excluded/no-memory surfaces | no-memory context provider | EXTRACT then RETIRE | Replace with explicit `NoMemoryContext` contract |
| Governed-memory package | 56 files; 16 reachable through response/auth coupling, 40 dormant | none after extraction | EXTRACT then ARCHIVE/RETIRE | Zero live imports and migration/runtime callers; 10 provider-test errors at pre-adapter candidate `000fc2b2` still assert retired `memory_input`/`memory_application` fields and must be reconciled with retirement rather than treated as current adapter regressions |
| `memory_v1*` code and active-runtime manifest | 45 files/26,764 lines in `rag_engine` have no live roots; the so-called active-runtime manifest declares 39 `memory-v1-*` units, but the server now has zero installed `memory-v1-*` units and the manifest/verifier has only test/script consumers, with no live service or timer reference | none | ARCHIVE then RETIRE | Preserve the hash-bound manifest as historical evidence; retire it only through an explicit manifest after zero frontend and operational callers are reconfirmed; do not refresh its hashes to make obsolete architecture look current |
| Vantage code/routers | Unmounted/dormant | none | ARCHIVE then RETIRE | Confirm frontend `/vantage/query` caller removal and preserve required audit evidence |
| RESSE policy/evaluation code | Mostly dormant; live router name is misleading | evaluation archive | RENAME live component; ARCHIVE/RETIRE dormant code | Preserve any controlling decision artifact outside active imports |
| Direct cards/Vantage routes in `app.py` | Mounted but return retired conflict responses | none | RETIRE after frontend cutover | Remove `/api/identity` and inspect callers first |
| Chat/thread routes | Live and PostgreSQL-backed | conversation capability | KEEP + CONSOLIDATE | One authenticated router/service; preserve thread semantics |
| Conversation persistence | Candidate `d8d40adc` gives finalized responses and search exchanges one canonical `seebx.capabilities.conversation.persistence` owner; stored web source identifiers move to `seebx.contracts.conversation` without changing deployed values | conversation capability and shared contracts | CONSOLIDATED; PRODUCTION MOVE PENDING | 59 direct persistence/provenance/response tests, 157 search tests, compilation, and exact route/OpenAPI parity pass; retain atomic owner/thread checks, evidence receipt, ordering, and resume promotion |
| Chat history clear | Live; secure DB functions; hidden dependency on legacy memory outbox | conversation deletion service | CONSOLIDATE | Replace outbox dependency only after Zep deletion and replay semantics are tested |
| Full chat + Zep clear | Live | conversation deletion service | KEEP | Verify atomic/compensating behavior and owner isolation |
| Attachments | Candidate `988596bd` moves the complete validation/rendering contract from `rag_engine` to `seebx.capabilities.conversation.attachments` and the response-time owner/thread/message-bound SQL to `seebx.adapters.conversation_attachments`; direct CRUD, message binding, and thread-read SQL remain in `app.py` | conversation capability plus PostgreSQL adapter | CONTRACT + RESPONSE READ EXTRACTED; API ROUTES CONSOLIDATE | Exact normalized SQL and owner-context parity pass, 31 focused and 79 broader tests pass, 149-route/OpenAPI hashes match production, and three legacy compatibility failures are identical at pre-batch `f0c89e50`; next extract CRUD/binding/thread-read transactions while preserving the stored-first contract |
| User export | Route exists but returns retirement conflict; frontend still calls it | privacy/export capability | REBUILD | Owner-scoped export contract, bounded data, audit, and frontend test |

## Search, retrieval, and catalog

| Component | Verified current state | Target owner | Disposition | Gate |
|---|---|---|---|---|
| Trusted-health search profile | Candidate mounts `seebx.capabilities.search.trusted_health`; provider, policy, registry, evidence, admission, and audit have one canonical owner | search capability | KEEP | Preserve approved domains, NCBI/ODS behavior, source evidence, and audit semantics through deployment verification |
| Search executor | Candidate owns plan, budget, authorization, and execution under `seebx.capabilities.search`; shared language, OpenAI, transcript persistence, request identity, and voice-session authority now use their canonical contract, adapter, conversation, core, and voice owners | search capability | KEEP; SHARED BOUNDARIES EXTRACTED | Preserve server-owned budgets and exact verified owner behavior through authenticated candidate and deployment tests |
| Current-news search profile | Candidate expresses current news through the same provider, registry, evidence, admission, audit, and runtime boundaries | search capability policy profile | KEEP | Preserve freshness/source policy and exact public contract; do not recreate a separate engine |
| Legacy search-owned `rag_engine` modules | Candidate removes or moves 14 router, authorization, plan, budget, evidence, audit, provider, policy, registry, NCBI, and ODS modules; no candidate caller imports those paths | none | RETIRE CANDIDATE COMPLETE | Keep Git rollback; verify deployment import graph, routes, authenticated behavior, ODS timer, and stored audit records before production retirement is accepted |
| Legacy RAG/vector modules | Mostly dormant with Qdrant/Vantage dependencies | none or offline evaluation | ARCHIVE then RETIRE | Generated import graph and no runtime settings/services |
| `catalog_router.py` | Live against old platform `catalog_dev` | catalog capability in LifeSwitch database | MIGRATE + CONSOLIDATE | Repoint after row/function parity; do not keep two writable catalogs |
| LifeSwitch nutrition catalog access | Live against isolated database `catalog_dev` | same catalog capability | KEEP | Becomes sole catalog after callers cut over |

## LifeSwitch domain capabilities

| Component | Verified current state | Target owner | Disposition | Gate |
|---|---|---|---|---|
| Nutrition router | Live; 17 routes/1,503-line closure plus overlapping catalog paths | `capabilities.nutrition` | KEEP + SPLIT | Separate log, foods, meal plans, and catalog contracts |
| Training router | Live; 41 routes/2,956-line closure | `capabilities.training` | KEEP + SPLIT | Separate sessions, exercises, plans, and analytics without duplicate endpoints |
| Measurements | Live | `capabilities.measurements` | KEEP | Retain owner checks and isolated PostgreSQL |
| Plan/domain context | Live across multiple readers/adapters | `capabilities.plans` | CONSOLIDATE | One read model and one write authority; retire legacy fallback |
| LifeSwitch snapshots | Installed schema; currently empty | plans/recovery if required | DECIDE | Prove product caller and retention need |
| Agentic plan observation | Live import path | plans read model | CONSOLIDATE | Move SQL behind domain adapter; no response-router SQL |
| Duplicate nutrition batch routes | Live candidate duplication | nutrition service | CONSOLIDATE | Frontend caller trace and response equivalence |

## Preferences, forms, administration, and archive

| Component | Verified current state | Target owner | Disposition | Gate |
|---|---|---|---|---|
| Assistant response preferences | Backend router is owner-aware but unmounted; frontend calls it | preferences capability | RESTORE through refactor or RETIRE | Determine intended product behavior; do not remount implicitly |
| `user_settings` schema | Two empty tables in old database | platform preferences | MIGRATE/REBUILD if retained | Current authenticated API and schema contract |
| Forms router | Unmounted; trusts request-provided owner ID | forms capability | REBUILD or RETIRE | Never remount current implementation; require verified identity/RLS |
| Frontend forms surfaces | Active callers exist | forms capability | DECIDE | Product decision plus migration/removal UX |
| Telemetry router | Live and large/custom | observability capability | CONSOLIDATE behind OpenTelemetry contract | Define redacted trace/request/job correlation; select storage/alert backend separately |
| AI operations incidents | Live routes; four empty tables | operations | KEEP or REPLACE | Confirm consumer and alert delivery; product comparison |
| Admin memory workbench/health routes | Mounted retirement responses | none | RETIRE | Remove frontend/admin callers and historical tests first |
| Archive/document library | Separate planned capability; not chat memory | archive capability | DECIDE | Product evaluation for storage, extraction, mapping, export, and deletion |

## Voice

| Component | Verified current state | Target owner | Disposition | Gate |
|---|---|---|---|---|
| Voice session lease | Candidate `381704fb` removes the legacy combined router: `seebx.core.voice_identity` owns request/session authority, `seebx.adapters.voice_session` owns PostgreSQL lease operations, and `seebx.capabilities.voice.session` owns the unchanged HTTP contract | voice capability | EXTRACTED; PRODUCTION MOVE PENDING | 64 full voice tests, 41 authority-reference tests, 298 search/trusted-web tests, compilation, exact 149-route/OpenAPI parity, and all eight normalized lease SQL statements pass/match |
| Voice-language contract | Candidate moves the shared catalog, validation, request header, response instruction, and transcription prompt from `rag_engine` to `seebx.contracts.voice_language` | shared contracts | EXTRACTED; PRODUCTION MOVE PENDING | Candidate `f8b73bf1`; 111 direct voice/response tests, 154 search tests, 62 voice tests, and exact route/OpenAPI parity pass; production still requires reviewed deployment |
| Transcription router | Live; 18-module/7,922-line closure | voice transcription adapter | CONSOLIDATE | Remove unrelated response/memory dependency drag |
| Realtime voice preview | Live; 19-module/8,872-line closure | voice realtime adapter | CONSOLIDATE | Same conversation authority; bounded preview path |
| Synthetic canary | Active timer | voice operations | KEEP | Synthetic-only data and clear alert ownership |

## Work Runner

| Component | Verified current state | Target owner | Disposition | Gate |
|---|---|---|---|---|
| SeeBx job request/approval contracts | Not yet connected to cleaned backend | work capability | BUILD | Versioned contracts, explicit actor/target/tool authority |
| Work Runner sandbox manager | Separate server and repository | Work Runner | KEEP + CONTINUE | Disposable frontend/backend copies, no production credentials/data |
| Queue/scheduler | Not selected | work adapter | PILOT Supabase Queues/`pgmq`; Temporal deferred | Immutable job/receipt contracts first; failure/idempotency/load test before selection |

## Database schema disposition summary

| Current database/schema | Current direct or indirect use | Disposition |
|---|---|---|
| old `public` | chat, threads, attachments, telemetry, voice lease | MIGRATE to clean platform database |
| old `chat_history_private` | live deletion functions | MIGRATE after outbox decoupling |
| old `chat_integrity` | response attestation/snapshot | MIGRATE |
| old `trusted_web` | live search cache/audit/transcripts | MIGRATE |
| old `lifeswitch_usage` | live usage ledger | MIGRATE, likely rename platform usage |
| old `ai_operations` | live admin code; no estimated rows | DECIDE/possibly migrate |
| old `catalog_dev` | live catalog router; duplicate of isolated catalog | CONSOLIDATE into isolated catalog |
| old `user_settings` | router unmounted; empty | REBUILD/MIGRATE only if preferences retained |
| old `memory_ingest_private` | two terminal outbox rows; indirect deletion-function dependency | DECOUPLE, archive evidence, retire |
| old `memory` | 160 tables; no verified live SQL reference | ARCHIVE then RETIRE after zero-dependency proof |
| five old Vantage schemas | no verified live Python reference; historical data present | ARCHIVE then RETIRE after exact backup/approval |
| isolated `lifeswitch_*` schemas | live canonical domain data | KEEP |
| isolated `catalog_dev` | live canonical domain catalog candidate | KEEP and become sole catalog |

## Ledger maintenance rule

No component is deleted because it looks old. A retirement row advances only
after static imports, mounted routes, frontend callers, SQL/functions, services,
timers, environment settings, data retention, backups, tests, and rollback are
all explicitly resolved. Newly discovered components are added here before any
mutation batch is approved.
