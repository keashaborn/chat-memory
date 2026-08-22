# SeeBx database boundary v1

Status: target and migration constraints; no database-change authority

Evidence date: 2026-08-22 America/Chicago

Production source commit: `49f9e60cf4321c8e42c359845c1a62a8c987614d`

Candidate database-audit commit:
`fdffcbe0cecb5142d2ebc560e2ea1232394bddff`

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

The platform figures below are exact counts from the content-free 2026-08-22
governance receipt. They are inventory evidence, not deletion authority, and
must be frozen again at migration time.

### Older platform/legacy database: `memory`

| Schema | Tables | Exact rows | Other relations |
|---|---:|---:|---:|
| `ai_operations` | 4 | 58 | 0 |
| `catalog_dev` | 14 | 1,711 | 0 |
| `chat_history_private` | 2 | 22 | 0 |
| `chat_integrity` | 1 | 101 | 0 |
| `lifeswitch_usage` | 2 | 708 | 0 |
| `memory` | 160 | 35,118 | 6 views |
| `memory_ingest_private` | 7 | 1,216 | 0 |
| `public` | 14 | 3,676 | 1 sequence |
| `trusted_web` | 4 | 300 | 1 view |
| `user_settings` | 2 | 20 | 0 |
| **Total** | **210** | **42,930** | **8** |

Exact bounded counts verified during the audit:

- `public.chat_log`: 1,117 rows;
- `public.threads`: 53 rows;
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

The committed candidate consumer audit additionally proves that the old
platform `catalog_dev` has zero candidate runtime consumers. Three LifeSwitch
catalog/foods adapters were explicitly excluded from platform retention because
they use only the isolated `LIFESWITCH_POSTGRES_DSN`. See
`SEEBX_PLATFORM_DATABASE_INVENTORY_V1.md` for the bound evidence.

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

## Disposable restore and owner/RLS proof (2026-08-21)

The canonical root-only verifier created a uniquely named database from
`template0`, restored the current isolated LifeSwitch database, and matched the
complete content-free schema/ACL/RLS/function/policy/extension manifest plus
exact counts for 48 tables and 5,590 rows. Five owners across 33 protected
surfaces produced 165 administrator-versus-application comparisons. Three
cross-owner writes and direct snapshot access were denied; catalog access
succeeded. Delegation and People integration were both configured off and were
verified fail closed. The clone was dropped and its absence independently
proved. See `SEEBX_LIFESWITCH_DISPOSABLE_DATABASE_VERIFICATION_V1.md` for exact
hashes and limitations.

This closes the current-database backup/restore and disabled-delegation RLS
gate. It does not close clean-install migration, enabled-delegation, paired
frontend authentication, application-route, release, or cutover gates.

## Relation/function consumer proof (2026-08-21)

The committed content-free auditor maps the exact isolated-database manifest
to candidate runtime, operational, migration, and database-internal consumers.
It inventoried 218 relations/functions and 211 dependency edges. Fifty-seven
objects are application-direct, 32 are reachable database internals, and 118
are extension-owned. One object is operational-reference-only, five are
migration-only, and five have no verified consumer. Operational verifier
references are informational and never seed runtime retention.

The review-gated set is confined to dormant normalized catalog muscle/food
surfaces plus two recovery snapshot relations. It is preserved pending explicit
retain/archive/retire decisions. See
`SEEBX_LIFESWITCH_DATABASE_CONSUMER_AUDIT_V1.md` for exact identities, hashes,
row-count-only evidence, and limitations. A migration-only or unproven result
is a review gate, not deletion authority.

## Platform database consumer/governance proof (2026-08-22)

The database-aware candidate audit inventoried 794 platform-database
relations/functions and 3,382 dependency edges. Twenty objects are
application-direct, 26 are reachable database internals, and 118 are
extension-owned. Thirteen are migration-only, 18 are
operational-reference-only, and 599 have no candidate runtime or reachable
consumer. The 210 exact table counts total 42,930 rows; 165 unproven tables hold
36,234 of those rows, principally the retired custom-memory attempts.

The same receipt freezes 10 schemas, 218 relations, 2,867 columns, 576
functions, 315 policies, 232 non-internal triggers, 2,302 constraints, 777
indexes, five extensions, 42 non-system roles, nine memberships, and one
sequence. Definitions are stored only as SHA-256 values. The governance
manifest SHA-256 is
`3394fc40c3fe29e3d4b4bd029acbb76059b02c45bae8e1a9bd7f8a8e0d121cb8`.

This closes the current platform inventory gate. It does not close retained
object selection, Forms disposition, chat-clear/outbox decoupling, clean
installation, disposable restore, paired application verification, migration,
or retirement. See `SEEBX_PLATFORM_DATABASE_INVENTORY_V1.md`.
