# Memory V1 V5 activation readiness

Status: read-only v3 current-invariant gate. Admin-only V5 shadow retrieval is
live with zero prompt or answer influence.

Server boundary: seebx backend. The gate does not run on Verbal Sage, RESSE, or
Resse-Train. It makes no database or Qdrant writes and no model calls.

## Independent boundaries

1. `database_boundary`: every owner-bearing `memory.*` table forces RLS; the
   eight governed durable tables expose no direct mutation privilege to
   `brains_app`; controlled functions and restricted non-login roles remain in
   place; sanitized trace tables are forced-RLS and append-only; and the V5
   predicate registry remains proposed/inactive.
2. `automation_current`: the production checkout is clean, all four downstream
   planners enforce the current deterministic compiler hash, the exact timer
   contract is present, the private GPU tunnel is active, and systemd has no
   failed units.
3. `projection_consistent`: every supported Postgres claim has exactly one
   owner-scoped Qdrant projection, every Qdrant point maps back to that same
   Postgres claim and owner, and no point lacks its owner.
4. `allowlisted_zero_influence_shadow_observed`: shadow retrieval and sanitized
   trace persistence are enabled only for an explicit UUID allowlist; every
   allowlisted owner has claim traces; project trace owners are allowlisted;
   and every persisted trace asserts zero database writes, Qdrant writes,
   retrieval activation, prompt injection, and answer-model exposure.
5. `prompt_influence` and `general_account_activation` remain false. They are
   separate later decisions and are not implied by a passing shadow gate.

Current counts are diagnostics, not frozen expectations. New evidence,
entities, assessments, claims, and traces are legitimate append-only growth.
The v2 exact-row baseline incorrectly treated governed growth as drift and is
no longer used for activation readiness.

The report contains hashes, counts, role/function names, boolean outcomes, and
timer state. It removes raw supported-claim identifiers and hashes owner UUIDs
in trace distributions. It never stores query text, claim prose, evidence
prose, prompt content, or answer content. The report is written mode `0600`.

## Required sequence

1. Preserve a passing v3 report and checksum for the current admin-only shadow.
2. Add only the two owner-controlled test accounts to the zero-influence shadow
   allowlist; do not enable all authenticated accounts.
3. Gather ordinary-use traces for those accounts and rerun the v3 gate until
   all allowlisted owners have isolated traces.
4. Evaluate selection quality, suppression behavior, missing-memory rate, and
   false-positive rate from sanitized trace outcomes plus targeted owner-only
   review.
5. Expand zero-influence shadow coverage to the remaining real accounts only
   after the multi-owner canary passes.
6. Design a separately bounded prompt-influence canary with strict retrieval
   budgets, Postgres revalidation, rollback controls, and legacy fallback
   isolation.
7. Retire remaining legacy prompt contributors only after the new path passes
   live answer-quality and account-isolation checks.
