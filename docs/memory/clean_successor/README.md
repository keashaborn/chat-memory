# Governed Memory clean successor

## Current authority

Phase 9J has one current governed-memory application path and one current,
inactive dormant-store installation package. These files define the current
version:

- `ops/governed_memory/installation/current/package_manifest.json`: exact
  76-artifact stores/controller package;
- `tools/governed_memory_install/package.py`: offline package verifier;
- `ops/governed_memory/runtime_manifest.json`: current application,
  installation-proof, and activation status;
- `ops/governed_memory/phase9j_controller_runtime_release_receipt.json`:
  exact published dormant Phase 9J controller substrate;
- `ops/governed_memory/current_component_disposition.json`: current versus
  quarantined component boundary.

Numbered historical packages are not current authority. Git history retains
deleted implementations; the working tree does not keep executable archive
copies of them.

## Phase 9J package

The inactive package contains the claim-bound install and empty-rollback
controllers, durable journals and receipts, exact resource ledger, fixed
PostgreSQL stage machine, concrete Psycopg adapter, fixed Docker/Qdrant/systemd
transports, administrative writer fence, controller supervisor, standalone
CPython inspector, offline runtime-input stager, immutable runtime publisher,
and two separate validation harnesses.

The guarded synthetic harness proves only the in-process controller model. The
authority-gated Linux harness is separately executable against fixed,
invocation-owned disposable PostgreSQL and Qdrant resources. Its repository
issuer is deliberately excluded from the sealed runtime. A root-owned,
create-once recovery capsule and inherited whole-proof lock allow a fresh
sealed runner process to resume without retaining an online signing key.

Install and rollback use distinct signed operations and distinct nonces.
Rollback eligibility is derived only after verifying the durable install
receipt and its exact fifteen-resource anchored ledger. The retained evidence
digest is bound into rollback execution and the create-once claim before any
rollback effect. Resume modes cannot create a missing claim. The reserved
start mode is the only first-claim path after the original authorization
window, and it must match the preclaimed recovery reservation.

The rollback sequence is controller marker, exact supervisor removal,
administrative writer fence, live semantic-empty recheck, store stop, exact
physical removal, and durable receipt publication. The fence covers the
authorized controller paths; it does not claim to exclude an equivalent
privileged root bypass.

## Current status

The Phase 9J repository package verifies offline and remains inactive. The
exact current Phase 9J controller runtime and release are published on seebx
as a dormant controller substrate; the promoted root-owned publication receipt is
`ops/governed_memory/phase9j_controller_runtime_release_receipt.json`. No
persistent PostgreSQL or Qdrant store was created, no production state changed,
and the external Phase 9J disposable Linux proof receipt is not yet present.
The package verifier itself never builds a runtime, runs Docker, reads secrets,
connects to a store, installs a service, or activates a route.

The current inactive store target is generation `000002`: PostgreSQL
`127.0.0.1:55433`, Qdrant `127.0.0.1:6344`, and collection
`governed_memory_9a54cf123493_000002`. Generation `000001` is retained only as
the labeled Phase 8G disposable application-validation snapshot and is not
current store or routing authority.

Phase 8G application/runtime and chat-deletion evidence remains separate. It
does not substitute for the Phase 9J installation/rollback proof. Phase 7C
receipts are historical under `ops/governed_memory/history/phase7c/` and are
not reusable as current evidence.

## Data boundary

PostgreSQL is the canonical governed-memory authority. Qdrant is a derived,
rebuildable projection. A future successor installation starts with fresh
empty stores and imports no old memory claims, vectors, jobs, review artifacts,
preferences, compatibility state, or unprocessed legacy data.

Chat erasure is limited to chat threads, transcripts, attachments, and their
conversational derivatives. It does not delete accounts or structured
LifeSwitch data, including libraries, food logs, workouts, weightlifting
sessions, measurements, plans, or people data.

## Legacy boundary

The remaining Memory v1/v5 implementation is quarantined: it is not accepted
by the current response graph and is outside the default current package. Its
physical deletion still requires exact unreachable-closure proof, live
quiescence evidence, rollback-retention completion, and a separately
authorized deletion batch.

Repository retirement is not live retirement. Legacy timers, services,
PostgreSQL objects, and Qdrant collections remain outside Phase 9J and must not
be inferred absent from repository cleanup.
