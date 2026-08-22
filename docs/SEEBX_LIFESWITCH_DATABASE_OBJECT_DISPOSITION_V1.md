# SeeBx LifeSwitch database object disposition v1

Date: 2026-08-21

Status: candidate baseline-ready disposition; no production migration, database write, object
retirement, deployment, restart, or deletion authority

## Purpose

The consumer audit classifies current database use. This disposition manifest
turns that evidence into explicit, machine-readable clean-baseline decisions
without treating absence of a consumer as permission to delete anything.

The builder fails closed when the source report hash changes, operational
references are allowed to seed retention, an object is missing or duplicated,
or any unresolved identity lacks an exact disposition. The tracked manifest is
content-free and contains database object identities and classifications only.

## Bound evidence

- source consumer report SHA-256:
  `085fe940aa18c3e15c04685d281f84416303aa88a022d8fbc627fc68cd30d066`;
- source database manifest SHA-256:
  `fd71e52ac0825661df33f36c9bff1b595e0cf333221b8bd0ba090ad6dac4ad67`;
- source object catalog SHA-256:
  `e55ee06cd5733be20175450857b7c8b58eb2c974fcd8873350c3f467df22426b`;
- generated disposition manifest SHA-256:
  `ff8a8dc0ef313dc9717744c5610258e7e440bbc6c8479b86abc60fc85887524a`;
- 218 objects accounted for exactly once;
- five focused disposition tests pass.

## Disposition result

| Disposition | Count | Clean-baseline action |
|---|---:|---|
| retained active | 57 | include active application surface |
| retained dependency | 32 | include database dependency reachable from active code |
| retained extension | 118 | recreate the owning extension, not copied object-level DDL |
| retained canonical product data | 3 | include the approved normalized muscle identity, alias, and relationship model |
| recovery only | 2 | exclude from runtime baseline only after encrypted recovery evidence is bound |
| archive candidate | 6 | exclude only after exact archive and restoration proof |

`baseline_generation_allowed` is therefore `true`. The approved normalized
muscle model has 1,058 weighted mappings across 154 exercises. The
`catalog_dev.muscle`, `muscle_alias`, and `exercise_muscle` relations are
retained as canonical product data. This decision authorizes candidate baseline
generation and capability development only; it does not activate a route or
change production.

The empty, unconsumed `catalog_dev.exercise_muscle_rule` relation is the sixth
archive candidate. It is not part of the canonical model and still requires an
exact archive and restore proof before any production retirement.

The dormant internal food-search subtree is an archive candidate. The active
`catalog_dev.food` relation is retained because current application code still
uses it. USDA remains the active external food provider path.

The two `lifeswitch_snapshot` relations are recovery-only. They are not runtime
application surfaces. Application access is denied, and exclusion from a clean
runtime baseline requires an encrypted archive/count/hash receipt first.

## Authority boundary

The manifest sets both `deletion_authority` and `production_change_authority`
to `false`. `archive_candidate` and `recovery_only` are review
states, not execution instructions. Nothing in this batch changes production
schema, data, roles, services, environment, routing, or deployment state.

## Next gate

1. Define one versioned normalized exercise-muscle capability and PostgreSQL
   adapter, retaining the arrays only as temporary compatibility projections.
2. Produce encrypted, restore-tested receipts for the six archive candidates
   and two recovery-only snapshot relations.
3. Generate the canonical clean-install schema/role baseline from this exact
   manifest.
4. Install it only into a fresh disposable Work Runner database and run owner,
   RLS, migration, application-contract, and teardown tests.
