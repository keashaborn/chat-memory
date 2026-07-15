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

For live evaluation, each pass receives only its permitted subset of the
proposed/runtime-inactive V5 predicate registry and the object contracts used by
that subset. Registry subject type, modality, projection, surface policy,
temporal semantics, sensitivity floor, and object shape are revalidated before
pass selection. A registry violation therefore enters that pass's single
bounded repair; it is not deferred until final packet assembly.

The manifest-bound live wrapper requires the canonical 25-source manifest hash,
revalidates each owner-scoped source against Postgres under `brains_app` and
forced RLS, and takes complete `memory.*`, manifest-source, and owner-filtered
Qdrant signatures before and after evaluation. Its preflight mode constructs no
OpenAI client and records zero external calls. Reports are create-only mode 0600.

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

## First specialized live evaluation

Commit `585f858305ffe31e88c9ead1b2726e2b8d131cef` evaluated the exact
25-source manifest with `gpt-5.2`, `store=false`, and report mode 0600. The run
made 81 calls, including eight bounded repairs. There were no refusals,
incomplete responses, request errors, or final deterministic registry
rejections. Nine cases passed and sixteen failed. Postgres and owner-filtered
Qdrant signatures were unchanged.

Failure review found:

- six cases blocked by JSON-mode datetime strings crossing a strict
  Pydantic-to-Pydantic assembly boundary;
- two cases that remained invalid after their single pass-local repair;
- question/context deferral, occupation/transcription, pet-name correction,
  and project classification misses in the remaining cases;
- one dangling project-scope deferral caught by final integrity validation.

The assembler now uses Python-mode model dumps so timezone-aware `datetime` and
`date` values retain their typed representation. A regression test exercises
that boundary. Pass guidance now explicitly assigns global question/ambiguity
deferrals to temporal content, requires the graph pass to declare all later
content entities, requires exact short spans, and distinguishes project
requirements, proposals, current state, and pure external questions.

Case `v5-08` was resolved by explicit policy authorization. Its direct statement
that the memory project is partially complete is a `project.current_state`
observation with open `state_validity` anchored to source time, unresolved
project scope, and manual review. Later evidence closes or supersedes the
interval; it does not erase the original observation. The prior transient-only
expected result is retired.

The authorized rerun is restricted to the exact 16 failures from the first
specialized evaluation. The checked-in subset manifest binds the source
manifest hash, revised case-contract hash, baseline report hash, baseline
evaluator commit, authorization scope, and manifest-ordered case IDs. The live
runner rejects unknown, duplicate, reordered, added, or removed cases and still
hashes all 25 manifest sources plus all `memory.*` relations and owner-filtered
Qdrant state before and after the selected evaluation.

## Authorized failed-16 rerun

Commit `b90bf1745847d6c607705c31a834d549d8c9a548` evaluated the exact
16-case selection with `gpt-5.2`, `store=false`, and report mode 0600. Selection
hash `ed6a17ccb97a31d5d497f51ecdb0f63caca50a12038c3983c8ad738bbfcbd2d7`
bound the manifest-ordered cases. The run made 53 calls, including seven
bounded repairs. There were no refusals, incomplete responses, or request
errors. Six cases passed: `v5-02`, `v5-05`, `v5-18`, `v5-19`, `v5-20`, and
`v5-23`. Ten cases failed. The report SHA-256 is
`111eb78193c64631358a7e9c2a5155f2a535c2f7fccb066c96f5782b1a9c2ba1`.

The prior typed datetime assembly defect did not recur. Remaining failures are
closed-contract failures:

- `v5-01` over-extracted an ambiguous/question span as project current state;
- `v5-03` and `v5-14` omitted the required context-missing deferral;
- `v5-04` suppressed the supported occupation observation with its ambiguous
  transcription instead of producing a mixed result;
- `v5-08` and `v5-25` suppressed project current-state content instead of
  producing unresolved, manually reviewed project observations;
- `v5-10` and `v5-22` remained invalid after one bounded exact-span repair;
- `v5-13` and `v5-15` produced self entity mentions without the required
  `user:self` relationship role; `v5-13` then rejected two observations whose
  subject reference no longer resolved.

The zero-write proof passed. Before and after the run, all 47 database
relations hashed to
`437b38a8d85f771caec1eaf003356fde77775c26319bd463fc0f447fef67ddc4`
and the six owner-filtered Qdrant points hashed to
`60ff96fcc03e559d10a5a37144f145b5b1f699f1ae47d6e18b4bdcae8c8e4f87`.
No relation changed. No staging, persistence, retrieval activation, or further
external rerun is authorized by this result.
