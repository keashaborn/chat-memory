# Memory V1 Specialized Extraction V5

Status: proposed, zero-write, runtime-inactive.

## Purpose

Replace the overloaded single V5 extraction call with three narrow Structured
Output passes. Schema compliance does not establish semantic correctness; every
pass remains subject to deterministic server validation and the existing V5
predicate registry.

## Pass order

1. `entity_graph`
   - Owns entity mentions, `relationship.*`, and `residence.lives_at`.
   - Must declare every referenced self, person, animal, and place.
   - Cannot emit attributes, health content, preferences, corrections, or
     project knowledge.
2. `temporal_content`
   - Receives the validated, source-local entity catalog from pass 1.
   - Owns non-project attributes, events, states, health observations/plans,
     preferences, corrections, and comparison hints.
   - Cannot create entities or emit graph/project predicates.
3. `project_knowledge`
   - Owns only `project.current_state`, `project.requirement`,
     `project.proposed_feature`, and `project.constraint`.
   - May propose one unresolved project entity but cannot bind a trusted project.

## Server authority

The model never receives or authors:

- `owner_user_id`;
- source identity or evidence identity;
- trusted `project_id` or project binding;
- durable entity, observation, claim, or revision IDs;
- approval/review state;
- scalar or multidimensional salience;
- persistence or retrieval activation.

The server injects the source envelope, validates exact spans, assigns the
unresolved project reference, namespaces observation references, validates
predicate/object/temporal contracts, and constructs the existing V5 packet.

The API orchestration is dependency-injected and zero-write. It hashes owner
identity into `safety_identifier`, hashes source identity into non-reversible
metadata, sends `store=false`, and never places the owner UUID or raw source ID
in instructions, input, metadata, or audit output. Empty source text makes zero
API calls. Each non-empty source runs the passes sequentially; only the temporal
content pass receives the validated source-local entity catalog.

## Deterministic assembly invariants

- Entity references must resolve to the validated entity-graph catalog.
- Project entities cannot originate in the entity-graph pass.
- Graph predicates cannot originate in temporal content.
- Project predicates cannot originate in temporal content.
- Project observations require an unresolved project entity proposal.
- Observation references are renumbered by the server in pass order.
- Comparison hints may reference only accepted temporal-content observations.
- Existing V5 limits remain 24 entities, 32 observations, 32 comparison hints,
  32 deferrals, and 32 findings.
- Any unresolved reference, crossed lane, duplicate reference, or budget excess
  fails the entire assembled packet closed.

## Repair policy

Each pass may receive at most one validation-driven repair request. Repair input
contains deterministic error categories, never expected evaluation labels.
The repaired pass is accepted only if deterministic rejection and integrity
counts strictly improve. There is no third attempt.

Completed status, parsed output, and output content are checked separately.
Any refusal, incomplete response, missing parsed output, remaining validation
error after the bounded repair, or assembly error fails the source closed. Audit
rows retain response ID, pass, attempt, status, schema hash, validation reasons,
quality, selection, model, and `store=false`; they exclude raw owner and source
identifiers.

## Activation gate

Before external live evaluation:

- all specialized contract and existing V5 tests must pass;
- code must contain no persistence or retrieval activation path;
- the registry must remain proposed/runtime-inactive;
- the existing 25-record manifest must remain hash-locked;
- a new explicit authorization is required before the 25 records are sent to
  the OpenAI API under this new multi-pass design.

Before production staging:

- all 25 cases must pass in one clean run;
- repeatability and foreign-owner isolation runs must pass;
- Postgres and Qdrant before/after hashes must remain identical;
- staging/apply requires a separate production backup and authorization manifest.
