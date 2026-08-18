# SeeBx target architecture v1

Status: controlling cleanup target; implementation not yet complete

Evidence date: 2026-08-17 America/Chicago (2026-08-18 UTC)

Authority source: SeeBx backend commit `49f9e60cf4321c8e42c359845c1a62a8c987614d`

## Purpose

SeeBx is the reusable backend engine. LifeSwitch is one product built on it.
The cleanup target is one auditable owner and one canonical execution flow for
each capability. Compatibility code, historical experiments, migrations, and
derived stores must not remain mixed into the active runtime package.

This document defines the target. It does not authorize deletion, migration,
deployment, restart, database writes, or production activation.

The plane ownership, allowed dependency direction, cleanup waves, file/object
classification fields, and product decision register are controlled by
`docs/SEEBX_STRUCTURAL_DESIGN_MAP_V1.md`.

## Verified starting point

- Production `app.py` is 1,985 lines and owns 36 routes directly; the isolated cleanup candidate is now 1,682 lines with 32 direct routes after moving attachment CRUD and message binding behind the conversation capability/adapter boundary.
- Eighteen additional routers are mounted.
- `rag_engine` contains 232 Python files and 113,531 lines.
- The live service/import roots reach 107 `rag_engine` files and 49,851 lines.
- The remaining 125 `rag_engine` files and 63,680 lines are dormant from the
  verified service, Git-sync, and voice-canary roots.
- The mounted response router reaches 72 internal modules and 33,999 lines for
  one route.
- The frontend contains 143 API route files, including callers for unmounted or
  retired backend capabilities.
- The live backend connects to two PostgreSQL instances: the older platform
  database on loopback port 5432 and the isolated LifeSwitch database on
  loopback port 55433.
- The live process still receives `QDRANT_URL`, but no live Python caller uses
  the Qdrant client and no Qdrant service or container is running.

## Architectural invariants

1. Supabase identity, a verified service credential, or an equally strong
   server-side identity is the owner authority. Request fields, browser state,
   cookies, model output, and stored content are never owner authority.
2. PostgreSQL is canonical for application and domain records. Zep owns only
   conversational memory. Search indexes, caches, vectors, and model context
   are derived or external provider data.
3. A capability has one public router, one application service, one set of
   contracts, and one declared persistence boundary.
4. Providers are reached only through adapters. Provider-specific objects and
   environment variables do not leak into capability code.
5. Compatibility paths are time-bounded adapters with named callers, tests,
   telemetry, and a retirement condition. They are never permanent alternate
   architectures.
6. Production, staging, Work Runner, and disposable test environments have
   separate credentials, data, networks, and execution authority.
7. Passing tests is candidate evidence. It does not authorize migration,
   deployment, secret release, restart, or deletion.
8. Historical evidence belongs in an archive or immutable release artifact,
   not in the active import path.

## Logical structure

```text
seebx/
  main.py                         # composition only; no domain SQL
  core/
    config.py                     # validated settings, no hidden fallbacks
    identity.py                   # Supabase/service identity verification
    ownership.py                  # owner context and RLS/session binding
    policy.py                     # capability authorization
    audit.py                      # append-only action/audit contracts
    errors.py                     # stable external error taxonomy
  capabilities/
    conversation/
    preferences/
    nutrition/
    training/
    measurements/
    plans/
    forms/
    search/
    voice/
    archive/
    work/
  adapters/
    postgres_platform.py
    postgres_lifeswitch.py
    supabase.py
    zep.py
    openai.py
    object_storage.py
    work_runner.py
  contracts/
    voice_language.py             # shared voice/search/response language policy
    api/
    events/
    jobs/
  migrations/
    platform/
    lifeswitch/
  tests/
    contract/
    integration/
    security/
```

The transition may retain existing paths temporarily, but new code must follow
this dependency direction:

```text
router -> capability service -> contract -> adapter -> external system
```

Adapters may depend on `core`. `core` must not import capabilities or adapters.
Capabilities must not import another capability's router or persistence
implementation.

## Canonical capability flows

### Conversation

```text
authenticated request
  -> owner/thread verification
  -> immutable conversation snapshot
  -> optional Zep context retrieval
  -> optional search/domain context
  -> prompt assembly
  -> model adapter
  -> durable response and provenance
  -> Zep update
  -> response
```

There is one response orchestrator. Versioned experiments remain outside the
live import graph until selected. A selected successor replaces the prior path;
it does not become another permanent branch.

### LifeSwitch domain data

```text
authenticated request
  -> owner-bound capability service
  -> LifeSwitch PostgreSQL transaction/RLS context
  -> nutrition, training, plan, or measurement schema
  -> stable response contract
```

