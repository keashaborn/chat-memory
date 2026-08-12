# Governed Memory Phase 8A promoted-proof validation contract

## Current result

Phase 8A disposable proof passed and its external metadata is promoted. The
proof wrapper is `ops/governed_memory/phase8a_disposable_proof_receipt.json`.
Its SHA-256 is
`7aaa5f1d0ec21c8a714d4d72280b838026cb0e2d1bfc011f9933a7e0757c7a71`.
It binds the exact pre-promotion commit
`fe1bd715ed9fcb86d5085ab80efc9782935c09a3`, tree
`5010e2664ac56b0558ce4c79fee9d62dead0d276`, parent
`5c5253099e0350d79e68c0d1c09c46c25f8b54b6`, and the unchanged 59-artifact
package manifest SHA-256
`5db42bb942b2a96d453cf3b7db28687f7c227c24f07098add73666302be8a22e`.
The package/runtime manifest still says proof pending because those bytes are
the immutable at-execution snapshot. There is no live execution surface and no
installation or activation authority.

The hermetic controller proof passed all 336 closed scenarios. Its retained raw
receipt SHA-256 is
`56a9d17616a459f969a0a1ab1f7794b353955708e68e4e5f31b5994e53faaecf`
and canonical JSON SHA-256 is
`65b1ffd6250bc7c3a7545c8ca5d731ac3b0d5e44b27b428d7feab3d3e7193b37`.
The isolated PostgreSQL 16.14 proof passed 21 positive and 5 refusal scenarios.
Its retained raw receipt SHA-256 is
`99d9a1571c17da198c2995d97f816cc30a695ab4a21695de5272e56beab0e242`
and canonical JSON SHA-256 is
`68cb15de75cd885e8b6b09b5106f1ff238e7d7517e1746d49c90547f5c0c0726`.
That receipt self-binds harness SHA-256
`3e1245ad420b053c2762abcb2fd8fb01c2f68db880f5a9610ddf7e5101d615bd`.
All live, source-database, Qdrant, and provider effect counters were zero;
protected synthetic account, chat, and LifeSwitch sentinels were unchanged;
and invocation-owned resources were cleaned. The pinned proof image was
retained.

## Retained Phase 7C result

Phase 7C disposable revalidation passed for the inactive installation
candidate. The proof attests the exact pre-promotion candidate commit
`5c9524b463c4760297dcebe489271af8d0b246a7`, tree
`be2dffb8aa929548462c5e1eafc684d05febb02e`. Metadata promotion does not
rewrite those attested identities.

Current bindings:

- source inventory:
  `610d07f6e65a4b9648b7887a47d040658b6e08f9b14f627fdb6957cab4d9a8cd`;
- runtime receipt:
  `210cd0fe1bdaf60089668b3d2c8d37be760ed9b867e0909d4e83ebcc204e84b2`;
- pre-promotion migration manifest:
  `3dc839db27f1b0d4ac68260fd7bff9f77321e6a10159a299c8335eb36086bcc4`;
- runtime lock:
  `94ca231656579ce3b8f09c308e34dc8a03b8d1cf445f7a3681193767cd7db365`;
- project wheel:
  `2cd060454039e8e16149cd670bf0f8dba70d6d93cc749c542fa5cc86b938c05e`;
- current proof artifact:
  `ops/governed_memory/phase7c_disposable_proof_receipt.json`, SHA-256
  `d4ef8b5b855a57e308f468f1db80feef9bab826840c014006ea68bcad8db80d0`;
- raw attempt-4 log SHA-256:
  `75fb33b38574f62a781151487cfce37cfa3c1e343ec2cca27403c583ce953136`;
- proof-execution runner SHA-256:
  `bd591188d0afaf4d7a65753e54648241fc1aa5ff3289f194d76d6aa07dfe449f`;
- current post-promotion runner SHA-256:
  `2acd2fb134d834b04f9b41448a2cfead7712ce846dab38b001c1a8647eb9796b`;
- terminal canonical receipt SHA-256:
  `43f12d40f2bc44cc4bdf4413aa1fec65cbcb06d0dd491f6f173914a055e9d28a`;
- HTTP receipt SHA-256:
  `297c6c9ae6c6ce164480772e8b87ea765cff89e7386f860c29950b72223e0079`;
- deletion receipt SHA-256:
  `c387e947ce8758e3d396fe95385a4e3e1651cb10b3a5220c329b2250d20cc637`;
  and
- resilience receipt SHA-256:
  `3614697a7b6394a595ddd922481aed763e21a0f08f70e96aad2df14e40557351`.

Fresh invocation-owned PostgreSQL 16.14 and Qdrant 1.19.0 resources exercised
forward/rollback/absence/reapply, normalized-catalog equivalence, forced RLS,
direct-DML denial, synthetic JWT/JWKS owner isolation, the owner HTTP
lifecycle, two-database worker composition, projection rebuild, correction,
retraction, hard deletion, exact chat-only coordinated deletion, paging, nine
crash boundaries, stale-lease refusal and final absence. Exact proof resources
were removed and the ports released.

The basic deletion receipt intentionally reports its paging, crash-injection,
and stale-lease-injection fields as false because those cases belong to the
separate resilience receipt. The resilience receipt proves 501-message paging,
all nine crash boundaries, stale-lease replacement/refusal, replay safety,
future-date refusal, attachment identity/movement refusal, and final absence.

The run made zero provider or production endpoint calls, read no production
data, published no container ports, used no persistent mounts, and left the
protected LifeSwitch snapshot and other-owner snapshots unchanged.

