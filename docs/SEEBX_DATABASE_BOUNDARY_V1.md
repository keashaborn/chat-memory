# SeeBx database boundary v1

Status: target and migration constraints; no database-change authority

Evidence date: 2026-08-20 America/Chicago

Source commit: `49f9e60cf4321c8e42c359845c1a62a8c987614d`

## Current connections

The live `brains.service` process was inspected without exposing credentials.

| Setting | Endpoint | Current purpose |
|---|---|---|
| `POSTGRES_DSN` | PostgreSQL `127.0.0.1:5432/memory` | chat/platform, search audit/cache, usage, legacy schemas |
| `LIFESWITCH_POSTGRES_DSN` | PostgreSQL `127.0.0.1:55433/lifeswitch` | isolated LifeSwitch domain data |
| `QDRANT_URL` | HTTP `127.0.0.1:6333` | stale production setting; no caller or runtime; immutable candidate explicitly unsets it |

The cleanup candidate uses `seebx/adapters/lifeswitch_postgres.py` and requires
`LIFESWITCH_POSTGRES_DSN` for every new connection. It does not fall back to
`POSTGRES_DSN`; a missing domain credential fails closed rather than silently
crossing stores.

## Current database inventory

Row figures are PostgreSQL catalog estimates except where explicitly described
as exact counts. They are inventory evidence, not deletion authority.

### Older platform/legacy database: `memory`

| Schema | Tables | Estimated rows | Views |
|---|---:|---:|---:|
| `ai_operations` | 4 | 0 | 0 |
| `catalog_dev` | 14 | 1,610 | 0 |
| `chat_history_private` | 2 | 0 | 0 |
| `chat_integrity` | 1 | 101 exact rows | 0 |
| `lifeswitch_usage` | 2 | 581 | 0 |
| `memory` | 160 | 33,014 | 6 |
| `memory_ingest_private` | 7 | 1,167 | 0 |
| `public` | 15 | 3,159 | 0 |
| `trusted_web` | 4 | 307 | 1 |
| `user_settings` | 2 | 20 exact rows | 0 |

Exact bounded counts verified during the audit:

- `public.chat_log`: 1,045 rows;
- `public.threads`: 49 rows;
- `public.chat_attachments`: 0 rows;
- `memory_ingest_private.memory_ingest_outbox`: 2 rows;
- nonterminal `memory_ingest_outbox` rows: 0;
- `user_settings.assistant_response_preference_v1`: 1 row;
- `user_settings.assistant_response_preference_compilation_candidate_v1`: 19 rows.

The five Vantage schemas are absent from the current platform database. No Vantage service or executable Python root remains.

### Isolated LifeSwitch database: `lifeswitch`

| Schema | Tables | Estimated rows | Views |
|---|---:|---:|---:|
| `catalog_dev` | 14 | 1,711 | 0 |
| `lifeswitch_chat` | 3 | 2 | 0 |
| `lifeswitch_nutrition` | 10 | 1,516 | 0 |
| `lifeswitch_plan` | 3 | 0 | 0 |
| `lifeswitch_snapshot` | 2 | 0 | 0 |
| `lifeswitch_training` | 15 | 2,263 | 3 |
| `public` | 1 | 12 | 0 |

## Verified live SQL surface

Static analysis started from the three deployed execution roots:

- `app` (`brains.service`);
- `tools.git_daily_sync`;
- `scripts.voice_synthetic_canary`.

The live graph directly references these older-database areas:

- `public`: threads, chat log, attachments, active-thread selection,
  telemetry, and voice-session lease;
- `chat_history_private`: clear-history and clear-message-tail functions;
- `chat_integrity`: assistant transcript attestations;
- `trusted_web`: request windows, retrieval audit, cache, and response
  transcript;
- `lifeswitch_usage`: actor registry and usage events;
- `ai_operations`: incident monitoring;
- `catalog_dev`: the standalone catalog router;
- `user_settings`: production still has an unmounted preferences router and
  exactly twenty retained rows; the cleanup candidate restores the authenticated
  owner-scoped route and direct PostgreSQL adapter, but remains undeployed and
  does not yet project preferences into response composition.

