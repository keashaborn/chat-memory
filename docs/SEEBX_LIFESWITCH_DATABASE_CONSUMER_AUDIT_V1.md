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
retention only from verified runtime consumers and extension ownership.
Operational-script references remain informational and never seed retention.
It never reads or emits application row values. It writes a mode-0600,
content-free report and explicitly grants no deletion authority.

## Evidence boundary

- isolated database manifest SHA-256:
  `fd71e52ac0825661df33f36c9bff1b595e0cf333221b8bd0ba090ad6dac4ad67`;
- committed auditor candidate:
  `54671375f3068a9d4d76233b2e0e4181a8191680`;
- database object catalog SHA-256:
  `e55ee06cd5733be20175450857b7c8b58eb2c974fcd8873350c3f467df22426b`;
- 218 relations/functions inventoried;
- 211 dependency edges;
- runtime scan: 185 files;
- operational scan: 25 files;
- migration scan: 113 files;
- nine focused auditor tests, eleven disposable-restore contract tests, and the
  complete 1,363-test candidate suite pass.

The committed rerun produced:

- report:
  `/var/backups/seebx-cleanup/lifeswitch-database-consumers-v1/20260822t041100z/consumer-audit.json`;
- report SHA-256:
  `085fe940aa18c3e15c04685d281f84416303aa88a022d8fbc627fc68cd30d066`.

Earlier development receipts and the first committed report are superseded.
The first report incorrectly allowed operational verifier references to seed
retention; the corrected report does not.

## Classification result

| Classification | Count | Meaning |
|---|---:|---|
| application direct | 57 | exact or schema-bound application reference |
| database internal reachable | 32 | dependency reachable from a verified runtime consumer |
| extension owned | 118 | installed PostgreSQL extension object |
| operational reference only | 1 | mentioned by an operational verifier but not reachable from runtime |
| migration only | 5 | found only in historical/installation SQL |
| unproven | 5 | no verified runtime or reachable database consumer |

The eleven review-gated objects are:

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
| operational reference only | `lifeswitch_snapshot.analysis_source_row` | 20 rows; restore verifier proves application access is denied, but no runtime consumer exists |

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
- Operational scripts are inventoried for review evidence but do not make a
  relation or function runtime-retained.
- The v1 scope covers relations and functions. Types, constraints, indexes,
  grants, roles, extensions, policies, and triggers are preserved by the exact
  restore manifest and contribute dependency evidence, but need their own
  baseline coverage before clean installation is complete.
- A migration-only or unproven result identifies required review; it does not
  prove that an object is safe to remove.

## Next gate

1. Bind and publish the exact committed audit receipt.
2. Create explicit retain/archive/retire decisions for the eleven review-gated
   objects.
3. Generate a canonical schema-and-role baseline only from approved retained
   objects.
4. Install that baseline into a fresh disposable database, load synthetic data,
   and run owner/RLS plus application contract tests.
5. Compare the fresh baseline against the approved retained-object manifest.

No production schema change follows automatically from any passing audit.
