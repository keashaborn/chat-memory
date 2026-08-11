# Governed Memory clean successor - Phase 7C disposable-validated inactive package

Phase 7C is a disposable-revalidated, inactive installation candidate. It has not installed,
enabled, or started a successor service, timer, listener, route, PostgreSQL
database, Qdrant collection, role membership, credential, or firewall rule. It
has not changed production data or called a provider. The package and its
offline evaluator do not authorize installation, rollback, activation, or
cleanup.

PostgreSQL remains the canonical Memory store. Qdrant is derived and
rebuildable. Both successor stores must start fresh and isolated; no legacy
database, row, collection, vector, snapshot, volume, preference, review,
compatibility state, attachment content, or unprocessed chat data may seed the
successor. Supabase remains account authority and accounts are not copied into
Memory.

## Current sealed runtime

The current CPython 3.12.3 Linux runtime is bound to source inventory
`610d07f6e65a4b9648b7887a47d040658b6e08f9b14f627fdb6957cab4d9a8cd`.
The content-free runtime-build receipt is
`ops/governed_memory/runtime_build_receipt.json`, SHA-256
`210cd0fe1bdaf60089668b3d2c8d37be760ed9b867e0909d4e83ebcc204e84b2`.
The offline build recorded zero network and provider calls, zero persistent
resources, and zero production-state changes. Build reproducibility alone is
not database/Qdrant proof; the separate Phase 7C receipt closes that boundary.

## Current disposable proof

Attempt 4 passed against pre-promotion candidate commit
`5c9524b463c4760297dcebe489271af8d0b246a7`, tree
`be2dffb8aa929548462c5e1eafc684d05febb02e`. The current structured proof is
`ops/governed_memory/phase7c_disposable_proof_receipt.json`, SHA-256
`d4ef8b5b855a57e308f468f1db80feef9bab826840c014006ea68bcad8db80d0`.
It binds raw-log SHA-256
`75fb33b38574f62a781151487cfce37cfa3c1e343ec2cca27403c583ce953136`,
source inventory
`610d07f6e65a4b9648b7887a47d040658b6e08f9b14f627fdb6957cab4d9a8cd`,
runtime-build receipt
`210cd0fe1bdaf60089668b3d2c8d37be760ed9b867e0909d4e83ebcc204e84b2`,
and pre-promotion migration manifest
`3dc839db27f1b0d4ac68260fd7bff9f77321e6a10159a299c8335eb36086bcc4`.

The proof created fresh invocation-owned PostgreSQL 16.14 and Qdrant 1.19.0
resources, exercised apply/rollback/absence/reapply, the owner HTTP lifecycle,
two-database worker composition, Qdrant projection/rebuild, correction,
retraction, exact chat deletion, paging and crash recovery, then removed every
exact proof resource and released its ports. It made zero provider or
production endpoint calls, read no production data, published no container
ports, and created no persistent mounts. The protected LifeSwitch snapshot was
unchanged. Phase 6E receipts remain under
`ops/governed_memory/history/phase6e/` as historical, noncurrent evidence.

Attempt 4 executed runner SHA-256
`bd591188d0afaf4d7a65753e54648241fc1aa5ff3289f194d76d6aa07dfe449f`.
The current sealed runner SHA-256 is
`2acd2fb134d834b04f9b41448a2cfead7712ce846dab38b001c1a8647eb9796b`;
it differs only in the expected promoted manifest hash and validation-state
label. That post-proof metadata rebind was not executed by attempt 4 and does
not claim a second disposable run.

Terminal proposal replay is bounded to the 30 days before proposal retention
purge. After purge, exact replay is unavailable and the API returns
`proposal_retention_purged` from content-free audit evidence; deleted proposal
content is never reconstructed.

## Inactive installation package

The package contains:

- pinned persistent PostgreSQL and Qdrant descriptors with exact resource
  identities and `restart: "no"`;
