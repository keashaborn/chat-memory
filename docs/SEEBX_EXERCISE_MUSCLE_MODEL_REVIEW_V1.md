# SeeBx exercise-muscle model review v1

Date: 2026-08-21

Status: canonical model approved for candidate development; no database, route,
deployment, retirement, or activation authority

## Approved candidate decision

The isolated LifeSwitch catalog contains two different representations of
exercise muscles:

1. `catalog_dev.exercise.primary_muscles` and `secondary_muscles` store repeated
   text arrays directly on each exercise.
2. `catalog_dev.muscle`, `muscle_alias`, and `exercise_muscle` store canonical
   muscle identities and explicit weighted exercise relationships.

The second representation is the approved canonical model for candidate
development. This is a product-model decision, not production activation
authority.

## Read-only findings

The review queried only PostgreSQL catalog metadata and aggregate counts inside
a repeatable-read, read-only transaction. No muscle, alias, exercise, or notes
text was emitted.

- 83 muscles form an eight-root, 75-child hierarchy;
- all muscles have normalized names and regions;
- 20 aliases are normalized, locale-bound, and unique within locale;
- 1,058 exercise-muscle mappings cover 154 of 188 exercises;
- 563 mappings are primary, 364 secondary, and 131 stabilizer;
- all mappings have valid weights from 0.050 through 1.000 and nonblank notes;
- 71 exercises have stabilizer information that the array model cannot express;
- the normalized primary display-name set exactly matches only 35 of 188 array
  records, and the normalized secondary set matches 74 of 188;
- 34 exercises have no normalized mapping;
- `exercise_muscle_rule` contains zero rows and has no runtime consumer.

The aggregate evidence is recorded in
`ops/database/lifeswitch_exercise_muscle_review_v1.json`. It is bound to the
exact database manifest, object catalog, consumer report, and candidate commit.

## Canonical flow

Use one flow:

`catalog_dev.muscle` and `muscle_alias`
→ `catalog_dev.exercise_muscle`
→ one read-only catalog adapter
→ one versioned exercise-muscle capability contract
→ LifeSwitch and future SeeBx frontends.

The existing primary/secondary arrays should initially be produced as a
compatibility projection from the normalized relationships. They should not
remain a second writable authority. After all 188 exercises are reviewed and
downstream parity is proven, the arrays can be retired under a separate
rollback-bound migration.

`catalog_dev.exercise_muscle_rule` is not part of the canonical flow merely
because it shares a name with the useful tables. It is empty and unconsumed, so
its explicit disposition is `archive_candidate`. That remains a review state,
not permission to remove it from production.

## Required gates before activation

1. Define the versioned output contract without exposing database internals.
2. Curate the 34 unmapped exercises and reconcile all 188 array projections.
3. Prove adapter, owner/public visibility, sorting, and compatibility behavior
   against a disposable Work Runner database.
4. Make normalized relationships the sole write authority.
5. Retire arrays and the empty rule table only after exact backup, restore,
   rollback, and frontend compatibility evidence.

No production change follows automatically from this review.
