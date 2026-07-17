# Memory V1 V5 production validation recovery — 2026-07-15

Target: **seebx backend** only.

The authorized installation run
`20260715T160437Z_12682238-1432-47a5-9aee-ee27d6861744` completed its backup and
all five schema/function migrations. It stopped during the first rollback-only
security suite with exit code 3.

Cause: the suite created `memory_v5_clone_writer`, which was valid when tested
immediately after the relational staging migration. The production installer
correctly installed the restricted writer migration before running all suites.
That migration narrows V5 RLS policies to `memory_v5_writer`, so the temporary
role was denied before synthetic staging rows could be tested.

Verified after the stop:

- The custom-format backup, restore catalog, and SHA-256 are present.
- Every preexisting user-table count equals the captured baseline.
- Every installed V5 data/staging table is empty.
- The synthetic transaction and temporary role were rolled back.
- `memory_v5_writer` remains `NOLOGIN NOINHERIT NOBYPASSRLS` and non-superuser.
- The 44-row predicate registry remains `proposed` and runtime-inactive.
- No extraction, durable apply, retrieval activation, worker, timer, or restart
  occurred.

The corrected final-state suite retains its temporary restricted role and grants,
then adds owner-scoped test-only policies inside the same transaction. The role,
policies, and synthetic rows all disappear at `ROLLBACK`. All four final-state
suites passed against a disposable PostgreSQL 16 production-schema clone.

The first authorized recovery then passed the first three production suites and
stopped in the fourth. That suite compared its one synthetic claim and revision
against global table counts; production already contained 25 claims and 45
revisions. The fourth transaction rolled back, and baseline/V5-empty checks
passed again. The production variant now scopes all apply-state counts and both
replay fingerprints to synthetic owner
`11111111-1111-4111-8111-111111111111`. All four suites passed again on the
disposable PostgreSQL 16 production-schema clone.

Recovery is controlled by:

- `ops/manifests/memory_v1_projection_v5_production_recovery_plan_20260715.json`
- `scripts/memory_v1_projection_v5_recovery.py`
- `tools/memory_v1_projection_v5_production_recovery.sh`

The committed recovery plan remains unauthorized. A separate mode-`0600`,
30-minute authorization must bind its canonical plan hash, exact Git commit,
seebx target, and the failed run ID. The recovery runner installs no migration;
it runs only the four hash-locked rollback suites, verifies the original backup
and baseline again, writes an audit report, and stops before live extraction or
durable application.