The live graph directly references these isolated LifeSwitch areas:

- `lifeswitch_chat`: owner read context, timezone, domain read functions, and
  answer provenance;
- `lifeswitch_nutrition`: nutrition log, foods, servings, meals, and context;
- `lifeswitch_training`: sessions, sets, conditioning, roles, and context;
- `lifeswitch_plan`: legacy plan-profile fallback;
- isolated `catalog_dev`: nutrition and personal-food creation;
- isolated `public`: measurement entries.

No direct live Python SQL reference to the 160-table `memory` schema or the five
Vantage schemas was verified. String names inside Zep provenance are not SQL
references.

## Hidden database-function coupling

`chat_history_private.clear_history` and
`chat_history_private.clear_message_tail` are secure-definer functions with
owner/session checks and replay receipts. Both still lock, inspect, and delete
from `memory_ingest_private.memory_ingest_outbox` before deleting chat rows.

That dependency is obsolete after the Zep cutover, but it cannot be removed by
dropping the schema first. The functions must be replaced transactionally with
versions that preserve:

- `brains_app` caller restriction;
- verified `app.user_id` and auth-context binding;
- supported selector validation;
- owner-scoped target selection;
- deterministic replay protection;
- thread/message/attachment/search/attestation cleanup behavior;
- deletion counts and cryptographic receipt semantics.

The two terminal outbox rows are migration evidence. They are not active work.

## Target stores

### Platform PostgreSQL

Owns reusable SeeBx platform data:

- threads and messages;
- attachments and processing state;
- active-thread selection;
- response provenance and integrity receipts;
- trusted-web audit/cache/transcripts;
- response preferences;
- platform usage and operational audit;
- deletion/export receipts;
- voice-session authority when not moved to a dedicated ephemeral store.

The physical host may remain self-managed during cleanup or later move behind a
managed PostgreSQL adapter. Logical ownership and migration contracts must not
depend on that infrastructure choice.

### LifeSwitch PostgreSQL

Owns only LifeSwitch domain data:

- nutrition and personal foods;
- training and conditioning;
- measurements;
- plan state and domain snapshots;
- the one canonical food catalog if catalog is product-scoped.

It uses a distinct mandatory DSN and least-privilege roles. Conversation reads
through owner-bound domain read contracts rather than direct cross-capability
SQL.

### Zep

Owns conversational memory only. It does not own chat transcripts, nutrition,
training, plans, archive documents, user identity, or deletion authority.

### Archive/object storage

Owns original archived documents and immutable content objects after a product
decision. PostgreSQL stores owner-scoped metadata and processing state. Search
indexes and extracted text remain rebuildable derivatives.

## Migration gates

1. Freeze table/function/role/RLS/grant inventories and encrypted backups.
2. Replace the chat-clear functions without the memory-outbox dependency in a
   disposable database; prove success, failure, replay, and cross-owner denial.
3. Create a clean platform database from versioned migrations, not from an
   unreviewed whole-database restore.
4. Copy required platform rows with exact counts, primary/foreign keys,
   timestamps, ownership, hashes where available, and sequence state.
5. Verify functions, views, roles, grants, RLS, and service credentials.
6. Cut a candidate service to the clean platform database and run contract,
   integration, security, deletion, export, search, voice, and frontend tests.
7. Cut the catalog router to the isolated canonical catalog and prove row and
   behavior parity.
8. Deploy only with an encrypted pre-cutover snapshot and tested rollback.
9. Observe stability before removing old credentials or containers.
10. Archive and remove retired schemas/database only through a separately
    authorized manifest with exact targets and post-deletion verification.

## Required zero-dependency proof before retirement

The old `memory`, `memory_ingest_private`, Vantage, duplicate catalog, and old
database container remain protected until all of the following return zero:

- imports from deployed roots;
- mounted routes and direct routes;
- frontend callers;
- SQL and database-function dependencies;
- services, timers, cron jobs, containers, and processes;
- configured environment variables and secrets;
- queued or nonterminal work;
- required retention/legal/export obligations;
- rollback references that have not been superseded by an encrypted clean-state
  backup.
