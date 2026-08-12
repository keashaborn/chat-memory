# Governed Memory activation boundary

## Current decision

Do not install or activate the successor. Phase 8F produces a cleaner inactive
repository candidate; it does not produce a functioning memory service.

The package still lacks a complete claim-bound live installation composition,
live rollback executor, live activation executor, promoted live proof, and
several durability and resource-ownership guarantees. The authoritative exact
blocker list is in
`ops/governed_memory/runtime_manifest.json` and is synchronized with the
bootstrap, pilot, and schema contracts.

The current source is intentionally not bound to the historical Phase 7C build
receipt. A new runtime build and authorized disposable revalidation are required
before any installation decision.

## Before a dormant stores-only installation

A later approval must bind one reviewed commit and tree, the exact package and
contracts, controller runtime receipt, trusted authority substrate, local image
identities and staging receipt, exact targets, and a fresh read-only preflight.
It must also provide fresh store credentials without reading or reusing legacy
or provider credentials.

The live composition must close these implementation gaps before authorization:

- opaque verified-package capability at the claim boundary;
- atomic attempt-bound resource ownership between pre-probe and apply;
- complete command, secret, migration, readiness, and store-effect adapters;
- terminal-seal and composite-step crash recovery;
- cross-process durable file identity or an equivalent seal;
- lock-bound and durably anchored resource-identity ledger;
- complete image-baseline, hardening, and external-readiness checks.

After implementation, a separately authorized disposable live run must prove
process-crash recovery, cold restart, empty rollback, exact cleanup, and absence
of effects outside its invocation-owned resources. Synthetic proof cannot
substitute for this.

## Before application activation

Dormant store installation and application activation are different approvals.
Activation additionally requires application runtime installation, fresh runtime
credentials, Supabase session authority, owner isolation, chat capture/deletion
route verification, provider and embedding validation, semantic calibration,
projection reconciliation, authenticated frontend verification, and an
authorized pilot owner/scope.

Legacy memory readers, writers, timers, cron, provider surfaces, routers, and
admin paths must be proven quiescent for the required observation window before
exclusive routing. `brains.service` must remain available for chat and
LifeSwitch; whole-service shutdown is not the exclusivity mechanism.

## Data guarantees

The successor starts with fresh empty PostgreSQL and Qdrant stores. PostgreSQL
is canonical; Qdrant is derived. No legacy memory data, vectors, preferences,
jobs, review artifacts, or unprocessed data is imported.

Only chat-owned conversations, transcripts, attachments, and conversational
memory participate in chat deletion. Account identities and structured
LifeSwitch records remain outside that deletion graph.

## Separate source preparation

Source logging remediation, inactive source roles, and conversation-bridge
preparation require a separate source-preparation authorization. Numbered phase
labels are not authority. A stores-only signature cannot authorize source
PostgreSQL access, migration 0002, memberships, routes, runtime credentials, or
activation.

There is currently no packaged replacement for the retired Phase 8A source-role
bootstrap template or service-account contract. Both must be rebuilt, reviewed,
and bound under their appropriate future authority before source preparation or
application service installation.
