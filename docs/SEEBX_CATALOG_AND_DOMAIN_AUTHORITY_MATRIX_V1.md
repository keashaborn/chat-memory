# SeeBx catalog and LifeSwitch domain authority matrix v1

Date: 2026-08-20

Status: controlling candidate design decision; no production migration,
deployment, route retirement, database write, credential change, or deletion
authority

Evidence baseline:

- SeeBx production: `49f9e60cf4321c8e42c359845c1a62a8c987614d`, clean,
  service active with zero restarts;
- cleanup candidate and GitHub branch:
  `555ecfd8af052a2e6f62214ca76ad7a3d438e134`, clean;
- Verbal Sage production: `858b61527186571cabb5580dc5159fdf6b69ae2e`,
  service active with zero restarts.

## Decision

The isolated LifeSwitch PostgreSQL `catalog_dev` schema is the sole target
catalog data authority. The platform `memory.catalog_dev` schema is an exact
duplicate and becomes a read-freeze, cutover, soak, and later retirement
candidate. It is not deleted or modified by this decision.

This choice is evidence-based:

- all 14 tables have identical column hashes, exact row counts, and content
  hashes in both databases;
- all six schema functions have identical definitions;
- only the isolated copy is integrated with LifeSwitch domain data through
  `lifeswitch_nutrition.meal_plan_item_food_id_fkey`;
- the isolated `lifeswitch_app` role has catalog read only, while platform
  `brains_app` currently has full catalog DML;
- nutrition already reads the isolated catalog and must not fall back to the
  platform database.

Catalog rows are shared reference data, not owner records. They do not require
per-owner RLS, but ordinary application identities must remain read-only.
Catalog mutation requires a separately authenticated curator capability and a
dedicated least-privilege writer role; it must never be a side effect of GET.

## Database parity evidence

The inventory ran in read-only transactions against both protected service
DSNs. It emitted no credentials or row contents. SHA-256 content hashes were
computed over length-delimited, canonical `to_jsonb(row)::text` rows sorted by
their canonical text.

| `catalog_dev` table | Rows in platform | Rows in LifeSwitch | Columns | Content |
|---|---:|---:|---|---|
| `equipment_brand` | 9 | 9 | exact | exact |
| `exercise` | 188 | 188 | exact | exact |
| `exercise_alias` | 182 | 182 | exact | exact |
| `exercise_family` | 28 | 28 | exact | exact |
| `exercise_family_member` | 131 | 131 | exact | exact |
| `exercise_muscle` | 1,058 | 1,058 | exact | exact |
| `exercise_muscle_rule` | 0 | 0 | exact | exact |
| `food` | 7 | 7 | exact | exact |
| `food_alias` | 5 | 5 | exact | exact |
| `food_nutrient` | 0 | 0 | exact | exact |
| `food_portion` | 0 | 0 | exact | exact |
| `muscle` | 83 | 83 | exact | exact |
| `muscle_alias` | 20 | 20 | exact | exact |
| `nutrient` | 0 | 0 | exact | exact |
| **Total** | **1,711** | **1,711** | **exact** | **exact** |

The six exact functions are `escape_like`, `immutable_unaccent`, `norm_text`,
`search_exercises`, `search_foods`, and `tg_set_updated_at`. ACLs intentionally
differ and are not included in parity: platform `brains_app` has DML; isolated
`lifeswitch_app` has read only.

## Catalog route/caller/effect matrix

There are nine mounted backend routes and six Verbal Sage BFF proxies. Current
product source calls four catalog route families. Fifteen retained Nginx access
logs contain three successful catalog calls: two USDA guide and one USDA
barcode request. The retained SeeBx service journal, spanning
2025-11-07 through 2026-08-17, contains zero catalog requests. Logs are
supporting evidence only; mounted source callers control retention decisions.

