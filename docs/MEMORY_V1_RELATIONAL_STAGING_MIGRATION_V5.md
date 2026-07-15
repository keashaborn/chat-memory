# Memory V1 relational staging migration V5

Status: proposed, executable, runtime-inactive. Server: seebx backend.

## Boundary

`20260715_memory_v1_relational_staging_v5.sql` creates staging and audit structures only. It does not change `memory_v1_store.py`, retrieval, projection, consolidation, Qdrant, prompt construction, owner resolution, authentication, or service timers. `brains_app` receives read-only access to the global predicate contract and no access to owner-scoped V5 staging tables.

The migration installs 25 extraction-enabled V5 predicates and 19 legacy read-only contract rows. The registry remains `status=proposed` and `runtime_active=false`. Unknown or legacy predicates cannot be inserted as V5 observations.

## Data flow

1. `memory.entity_mention` stores source-local entity references and immutable source spans. It contains no model-created durable entity key.
2. `memory.entity_resolution_plan` and `memory.entity_resolution_candidate` store the owner-scoped resolver decision and its feature vector. There is no scalar identity score.
3. `memory.entity_resolution_review` hash-locks manual review. `memory.entity_resolution_apply` is the append-only applied-result ledger.
4. `memory.observation` stores the extracted relation against mention IDs. It does not directly bind durable entity IDs.
5. `memory.observation_temporal` stores one typed temporal record using `tstzrange`, `daterange`, a relative-offset object, or the deliberately narrow recurring marker.
6. `memory.observation_entity_binding` binds an observation to durable entities only after the corresponding resolutions have been applied.
7. `memory.claim_observation` and `memory.candidate_observation` preserve provenance for later projection. This migration does not create either link.

All owner tables use forced RLS and composite owner foreign keys. All rows are append-only. Source-backed inserts require active evidence owned by the current actor. New trigger and validation functions are `SECURITY INVOKER`.

## Entity rules enforced now

- The model supplies `entity_ref`, type, mention kind, text, source spans, and confidence only.
- `self` and `project` entities cannot be created by a model resolution proposal.
- Automatic linking requires an active, type-matched candidate with zero conflicting attributes. Named auto-links additionally require exactly one same-name candidate and an exact canonical name or alias. Trusted self binding is the only exception.
- Manual resolution cannot be applied without an approved review whose mention, candidate-set, and decision hashes match the plan.
- Same normalized names may coexist both across owners and within one owner. No name uniqueness rule silently merges people or animals.

## Temporal rules enforced now

- Calendar precision uses `daterange`; time-zone-aware instants and intervals use `timestamptz`/`tstzrange`.
- Bounded values use half-open `[)` ranges. Open ranges may have exactly one unbounded endpoint.
- Relative values retain direction, magnitude, unit, approximation, and the `evidence_observed_at` anchor instead of pretending to be an exact timestamp.
- Observation time, event occurrence, planned time, and state validity remain separate semantics.
- Month/year precision cannot be persisted as an exact instant.
- A missing temporal statement is represented explicitly by the all-`none` temporal row.

## Clone security suite

`tools/memory_v1_relational_staging_v5_ci.sh` rebuilds the current Memory V1 foundation in an isolated PostgreSQL 16 container, applies the migration twice, runs adversarial tests, verifies schema dumpability, proves the migration cannot run as `brains_app`, rolls it back, and confirms the predicate registry returns to its baseline.

`tools/memory_v1_relational_staging_v5_production_clone.sh` reads only the production schema with `pg_dump --schema-only`, restores it into an isolated container, and runs the same migration/security/rollback sequence. No production rows, chat content, credentials, or Qdrant data enter the clone.

The SQL suite proves:

- forced RLS and no service-role access on every owner-scoped staging table;
- no new `SECURITY DEFINER` functions;
- owner A cannot be read or referenced by owner B;
- writes with a missing actor fail closed;
- cross-owner durable entity references fail through composite foreign keys;
- identical names do not merge across or within accounts;
- legacy predicates, unresolved project scope, invalid time precision, model-created projects, mutation, and deletion are rejected;
- a valid mention → resolution → observation → temporal → entity-binding path works and rolls back to zero rows.

## Activation gates not included

Production installation, writer-role creation, controlled insert/apply functions, extractor wiring, projection into claims/preferences/projects, retrieval activation, and background scheduling require separate review. The controlled writer must receive only the exact table/function privileges required for its transaction; `brains_app` must not receive direct owner-table writes.
