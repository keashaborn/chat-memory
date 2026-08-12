# Governed Memory clean successor - Phase 8A promoted proof metadata

Phase 8A disposable proof passed and its external proof metadata is promoted.
The proved installation package remains byte-for-byte unchanged: all 59 package
artifacts and the package-embedded `proof-pending` fields are the frozen
at-execution snapshot, not the current external evidence state. The Linux
execution backend remains hard-disabled and exposes no live install, rollback,
activation, or cleanup command. Nothing installed, enabled, or started a
successor service, timer, listener, route, PostgreSQL database, Qdrant
collection, role membership, credential, or firewall rule. Production data was
not read or changed and no provider was called. The package, proof, authority
verifier, controller, synthetic backend, and observation evaluator do not
authorize installation, rollback, activation, or cleanup.

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

## Retained Phase 7C successor proof

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

## Phase 8A controller proof

The external wrapper is
`ops/governed_memory/phase8a_disposable_proof_receipt.json`, SHA-256
`7aaa5f1d0ec21c8a714d4d72280b838026cb0e2d1bfc011f9933a7e0757c7a71`.
It binds branch
`codex/governed-memory-phase8a-installer-controller-20260811`, commit
`fe1bd715ed9fcb86d5085ab80efc9782935c09a3`, tree
`5010e2664ac56b0558ce4c79fee9d62dead0d276`, parent
`5c5253099e0350d79e68c0d1c09c46c25f8b54b6`, and package manifest SHA-256
`5db42bb942b2a96d453cf3b7db28687f7c227c24f07098add73666302be8a22e`.

The hermetic controller receipt is retained under
`ops/governed_memory/history/phase8a/`, with raw SHA-256
`56a9d17616a459f969a0a1ab1f7794b353955708e68e4e5f31b5994e53faaecf`
and canonical JSON SHA-256
`65b1ffd6250bc7c3a7545c8ca5d731ac3b0d5e44b27b428d7feab3d3e7193b37`.
All 336 closed controller scenarios passed, the exact 59-artifact map matched,
all external and live effect counters were zero, the synthetic account, chat,
and LifeSwitch sentinels were unchanged, and its temporary root was removed.

The self-bound PostgreSQL 16.14 receipt is retained beside it, with raw SHA-256
`99d9a1571c17da198c2995d97f816cc30a695ab4a21695de5272e56beab0e242`
and canonical JSON SHA-256
`68cb15de75cd885e8b6b09b5106f1ff238e7d7517e1746d49c90547f5c0c0726`.
Its 21 positive and 5 refusal scenarios passed against the exact canonical
forward and rollback templates. The archived harness SHA-256 is
`3e1245ad420b053c2762abcb2fd8fb01c2f68db880f5a9610ddf7e5101d615bd`;
the receipt self-binds that harness. Invocation-owned container and network
resources were removed. The pinned PostgreSQL proof image was intentionally
retained and is not a successor persistent resource.

## Inactive installation package

The unchanged Phase 8A package adds an Ed25519 scope verifier, an append-only
intent-before-effect controller journal, a fail-closed state machine, a
synthetic disposable backend, a hard-disabled Linux backend, an exact ordered
install/rollback plan, and the hermetic controller proof runner. Its exact 336
closed synthetic scenarios are now attested by the retained receipt external to
the package. That receipt is not an external signature. The package-embedded
pending label is preserved because rewriting any package member after proof
would invalidate the attested artifact map.

The package also contains:

- pinned persistent PostgreSQL and Qdrant descriptors with exact resource
  identities and `restart: "no"`;
- a fresh successor cluster bootstrap with API and worker roles left `NOLOGIN`;
- a narrowly additive source-cluster role bootstrap that grants no membership,
  packaged only for the separately authorized Phase 8C boundary;
- a closed v2 structural decision receipt whose
  `phase8b_migration_execution_contract` permits only canonical migrations
  0001, 0003, and 0004, requires
  `-v governed_memory_inactive_installation=on` for canonical role preflight,
  and excludes every source-PostgreSQL step and migration 0002;
- dormant systemd templates; root-owned secret-file contracts; and
- a content-free observation evaluator that performs no commands or mutations.

The container bootstrap administrator remains a LOGIN role backed by the
root-only `bootstrap.env` file at mode `0600`. Phase 8A does not retire it.
Retirement requires a separate tested administrator/recovery login and a
separately approved operation. API and worker passwords and LOGIN capability
remain absent.

The persistent descriptors are intentionally not restart-supervised. The
package-embedded Phase 8B blocker
`canonical_cluster_rollback_not_disposable_postgresql_executed` is also a
frozen at-execution snapshot; the promoted external PostgreSQL 16 proof supplies
that evidence without rewriting the package. Phase 8B remains blocked on a
packaged stores-only supervisor and boot recovery,
encrypted PostgreSQL backup/restore, a persistent Qdrant digest, a trusted clock
with atomic one-use nonce claims, a canonical global execution lock, an external
journal-seal anchor, exact live probes, a same-filesystem quarantine preflight,
and remediation of the known PostgreSQL 16 failure-path issue described below.
The Linux execution backend remains hard-disabled. Disposable proof did not
create or authorize the persistent package targets.

PostgreSQL 16 `psql` ignores the numeric argument in `\quit 3`. The frozen
package therefore has ineffective invalid-mode failure exits at
`governed-memory-migrations/roles_preflight.pgsql:39` and
`governed-memory-migrations/0002_conversation_bridge/forward.pgsql:20`.
The first is a Phase 8B remediation; the second remains in the separate Phase
8C source-integration boundary. Neither file is changed by this promotion.
Both fixes require fresh Phase 7C failure-path revalidation before their bytes
can be accepted.

Phase 8B, if separately authorized later, is limited to fresh dormant
successor stores. Every Phase 8B observation stage requires zero source
PostgreSQL connections, catalog reads, application-row reads, and writes, and
must cover every exact target. These are future acceptance requirements, not
current or live observations. Install postflight must keep the HTTP and worker
application units disabled and inactive while the separately classified store
supervisor may be enabled and active for the stores only. Empty rollback
must remove the exact candidate resources but retain `install_root`,
`environment_root`, `runtime_environment_root`, `state_root`, `backup_root`,
and the exact nonce-bound legacy-secret quarantine path. Phase 8C is the
separate authority boundary for source logging remediation, inactive source
roles, and the inactive conversation bridge. Runtime credentials, memberships,
routes, services, provider validation, and pilot activation remain later gates.

## Source logging refusal

Every source preflight requires all statement, duration, sampled-duration, and
transaction-sampling logging disabled; `log_parameter_max_length=0`;
`log_parameter_max_length_on_error=0`; pgAudit absent; and auto-explain
parameter logging disabled. The 2026-08-11 read-only snapshot observed
`log_duration=off` but `log_parameter_max_length=-1`. PostgreSQL treats `-1` as
full bind-value logging, so the current source configuration is unsafe and the
role bootstrap must refuse before its first write. Phase 8A does not change the
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

Production remains unchanged. Legacy components are not deleted by Phase 8A.
Retirement requires an active successor, exclusive routing proof, a rollback
window, exact target receipts, and separate deletion authorization.
