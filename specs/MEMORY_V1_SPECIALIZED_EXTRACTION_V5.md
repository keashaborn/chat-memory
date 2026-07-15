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
   - Emits source-local project observations but cannot create, name, or bind a
     project entity.

## Server authority

The model never receives or authors:

- `owner_user_id`;
- source identity or evidence identity;
- trusted `project_id` or project binding;
- a project entity or project name;
- durable entity, observation, claim, or revision IDs;
- approval/review state;
- scalar or multidimensional salience;
- persistence or retrieval activation.

The server injects the source envelope, validates exact spans, creates one
anonymous `project:unresolved` entity from the first accepted project
observation, assigns its source-local reference, namespaces observation
references, validates predicate/object/temporal contracts, and constructs the
existing V5 packet.

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
- Accepted project observations cause the server to create exactly one
  anonymous unresolved project entity; no model project entity is accepted.
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

## Offline correction after the failed-16 rerun

The next revision removes project-entity authorship from the model schema. The
server derives one anonymous `project:unresolved` entity from the first accepted
project observation and never treats model output as a trusted project binding.
This removes a repeated failure mode in which valid project observations were
discarded only because the same response omitted a redundant entity proposal.

The graph-pass validator now rejects malformed self entities before dependent
passes run. A self mention requires `mention_kind=self_reference`,
`relationship_role=user:self`, and direct grounding in a first-person singular
source span. The enrichment layer derives current/deceased pet roles only from
accepted owner-pet relationships and death observations; an unrelated animal
death cannot become a user pet.

The project-pass validator now treats a project-scope deferral without an
atomic project observation as repairable invalid output and rejects global
question/context deferrals that belong to temporal content. Bounded repair
instructions include exact-span recovery guidance. Pass instructions now make
context-dependent recall questions, embedded occupation statements, current
website/app state, planned veterinary procedures, and mixed question-plus-state
turns explicit. These changes remain zero-write and runtime-inactive. No new
external model evaluation is authorized by the offline revision.

The second rerun selection is a separate versioned manifest containing exactly
the ten cases that failed the authorized failed-16 run. It binds that run's
evaluator commit and report hash, retains manifest order, and rejects the old
16-case count under the new selection version. Its preflight remains zero-call;
using it for an external evaluation requires separate authorization.

## Authorized failed-10 live evaluation

Commit `6f611c3aaabd7f3c96e1882b3189e595670654bb` evaluated the exact
ten-case selection with `gpt-5.2`, `store=false`, and report mode 0600. Selection
hash `8919ff4880a3575c728ec217ad8fe9267db4cb69b70d39c68cd0be0d29bf5432`
bound `v5-01`, `v5-03`, `v5-04`, `v5-08`, `v5-10`, `v5-13`, `v5-14`,
`v5-15`, `v5-22`, and `v5-25` in manifest order. The run made 37 calls,
including nine bounded repairs. There were no refusals, incomplete responses,
request errors, deterministic rejections, or final integrity errors.

Five cases passed: `v5-01`, `v5-08`, `v5-13`, `v5-14`, and `v5-22`. Five
cases remain closed:

- `v5-03` produced the correct question/context deferrals but retained unused
  self and concept entities, which incorrectly forced manual review;
- `v5-04` extracted the occupation correctly but omitted the required
  ambiguity deferral for the uncertain credential/organization phrase;
- `v5-10` retained one redundant non-exact entity span after its bounded graph
  repair;
- `v5-15` treated an explicit pet-name correction as a context-dependent
  question and emitted no corrective observation;
- `v5-25` identified project content, but its project temporal value remained
  invalid after the bounded repair.

The report SHA-256 is
`3b71ad68596a51b68f5102a2e518703bcb6848fdd2824ab602c69b9efab407c6`.
The zero-write proof passed: all 47 database relations retained SHA-256
`437b38a8d85f771caec1eaf003356fde77775c26319bd463fc0f447fef67ddc4`,
and all six owner-filtered Qdrant points retained SHA-256
`60ff96fcc03e559d10a5a37144f145b5b1f699f1ae47d6e18b4bdcae8c8e4f87`.
No staging, persistence, retrieval activation, or additional external rerun is
authorized by this result.

## Offline correction after the failed-10 evaluation

The next revision keeps all five changes server-bounded and runtime-inactive.
Entity mentions not referenced by an accepted observation are removed before
assembly, preventing question-only concepts from creating manual-review work.
An invalid source span may be pruned only when the same entity, observation, or
deferral retains at least one independently valid exact span; the packet records
that normalization. A sole invalid evidence span still fails closed.

An uncertain qualifier immediately attached to an organization or credential
adds `ambiguous_transcription` without deleting a separately supported
occupation observation. A self-contained, explicitly marked pet-name correction
can deterministically create its source-local animal subject, canonical-name
observation, and unresolved owner-scoped `corrects`/`supersedes` comparisons;
question/context deferrals shaped as that same correction are removed.

`project.current_state` temporal materialization is server-authoritative. The
model supplies the predicate and exact evidence span; the server constructs the
open state-validity interval from the trusted source timestamp. The remaining
five-case selection is preflight-only and rejects any external model call. A
new external evaluation requires a separately authorized selection manifest.

Commit `c823dacbd34200f4841035e5cfea827eaa4df782` passed all 124 offline
Memory V1 Python tests. Its exact-five preflight selected `v5-03`, `v5-04`,
`v5-10`, `v5-15`, and `v5-25` with selection SHA-256
`956c918cc33ac218655493ddf6a4bd8f2d56c29abd14897083e2dee96ef98dba`.
It made zero external model calls. The mode-0600 report at
`/home/ubuntu/memory-v1-reviews/v5-specialized-remaining5-offline-correction-preflight-20260715T225729Z.json`
has SHA-256
`df4a7dd472a452fc26c750f0b03a89c75bfacd7240e652f38ad5b14d2efd2a8e`.

The preflight zero-write proof passed. All 47 database relations retained
SHA-256
`437b38a8d85f771caec1eaf003356fde77775c26319bd463fc0f447fef67ddc4`,
and all six owner-filtered Qdrant points retained SHA-256
`60ff96fcc03e559d10a5a37144f145b5b1f699f1ae47d6e18b4bdcae8c8e4f87`.
No relation changed. The exact-five manifest remains preflight-only; this result
does not authorize an external rerun, staging, persistence, or retrieval use.
