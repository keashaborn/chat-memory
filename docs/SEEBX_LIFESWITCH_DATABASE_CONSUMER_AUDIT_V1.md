# SeeBx LifeSwitch database consumer audit v1

Date: 2026-08-21

Status: candidate evidence; no production migration, database write, object
retirement, deployment, restart, or deletion authority

## Purpose

The disposable restore verifier proves that the current isolated LifeSwitch
database can be backed up, restored, and compared exactly. It does not prove
that every restored relation and function is still required. This audit adds
that missing consumer map before a clean-install baseline is designed.

`scripts/audit_lifeswitch_database_consumers_v1.py` runs database catalog
queries inside a repeatable-read, read-only transaction. It inventories
relations and functions in the seven retained schemas, scans only controlled
candidate source roots, maps database-internal dependencies, and propagates
retention only from verified consumers. It never reads or emits application row
values. It writes a mode-0600, content-free report and explicitly grants no
deletion authority.

## Evidence boundary

- isolated database manifest SHA-256:
  `fd71e52ac0825661df33f36c9bff1b595e0cf333221b8bd0ba090ad6dac4ad67`;
- committed auditor candidate:
  `3b349034d67dd37f783305136bedd2309e095e61`;
- database object catalog SHA-256:
  `e55ee06cd5733be20175450857b7c8b58eb2c974fcd8873350c3f467df22426b`;
- 218 relations/functions inventoried;
- 211 dependency edges;
- runtime scan: 185 files;
- operational scan: 25 files;
- migration scan: 113 files;
- nine focused auditor tests and eleven disposable-restore contract tests pass.

The committed rerun produced:

- report:
  `/var/backups/seebx-cleanup/lifeswitch-database-consumers-v1/20260822t033523z/consumer-audit.json`;
- report SHA-256:
  `9e5e5da28eebb01985323696aa5f629368811b8cb6de82f284f9890f4a34369e`.

Earlier development receipts are superseded because their source trees included
an uncommitted auditor.

## Classification result

| Classification | Count | Meaning |
|---|---:|---|
| application direct | 57 | exact or schema-bound application reference |
| database internal reachable | 28 | dependency reachable from a verified consumer |
| operational direct | 5 | retained operational verifier/worker reference |
| extension owned | 118 | installed PostgreSQL extension object |
| migration only | 5 | found only in historical/installation SQL |
| unproven | 5 | no verified runtime, operational, or reachable database consumer |

The ten unresolved objects are:

| Classification | Object | Evidence disposition |
|---|---|---|
| unproven | `catalog_dev.exercise_muscle` | 1,058 rows; normalized muscle mapping has no current consumer |
| unproven | `catalog_dev.exercise_muscle_rule` | zero rows; no current consumer |
| unproven | `catalog_dev.muscle` | 83 rows; no current consumer |
| unproven | `catalog_dev.muscle_alias` | 20 rows; no current consumer |
| migration only | `catalog_dev.food_alias` | five rows; reachable only from dormant `search_foods` |
| migration only | `catalog_dev.food_nutrient` | zero rows; historical food model only |
| migration only | `catalog_dev.food_portion` | zero rows; historical food model only |
| migration only | `catalog_dev.nutrient` | zero rows; historical food model only |
| migration only | `catalog_dev.search_foods(text,integer,text)` | candidate food routes are retired; USDA is the active provider path |
| unproven | `lifeswitch_snapshot.personalization_source_row` | one row; no candidate DDL source or caller found |

The catalog authority matrix previously retained all 14 catalog tables as a
single canonical schema. This audit narrows that decision: the active exercise
routes consume the exercise/family surfaces, while the normalized muscle model
and internal food-search model are dormant. Their data may still be useful, so
absence of a consumer is not deletion authority.

The personalization snapshot is not an application read surface and direct
application access is denied. Its missing migration provenance must be resolved
before the clean baseline: either document and retain it as recovery evidence,
or archive it with an exact hash/count receipt and retire it under a separate
authorization.

## Limitations

- Function overload references are conservatively applied to every matching
  overload.
- Unqualified source identifiers are not accepted as evidence.
- Schema-bound Python SQL templates are accepted only when the same file binds
  the exact schema name.
- The v1 scope covers relations and functions. Types, constraints, indexes,
  grants, roles, extensions, policies, and triggers are preserved by the exact
  restore manifest and contribute dependency evidence, but need their own
  baseline coverage before clean installation is complete.
- A migration-only or unproven result identifies required review; it does not
  prove that an object is safe to remove.

## Next gate

1. Bind and publish the exact committed audit receipt.
2. Create explicit retain/archive/retire decisions for the ten unresolved
   objects.
3. Generate a canonical schema-and-role baseline only from approved retained
   objects.
4. Install that baseline into a fresh disposable database, load synthetic data,
   and run owner/RLS plus application contract tests.
5. Compare the fresh baseline against the approved retained-object manifest.

No production schema change follows automatically from any passing audit.