| Backend route | Current effect | Verbal Sage evidence | Target disposition |
|---|---|---|---|
| `GET /catalog/exercises/search` | platform catalog read | BFF proxy; active Workouts, Exercises, and Training Capture callers | KEEP; isolated catalog read adapter |
| `GET /catalog/exercises/browse` | platform catalog read | BFF proxy; active Workouts caller | KEEP; isolated catalog read adapter |
| `GET /catalog/foods/search` | platform catalog read | BFF proxy; no current UI caller or retained hit | UNMOUNT unless a named caller is retained |
| `GET /catalog/foods/by_barcode` | platform read plus Open Food Facts lookup and catalog upsert | no BFF proxy, repository caller, or retained hit | RETIRE; GET must never mutate; rebuild only as provider query plus explicit curator command |
| `GET /catalog/foods/usda/search` | USDA provider lookup only | BFF proxy; no current UI caller or retained hit | UNMOUNT unless a named caller is retained |
| `GET /catalog/foods/usda/barcode` | USDA provider lookup only | BFF proxy; active Foods caller; one retained 200 | KEEP behind USDA adapter |
| `GET /catalog/foods/usda/guide` | USDA provider lookup/scoring only | BFF proxy; active Foods caller; two retained 200s | KEEP behind USDA adapter |
| `POST /catalog/foods/usda/import` | USDA lookup plus platform catalog upsert | no BFF proxy, repository caller, or retained hit | UNMOUNT; rebuild only in authenticated curator capability |
| `POST /catalog/foods/approve` | platform catalog update | no BFF proxy, repository caller, or retained hit | UNMOUNT; rebuild only in authenticated curator capability |

The current platform HTTP boundary exempts every catalog GET from service-token
verification. Current BFF proxies authenticate LifeSwitch product access but
do not forward the verified bearer or service token because the backend GETs
are public. The target is default-deny: the paired frontend forwards the
original bearer and service credential, SeeBx verifies the actor/service
principal, and provider/read adapters receive no identity authority of their
own. This security change is a separate paired contract batch from database
repointing.

## Direct SQL/transaction inventory

The callsite counts below are AST-derived `fetch`, `fetchrow`, `fetchval`,
`execute`, and `transaction` calls. They exclude connection acquisition,
connection close, provider HTTP calls, and the non-database LifeSwitch stage
`.execute` method.

| Capability module | Mounted routes | Direct SQL/transaction callsites | Current database | Frontend BFF surface |
|---|---:|---:|---|---|
| `catalog/routes.py` | 9 | 7 | platform PostgreSQL | 6 route files |
| `nutrition/routes.py` | 17 | 61 | isolated LifeSwitch PostgreSQL | nutrition group: 27 route files |
| `nutrition/meals.py` | 7 | 14 | isolated LifeSwitch PostgreSQL | nutrition group |
| `nutrition/logs.py` | 7 | 26 | isolated LifeSwitch PostgreSQL | nutrition group |
| `training/routes.py` | 41 | 72 | isolated LifeSwitch PostgreSQL | training group: 39 route files |
| `training/logs.py` | 0 | 2 | caller-owned isolated connection | training group |
| `measurements/routes.py` | 3 | 4 | isolated LifeSwitch PostgreSQL | 3 route files |
| `plans/routes.py` | 5 | 8 | isolated LifeSwitch PostgreSQL | 5 route files |

These are eight of the eleven remaining direct database-effect capability
modules. Forms, AI operations, and telemetry are separate capability groups
and are not part of this catalog/domain decision.

## Canonical target flow

```text
authenticated frontend or authorized service principal
  -> catalog read/provider route
  -> catalog capability policy
  -> isolated catalog read adapter OR USDA provider adapter
  -> stable catalog/provider response
```

```text
authenticated curator with explicit catalog-write capability
  -> validated curator command
  -> catalog mutation service
  -> dedicated isolated-catalog writer adapter/role
  -> immutable audit record
```

Forbidden target flows:

- catalog route to platform `POSTGRES_DSN`;
- catalog GET to any insert, update, delete, or implicit import;
- ordinary `lifeswitch_app` identity to catalog write;
- provider lookup to database mutation;
- public network prefix bypass for mounted catalog routes;
- nutrition/training fallback from isolated PostgreSQL to platform PostgreSQL;
- two writable catalog copies.

## Bounded implementation order

1. Add a narrow isolated-catalog read adapter and a USDA provider adapter.
   Repoint only the four active read families after exact response parity;
   platform and isolated rows are already hash-identical.
2. In a separate route-retirement batch, unmount the five no-current-caller
   surfaces unless a named retained caller is produced. Explicitly guard
   against reintroducing a GET with write effects.
3. In a paired backend/frontend security batch, remove the public catalog GET
   prefix, forward the original bearer plus service credential, and verify
   authenticated LifeSwitch access and denial paths.
4. Soak with both catalog copies hash-monitored and the platform copy frozen.
   Any divergence stops retirement.
5. After backup/restore proof and explicit production authorization, remove
   platform catalog grants/schema and retain the isolated copy only.
6. Split nutrition and training by domain aggregate, moving one transaction
   boundary at a time behind isolated PostgreSQL adapters.

Every step preserves Git rollback. No step combines route retirement, data
migration, security-boundary change, and production activation.