- a fresh successor cluster bootstrap with API and worker roles left `NOLOGIN`;
- a narrowly additive source-cluster role bootstrap that grants no membership;
- verified migrations whose inactive execution must explicitly pass
  `-v governed_memory_inactive_installation=on` to both
  `roles_preflight.pgsql` and the conversation-bridge forward migration;
- dormant systemd templates; root-owned secret-file contracts; and
- a content-free observation evaluator that performs no commands or mutations.

The container bootstrap administrator remains a LOGIN role backed by the
root-only `bootstrap.env` file at mode `0600`. Phase 7C does not retire it.
Retirement requires a separate tested administrator/recovery login and a
separately approved operation. API and worker passwords and LOGIN capability
remain absent.

The persistent descriptors are intentionally not restart-supervised. Boot
recovery, encrypted PostgreSQL backup/restore, and persistent Qdrant digest
authorization remain blockers. Disposable proof did not create or authorize
the persistent package targets.

## Source logging refusal

Every source preflight requires all statement, duration, sampled-duration, and
transaction-sampling logging disabled; `log_parameter_max_length=0`;
`log_parameter_max_length_on_error=0`; pgAudit absent; and auto-explain
parameter logging disabled. The 2026-08-11 read-only snapshot observed
`log_duration=off` but `log_parameter_max_length=-1`. PostgreSQL treats `-1` as
full bind-value logging, so the current source configuration is unsafe and the
role bootstrap must refuse before its first write. Phase 7C does not change the
live setting. Remediation requires separate authorization and a new receipt.

## Bridge catalog seal

HTTP startup requires
`GOVERNED_MEMORY_CONVERSATION_BRIDGE_CATALOG_SHA256`. The digest covers the
complete selected database, role and membership graph; private bridge routines;
all target relations, columns, constraints, indexes, policies, triggers, rules,
inheritance and schema ACLs; every noninternal target-trigger routine; and
non-pg_catalog routines referenced by target RLS policies.

The inactive install-postflight digest is not an activation digest. LOGIN and
membership activation change the catalog. After separately authorized
credentials and exact memberships are applied, the full catalog must be
recomputed and sealed before either runtime starts.

The deletion-route service test uses the real Supabase JWT/JWKS resolver with a
synthetic RSA token and a recorded authority verifier. It proves pool, role,
owner-context, and deletion-SQL order and proves wrong session/current role or
membership causes zero deletion SQL. It is not live Supabase verification.

## Chat-only deletion boundary

The only accepted selectors are `thread`, `message_tail`, `recent`, and
`all_conversations`. A request may remove exact selected chat rows, matching
chat attachments, matching bridge rows, eligible empty chat threads, and
successor Memory derived from those exact targets. It may retain content-free
consent, security, audit and final-absence receipts.

It cannot delete accounts or structured LifeSwitch data, including libraries,
workouts, weightlifting sessions, food logs, measurements, plans, or tracking
records. It has no arbitrary Memory-only or account-wide purge.

Legacy project/thread restrictions, weak trusted-web lineage, and legacy
owner/thread gaps remain fail-closed blockers. The 2026-08-11 read-only,
drift-prone snapshot recorded 1,848 chat rows, 117 threads, zero attachments,
eight active-thread selections, eight trusted-web transcripts, 710 rows with a
null thread ID, and 118 rows with a null owner. `brains.service` remained
active, so refresh these counts before any activation decision.

## Frontend and production boundary

Frontend candidate `71377a838058d75320b55817fc8c9656d404f955`, tree
`7d29afff30676eccc77465e5f144ec9129aaae4d`, build
`-zedD-GFt2yko7J17swb6`, remains undeployed and visually unverified. Successor
`operation_id` and confirmation binding semantics remain unresolved.

Production remains unchanged. Legacy components are not deleted by Phase 7C.
Retirement requires an active successor, exclusive routing proof, a rollback
window, exact target receipts, and separate deletion authorization.