Conversation may read domain data through a read-only domain-context contract.
It must not embed domain SQL in response orchestration.

### Search and current information

```text
authenticated request
  -> search policy
  -> provider adapter
  -> normalized sources
  -> trusted-web audit/cache
  -> caller
```

Current-news and trusted-web variants share this pipeline and differ through
policy/configuration, not duplicated provider implementations.

### Voice

Voice session authority, transcription, realtime transport, and synthetic
canary remain separate sub-capabilities. They share the same authenticated
conversation service and cannot bypass owner or persistence policy.

### Work Runner

SeeBx creates versioned work requests and receives signed receipts. Work Runner
owns sandbox construction and tool execution. SeeBx never grants production
authority merely because a disposable job succeeded.

## Data ownership

| Data | Canonical owner | Derived/external copy |
|---|---|---|
| Account identity and authentication | Supabase Auth | verified request context |
| Threads, messages, attachments, deletion receipts | platform PostgreSQL | Zep session/context |
| Response preferences | platform PostgreSQL | prompt projection |
| Nutrition, training, measurements, plans | LifeSwitch PostgreSQL | bounded prompt context |
| Conversational memory | Zep | prompt context/provenance hashes |
| Search audit and normalized sources | platform PostgreSQL | provider/cache objects |
| Archived documents | archive/object-storage capability, to be selected | extracted text/indexes |
| Work jobs and approvals | SeeBx control records | Work Runner workspace/receipt |

## Naming standard

- Use product-neutral names for reusable engine capabilities: `conversation`,
  `search`, `voice`, `archive`, `work`, `identity`, and `preferences`.
- Use `lifeswitch_` only for LifeSwitch domain contracts and schemas.
- Do not retain `brains`, `Verbal Sage`, `RESSE`, `Vantage`, `successor`,
  `memory_v1`, or version suffixes in final active component names unless they
  identify an external protocol version that must coexist.
- Version API and event contracts, not ordinary implementation filenames.
- Name one settings variable per store. Fallback from the LifeSwitch database
  to the platform database is prohibited in the final state.

## Build-versus-buy gate

Every undecided capability receives a written decision before implementation.
A proven product is preferred when the capability is commodity infrastructure,
reduces operational/security burden, has owner isolation and export support,
and can be replaced through a narrow adapter.

Custom code is retained when it expresses SeeBx authority, LifeSwitch domain
rules, response composition, approval policy, or Work Runner security policy.

Required comparison fields:

1. authority and tenant-isolation model;
2. data ownership, export, deletion, and retention;
3. availability, observability, and recovery;
4. integration complexity and vendor lock-in;
5. operating and usage cost;
6. local/disposable-test support;
7. failure behavior and adapter escape path.

The current decision register keeps Supabase Auth, Zep for conversational
memory only, rootless Podman, and the OpenAI model adapter; adopts
OpenTelemetry as the observability contract and AWS-managed configuration and
secret boundaries; pilots a PostgreSQL-native queue only after Work job
contracts exist; and defers Temporal until durable multi-restart workflows are
a demonstrated need. Supabase Postgres, archive, forms, the telemetry backend,
and the final job scheduler remain gated decisions. OpenAI Sandbox Agents is an
interface-alignment/pilot candidate because the capability is beta, not a
replacement for the current Work Runner policy and receipt boundary.

## Migration order

1. Remove false runtime dependencies and stale health/config surfaces.
2. Extract generic identity/ownership contracts from retired memory packages.
3. Reduce response orchestration to one Zep-aware canonical flow.
4. Consolidate duplicate search/current-news/trusted-web provider paths.
5. Restore or rebuild preferences, export, and forms only behind current owner
   authority.
6. Move the required platform schemas/functions into a clean platform database
   and remove the retired memory-outbox dependency from chat deletion.
7. Move the food catalog to one canonical database and update all callers.
8. Prove zero live imports, routes, SQL, services, timers, and frontend callers
   for retired packages and schemas.
9. Archive evidence and delete retired code/data in separately authorized,
   rollback-safe batches.
10. Snapshot, verify Git/GitHub, and connect Work Runner only through the new
    versioned contracts.

## Completion proof

The cleanup is complete only when a generated import/route/SQL/service map
shows one canonical flow per capability; every retained object appears in the
component ledger; every retirement has backup and rollback evidence; focused
and full tests pass; production health and frontend behavior are verified; Git
and GitHub match; encrypted recovery artifacts exist; and no active setting or
service points at Qdrant, Vantage, RESSE, governed-memory, or `memory_v1`.