Phase 6E runtime and proof receipts are archived unchanged under
`ops/governed_memory/history/phase6e/`, with SHA-256 values
`cfe7a60c2e69de5a1603f86717f72d093f6fc2e623c2cb627008dbabb97c1c86`
and
`79ed0cf79299f856b8a0ab77d420386adf3705f286f6c78b21225cba8eed1097`.
They attest earlier bytes only and are not current Phase 7C evidence.

## Package evaluator boundary

`tools/governed_memory_install/inactive_installation.py` verifies exact package
hashes and evaluates a caller-supplied, content-free observation. It executes
no Docker, systemd, PostgreSQL, Qdrant, provider, installation, rollback, or
cleanup command.

CLI results are:

- `0`: package structure and hashes verified;
- `3`: an observation matched a closed structural profile, without authority;
  and
- `2`: refusal.

Every closed v2 decision receipt states zero evaluator mutations, zero provider
calls, and no state change. Its `phase8b_migration_execution_contract` permits
only canonical migrations 0001, 0003, and 0004 and excludes all source
PostgreSQL steps and migration 0002. Observation profiles describe externally
observed state, not work performed by the evaluator.

The Phase 8A controller runner is separately scoped to the synthetic backend.
Its closed matrix passed exactly 336 scenarios proving crash/resume,
compensation, rollback, tamper refusal, empty-only gates, sentinel preservation,
zero source PostgreSQL connections, catalog
reads, application-row reads, and writes at every observation stage, and zero
live effects. It covered every exact target, distinguished the active
stores-only supervisor from disabled and inactive HTTP/worker units, and proved
empty rollback retains every named root
and the nonce-bound quarantine path. The separate PostgreSQL 16 proof passed 21
positive and 5 refusal cases for the canonical cluster forward/rollback
templates. Even these passing results cannot authorize
Phase 8B installation.

The Phase 8B target, unit, source-I/O, and retained-root values recorded in the
proof-pending manifest are future acceptance requirements. They are not
current/live observations and do not claim that Phase 8B has executed.

Before Phase 8B can be considered, separate artifacts and proof are still
required for the trusted clock and atomic nonce claim, global execution lock,
external journal-seal anchor, exact live probes, same-filesystem quarantine
preflight, stores-only supervisor/boot recovery, encrypted backup/restore, and
the remaining installation surfaces. The external PostgreSQL 16 proof now
closes the canonical cluster rollback evidence gap recorded in the immutable
package snapshot. The Linux backend stays hard-disabled in Phase 8A.

PostgreSQL 16 `psql` ignores the numeric argument to `\quit 3`. The unchanged
package contains that ineffective failure exit at
`governed-memory-migrations/roles_preflight.pgsql:39` and
`governed-memory-migrations/0002_conversation_bridge/forward.pgsql:20`.
Remediation is deferred to Phase 8B and Phase 8C respectively, is not authorized
by this promotion, and must be followed by fresh Phase 7C failure-path
revalidation.

## Required inactive migration mode

The Phase 8B canonical executor must pass
`-v governed_memory_inactive_installation=on` to `roles_preflight.pgsql` and may
apply only migrations 0001, 0003, and 0004. Omission defaults to active-mode
LOGIN expectations and invalidates the inactive sequence. Phase 8B excludes
the source-cluster role bootstrap and
`0002_conversation_bridge/forward.pgsql`; both remain inside the separate Phase
8C authority boundary. Inactive canonical postflight requires API/worker
`NOLOGIN`, the exact canonical role identities, and no writer/requester
membership edges.

Rollback is empty-only and refuses if any role remains a direct member of
`memory_ingest_writer` or `memory_erasure_requester`.

## Required source logging state

Install-preflight observations and both source SQL defenses require:

- `log_statement=none`;
- `log_duration=off`;
- `log_min_duration_statement=-1`;
- `log_min_duration_sample=-1`;
- `log_transaction_sample_rate=0`;
- `log_parameter_max_length=0`;
- `log_parameter_max_length_on_error=0`;
- pgAudit absent from `shared_preload_libraries`; and
- auto-explain parameter logging absent or zero.

The 2026-08-11 snapshot observed unsafe
`log_parameter_max_length=-1`; therefore current installation observations must
refuse before source-role creation. Phase 8A authorizes no configuration change.

## Current route-test scope

The service-level POST test invokes the real resolver with synthetic RSA
JWT/JWKS input and a recorded live-authority verifier. A shared event log proves
resolver/authority verification precedes conversation-pool acquisition, local
requester role, role-context validation, owner/auth-context binding, and only
then begin/read deletion SQL. Wrong session user, current user, or requester
membership produces zero deletion SQL.

This is not a live Supabase user/session RPC test. Live credentials, session
freshness, route installation, firewall/TLS, visual QA and production routing
remain unverified.

## Residual validation boundary

The retained Phase 7C proof does not establish live Supabase session freshness,
persistent-store installation, production credentials or memberships, source
logging remediation, firewall/TLS transport, production routing, real provider or
embedding behavior, semantic calibration, frontend deployment, authenticated
visual QA, or activation. Those remain separate gates.

Any later change to runtime source, migration SQL, runtime lock, package lock,
or proof-execution behavior invalidates this current proof and requires a fresh
disposable run. The current runner was changed after attempt 4 only to expect
the promoted migration-manifest hash and `disposable_validated` label. The
release guard pins both runner hashes and classifies that exact two-line change
as metadata-only; the current runner is not described as executed by attempt 4.
Metadata and documentation changes that bind the retained Phase 7C receipt or
promote the external Phase 8A proof do not authorize installation or activation.
Phase 8B remains a separate audit/remediation and fresh-store authority boundary with
zero source PostgreSQL connections, reads, or writes at every stage and retained
named roots/quarantine after empty rollback; Phase 8C separately governs source
logging and inactive bridge preparation.
