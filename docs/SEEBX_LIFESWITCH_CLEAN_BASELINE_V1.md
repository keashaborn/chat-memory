# SeeBx LifeSwitch clean database baseline v1

Date: 2026-08-22

Status: candidate baseline and disposable restore verified; no production schema, data, role, service,
deployment, retirement, or deletion authority

## Purpose

`build_lifeswitch_clean_baseline_v1.py` converts the exact, hash-bound
218-object disposition into a schema-only clean-install package. It reads the
source schema with `pg_dump`; it does not write to the source database and does
not include table data.

The generator fails closed unless:

- the disposition is `candidate_baseline_ready`;
- baseline generation is explicitly allowed;
- deletion and production-change authority remain false;
- all seven excluded relations and the one excluded function match the exact
  reviewed set;
- the three normalized muscle relations are retained as canonical product data;
- the archive contains no table-data entries;
- the rendered SQL contains no data statements or excluded identities.

## Package

The output directory contains:

- a schema-only custom PostgreSQL archive;
- a deterministic filtered restore list;
- rendered clean schema SQL;
- a password-free, fail-closed role bootstrap;
- a psql install entry point; and
- a content-free hash and authority receipt.

Owners and ACLs are preserved. All baseline role identities are created
`NOLOGIN`; credential provisioning and login activation remain separate
environment responsibilities. The isolated bootstrap owner is `NOLOGIN` and
exists only to reproduce extension ownership and schema authority.

## Exact exclusions

- dormant catalog relations: `exercise_muscle_rule`, `food_alias`,
  `food_nutrient`, `food_portion`, and `nutrient`;
- retired catalog function: `search_foods`;
- recovery-only schema: `lifeswitch_snapshot`.

These are baseline exclusions, not production-removal instructions. Archive
and restore evidence is still required before any production retirement.

## Verified Work Runner proof

Candidate commit `37770d0b32f180a7ffd4cd8613a0cd426681283d` produced a
schema-only package bound to disposition SHA-256
`ff8a8dc0ef313dc9717744c5610258e7e440bbc6c8479b86abc60fc85887524a`.
The package restored successfully into the digest-pinned PostgreSQL 16 image
on rootless Work Runner with no network or published port. The proof found all
seven baseline roles as `NOLOGIN`, all three canonical muscle tables, and zero
excluded relations or functions.

The exact candidate PostgreSQL adapter and both versioned muscle contracts ran
under `lifeswitch_app_login` against synthetic rows. Alias `chest` resolved to
canonical `pectoralis_major`; normalized mappings were authoritative and the
deliberately stale legacy arrays were non-authoritative. The complete 1,385-test
backend suite passed. The container, data volume, socket volume, and disposable
workspace were then proved absent.

Machine-readable evidence is
`ops/database/lifeswitch_clean_baseline_verification_v1.json`.

## Next gate

Use this baseline for the real signed-session paired frontend/backend proof,
including Plan-to-Nutrition behavior and owner/RLS enforcement. Production
installation, migration, retirement, or cutover remains separately authorized.
