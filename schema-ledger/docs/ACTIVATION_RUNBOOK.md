# Schema Ledger V1 activation and rollback

Activation is a source-control/CI adoption only. It must not apply, rewrite, reverse, or mark any database migration; change PostgreSQL or Qdrant; restart a service; deploy; or push.

## Approval-gated activation

1. **seebx/backend — read-only:** use the absolute lease `discover` interface with the activating task/thread and `/opt/chat-memory`. Require the expected clean production commit/tree, zero active conflicting leases, unchanged 304-ref and 216-worktree identities, zero Git locks/processes, and the expected read-only catalog/source/Qdrant fingerprints.
2. **seebx/backend — mutation lease required:** acquire one exact fresh lease for the authority worktree and only the candidate paths listed in `MANIFEST.sha256`. The starting HEAD must equal the approved activation baseline. Do not reuse the Step 2 lease.
3. **seebx/backend — staged source only:** copy the candidate to a private staging child, verify `MANIFEST.sha256`, modes, regular-file identities, absence of symlinks, and no unexpected paths. Use no-clobber installation into the new `/opt/chat-memory/schema-ledger/` subtree. If that subtree exists, stop. Require `.github/workflows/auditability.yml` to remain Git blob `786f5a962451b398eb2b5efe3252ed74c8193377`, SHA-256 `2cd7164cfdb88eee09718faebf65c12f67b54ffa480c185d989e2af5d3be7de0` before applying the exact canonical adoption contract in `schema-ledger/ci/auditability-schema-ledger-v1.json`.
4. **seebx/backend — read-only validation:** run Python with `-B` and `PYTHONDONTWRITEBYTECODE=1`. Validate canonical evidence and ledger; compare all tracked SQL sources against the current Git tree; rerun the synthetic/adversarial suite. This validator does not need a database mutation and does not read row data.
5. **seebx/backend — commit lease:** confirm the worktree diff contains only the manifest-listed `/opt/chat-memory/schema-ledger/` files plus the one reviewed CI workflow edit. Commit exactly those paths. Do not push or deploy. The commit changes intended-history governance and CI validation only.
6. **seebx/backend — fresh read-only evidence:** release the starting-HEAD lease after the commit. A fresh lease is required for any further commit, handoff, push, deploy, or schema action. Re-run the PostgreSQL catalog validator in a read-only transaction and require the live database/Qdrant fingerprints to remain unchanged.
7. **CI adoption:** the existing `Auditability Guard (seebx)` workflow runs the three exact commands in `ci/auditability-schema-ledger-v1.json` immediately after checkout, before building the CI stack. It verifies the adversarial suite, exact candidate manifest, and every SQL source in the checked-out Git tree. CI must fail on altered/extra/missing SQL, noncanonical ledger/evidence, unsafe-source reclassification, or an unrecorded migration. Production catalog refresh remains a separate read-only job with controlled server access; CI must never receive production credentials or row data.

The activation review must name the exact candidate manifest hash, starting commit/tree, ending local commit, lease ID, test count, database catalog aggregate fingerprint, and rollback commit. It must also state that no migration or service/data action occurred.

## Scoped rollback

Before a commit, remove only newly installed files whose identity and SHA-256 match the staged manifest, then remove only empty directories created by that activation. Do not remove or overwrite any preexisting or identity-changed path.

After a local commit, rollback is a new leased Git commit that removes exactly the ledger subtree added by the activation and restores the exact CI workflow preimage. Do not rewrite history. The ledger has no runtime service/config effect, so no service restart or database rollback is needed. Retain the activation and rollback audit events.

If any audit event/evidence file was already committed, preserve it as history; never silently replace evidence. If production HEAD, catalog fingerprint, Qdrant configuration, file identity, or lease scope drifts during activation, stop and leave production schema/data unchanged for review.

## Remaining enforcement limits

This candidate detects schema/source drift and defines intended-history governance. It does not make legacy SQL utilities safe, prove which historical script created a live object, prevent a privileged operator from bypassing CI, or automatically enforce database migration execution. A later separately approved change may add a dedicated append-only migration execution ledger and controlled executor. That is not part of Step 3 activation.
