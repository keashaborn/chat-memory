# Legacy-memory retirement security boundary v1

This directory is a candidate-only, fail-closed provisioning contract. It has
not created an AWS secret or IAM role, changed PostgreSQL, installed a DSN,
restarted a service, run reconciliation, or retired a schema.

## Identity split

- `brains_app` is the normal application identity. It must remain unable to
  inspect or mutate the private legacy queues.
- `lifeswitch_retirement_auditor` is a temporary, `NOINHERIT`, read-only login.
  It receives no evidence-table or queue grants. Its only special authority is
  `EXECUTE` on `memory.legacy_retirement_evidence_v1()`, which returns six
  integer counts and no identifiers, hashes, timestamps, or chat content.
- `sage` remains the database owner/administrator and is accepted only by
  separately approved migration, rollback, and retirement paths.

## AWS key-custody split

- The existing SeeBx instance role is exactly
  `arn:aws:iam::339712834334:role/SeeBxSessionManagerRole`.
- It may assume only
  `arn:aws:iam::339712834334:role/LifeSwitchLegacyAttestationRecoveryV1`, with
  the bound external ID in both identity and trust policies.
- The recovery role may only describe/read/list versions of the exact
  `lifeswitch/seebx/legacy-attestation-quarantine-v1` secret. It cannot create,
  update, rotate, tag, delete, restore, or change that secret's policy.
- The secret value is a versioned JSON object conforming to
  `secret-value.schema.json`. The 32-byte AES key and database password must
  never appear in Git, shell arguments, logs, receipts, or assistant output.

This is least-privilege separation for normal operations. It is not a boundary
against root or arbitrary-code compromise of the SeeBx instance, because the
instance role is the sole trusted principal allowed to assume the recovery
role.

## Separately authorized production stages

1. Validate all IAM policies with AWS Access Analyzer and validate the secret
   resource policy as non-public. Do not create resources if either validator
   reports an error or security warning.
2. Create the database auditor and count function with `provision.pgsql`, using
   a generated password supplied only through the protected environment. Store
   its DSN in a root-owned mode-0600 environment file. Do not restart services.
3. Create the recovery role and exact policies, generate the AES-256 key in a
   protected process, create the secret without placing the value on a command
   line, and bind the resource policy.
4. Retrieve one exact immutable `VersionId` through the assumed recovery role,
   validate the secret schema, verify only the key fingerprint, and create the
   mode-0600 custody receipt. Never emit the key.
5. Run the read-only preflight. Reconciliation, schema retirement, auditor
   deprovisioning, secret retention/deletion, deployment, restart, and cutover
   each remain separate authorization gates.

`rollback.pgsql` removes the auditor/function only before retirement and refuses
active sessions or drift. `deprovision_after_retirement.pgsql` removes the
temporary auditor only after both legacy schemas are absent. Neither script
terminates sessions or uses `CASCADE`.
