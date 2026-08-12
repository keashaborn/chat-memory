# Governed Memory validation boundary

## What validation proves

The current validators read repository files and exercise a sealed synthetic
model. They can prove exact package membership, hashes, dependency closure,
closed contracts, deterministic controller behavior, refusal paths, and absence
of forbidden predecessor imports from the current Phase 8B package.

They cannot prove that PostgreSQL, Qdrant, Docker, systemd, networking, secrets,
routes, or the application consumed the package. Source code, tests, manifests,
and synthetic receipts are not live runtime evidence.

## Canonical repository checks

Run on **seebx** from the isolated candidate worktree:

```bash
python3 -I -B tools/governed_memory_install/package.py verify-package
python3 -I -B tools/governed_memory_validation/verify_store_migration_manifest.py
python3 -I -B tools/governed_memory_validation/run_phase8b_disposable_proof.py
python3 -B -m unittest discover -s tests/memory -p 'test_*.py'
git diff --check
```

The first two commands are offline verifiers. The proof runner is restricted to
its newly created disposable directory and a sealed in-process backend. It must
refuse environment, process, network, and external-filesystem effects. It does
not run Docker or connect to a store.

## Closed current package

The checked-in manifest must be byte-identical to generator output. Every local
Python import and package initializer must be a manifest member. Extra files,
old version-suffixed module names, Phase 8A source, migration 0002, runtime
credentials, application services, and legacy proof payloads are forbidden from
the stores-only package.

The current canonical modules are unversioned filenames. Schema-version values
inside signed or hashed documents remain explicit protocol identities and are
not rewritten merely to make filenames shorter.

## Evidence classifications

- Phase 7C: retained application/chat-deletion disposable evidence; inactive.
- Current installation package: statically verified and synthetic-proof capable;
  Phase 8D performed no installation, and current live installation state was
  not reverified by this repository-only phase.
- Synthetic controller receipt: unit/model evidence only; not promoted as live
  installation proof.
- Production: no successor installation or activation evidence.

Any future live claim must separately bind source, installed bytes,
configuration, enablement, running process, invocation, and consumption.

## Retirement verification

Phase 8D requires all of the following:

1. no executable Phase 8A successor-install source or test remains in the active
   tree;
2. no current successor control, CI job, test, or document treats Phase 8A as
   authority;
3. current exact inventories name only canonical successor modules;
4. the release guard verifies the current package and store migration manifest;
5. deleted artifact hashes are retained only in the compact retirement ledger;
6. Git history, rather than an active-tree code archive, preserves old bytes;
7. focused and full tests pass without Docker or live-store access.

These checks do not assert retirement of the separate Memory v1/v5 repository
runtime, units, or tests. That surface remains an explicitly deferred scope.

## Safety boundary

Validation and repository cleanup authorize no installation, secret handling,
image operation, service change, database operation, Qdrant operation, provider
call, route change, or activation. Source preparation requires a separately
scoped authorization whose name does not reuse a numbered repository phase.
