# SeeBx LifeSwitch clean database baseline v1

Date: 2026-08-22

Status: candidate generator; no production schema, data, role, service,
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

## Next gate

Build the package from the exact candidate commit containing this generator,
transfer only schema artifacts to Work Runner, restore into a fresh digest-pinned
PostgreSQL 16 container, load synthetic exercise-muscle fixtures, run the real
candidate adapter and versioned contract, prove expected objects and grants,
and destroy the complete disposable environment.
