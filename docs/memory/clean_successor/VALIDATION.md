# Governed Memory validation boundary

## Offline repository checks

Run on **seebx** from the isolated candidate:

```bash
PHASE9_VALIDATION_PYTHON=/tmp/governed-memory-successor-runtime-94ca231656579ce3b8f09c308e34dc8a03b8d1cf445f7a3681193767cd7db365-b52b753dc7974ee120e4abe264bb36f16b53341fa86b2ea8e0ab5bf86c735c20/bin/python
test "$(sha256sum "$PHASE9_VALIDATION_PYTHON" | cut -d ' ' -f 1)" = 1643dacd9feaedc58f3cc581e4d22577dfe25c09b10282936186ccf0f2e61118
"$PHASE9_VALIDATION_PYTHON" -I -B tools/governed_memory_install/package.py verify-package
"$PHASE9_VALIDATION_PYTHON" -I -B tools/governed_memory_validation/verify_store_migration_manifest.py
"$PHASE9_VALIDATION_PYTHON" -I -B tools/governed_memory_validation/verify_migration_manifest.py governed-memory-migrations
"$PHASE9_VALIDATION_PYTHON" -I -B tools/governed_memory_release/release_guard.py verify-artifacts
"$PHASE9_VALIDATION_PYTHON" -I -B tools/governed_memory_validation/run_memory_unit_tests.py
git diff --check
```

That exact dependency-complete Phase 8F application runtime is selected by
`runtime_manifest.json`; bare system or Local Mac `python3` is not a valid
substitute for this sequence.

The package verifier must report schema v6, Phase 9J inactive state, exactly 76
artifacts, no proof receipt inside the sealed package, no installation, no
secret access, no image staging, and no activation. The release guard separately
verifies the promoted current Phase 9J dormant-substrate receipt and must still refuse
production release with
`inactive_installation_package_not_authorized`.

These checks prove current bytes, contracts, source closure, deterministic
controller behavior, and fail-closed paths. They do not prove Docker,
PostgreSQL, Qdrant, systemd, process-crash recovery, or installation.

## Disposable Linux proof

The external Phase 9J proof is a separate, explicitly authorized operation on
**seebx**. The lease-guarded manager is the sole public entrypoint. Do not
invoke the permit publisher, staged-prefix disposition executor, substrate
bootstrap, proof issuer, or sealed runner directly. From the exact clean tagged
candidate, invoke the manager with the pinned Phase 9J interpreter and no arguments:

```bash
test -n "${CHAT_MEMORY_LEASE_ID:-}"
test -n "${CODEX_TASK_ID:-}"
test -n "${CODEX_THREAD_ID:-}"
sudo --preserve-env=CHAT_MEMORY_LEASE_ID,CODEX_TASK_ID,CODEX_THREAD_ID \
  /opt/governed-memory-controller/runtimes/0c19395ccfcab313c59792c81303f3d0257a78292dd0255596f203688985ff4d/bin/python \
  -I -B "$PWD/tools/governed_memory_validation/execute_phase9_disposable_live_proof_controller.py"
```

Those three values must identify the active production-write lease for this
exact candidate and task. Broad `sudo -E` is not permitted.

The manager validates the production change lease, publishes and rereads the
root-owned permit, requires the fixed tagged candidate and exact source blobs,
executes the corrected 000005 staged-prefix disposition, bootstraps only the
fixed 000005 empty substrate,
and launches the issuer for one disposable live proof. The permit authorizes
only that disposition-plus-proof chain; it does not authorize activation,
production-data access, provider calls, or any production, user, chat,
LifeSwitch, legacy, or non-owned deletion. Exact rollback removal of the
invocation-owned disposable proof resources is authorized and required.

The manager is the sole public Phase 9J entrypoint. Its child lineage, fixed
FD 198 control pipe, parent-death signal, and independent 30-second issuer
lease checks are cooperative controls for the shared UID 1000 environment. On
manager or lease loss, the issuer kills and reaps the effectful runner before
running only bounded `RECOVER_ONLY`; the manager must never kill that recovery
when its own wait bound expires. Control withdrawal sends `SIGCONT`, and the
issuer's Linux parent-death signal is also `SIGCONT`, so a stopped issuer wakes,
observes pipe/parent loss, and enters the same recovery path. The substrate
callable independently reproves the exact Phase 9J issuer/manager process lineage,
FD 198, active lease, and held fixed global lock immediately before mutation.
This is not a malicious same-UID confinement boundary and must not be reused
as Phase 10 activation authority.

The short permit/staged-prefix-disposition children may finish their
crash-consistent, create-once root metadata write if the manager dies during
that atomic step;
they cannot invoke the effectful runner. If the issuer or host cannot execute
recovery at all, the retained capsule is manual-retry evidence, not proof of
automatic host-death cleanup. If manager-side receipt verification disagrees
after an issuer exit of zero, the manager refuses the proof; it never treats
unverified output as success or silently launches a second effectful run.

The proof may use only pinned local PostgreSQL 16.14 and Qdrant 1.19.0 images,
fresh invocation-owned credentials, fixed loopback ports, synthetic data, and
fixed disposable resource names. It must perform install, real controller
process termination and resume, cold controller restart, reserved empty
rollback, rollback termination and resume, and exact final absence.

The proof must not read production data or credentials, call a provider,
connect to production endpoints, alter live services, import legacy memory, or
activate the successor. Its content-free receipt is external to the sealed
package and is promotable only when all terminal checks pass.

## Recovery and refusal

The recovery capsule is create-once root-owned state, not cleanup authority.
The runner first reconciles an exact prior capsule/publication state. After
the bounded preclaim and verify modes, a proof may perform one initial
start-or-recover and one recover-only launch. Only a verified pair-only,
no-install-effect result may retry the same already-claimed pair once; a failed
retry may be followed by one final recover-only launch. Therefore the maximum
effectful/recovery chain is two start-or-recover and two recover-only launches,
with no new claim minted by the retry. Missing claims, foreign resources,
identity drift, ambiguous publication prefixes, expired unreserved authority,
or incomplete terminal absence fail closed.

Never delete or normalize an ambiguous journal, receipt, claim, runtime
publication, capsule, or disposable resource by assumption. Diagnose it under
the same global lock and exact identities.

## Evidence classification

- Phase 8G: current application/runtime and chat-deletion disposable evidence;
  not installation or activation evidence.
- Phase 9J package verification: static repository evidence only.
- Phase 9J disposable Linux receipt: install/recovery/empty-rollback evidence
  for fixed disposable resources only; never production activation evidence.
- Production: remains unchanged and activation-blocked until Phase 10 receives
  separate authority and proves the full installed-to-consumed chain.

Chat-erasure validation remains chat-only. Accounts and structured LifeSwitch
records are outside the deletion graph.
