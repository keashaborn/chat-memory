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

## Authorized remaining-five external evaluation

The separate version-4 selection binds the correction evaluator commit
`c823dacbd34200f4841035e5cfea827eaa4df782`, the successful zero-call preflight
report, the unchanged 25-case contract, and exactly `v5-03`, `v5-04`, `v5-10`,
`v5-15`, and `v5-25` in manifest order. Its authorization permits one
`store=false`, zero-write external evaluation. It does not permit staging,
persistence, projection, retrieval activation, or another external rerun.

Commit `9a17a6e25f5500c7148b99b6a5f7ab06d1ab37b9` executed the authorized
remaining-five evaluation with `gpt-5.2`, `store=false`, and selection SHA-256
`ed52b4e04c5062fa2eebbe3ac63a209052e2121a09525e79b406fdf69d689686`.
The run made 22 model calls, including seven bounded repairs. There were no
refusals, incomplete responses, request errors, deterministic rejections, or
integrity errors.

Four cases passed: `v5-03`, `v5-04`, `v5-15`, and `v5-25`. The remaining
`v5-10` packet extracted every required predicate, temporal feature, projection
class, deferral, and family entity. It remained closed only because all three
distinct named sisters used the generic role `family:sister` instead of the
required source-order roles `family:sister:1`, `family:sister:2`, and
`family:sister:3`.

The mode-0600 report at
`/home/ubuntu/memory-v1-reviews/v5-specialized-remaining5-authorized-live-20260715T230349Z.json`
has SHA-256
`2e719580e433b2154b05041d3f01d6fad5ddf6b6731857c4f1f16544b3abff3e`.
The zero-write proof passed: all 47 database relations retained SHA-256
`437b38a8d85f771caec1eaf003356fde77775c26319bd463fc0f447fef67ddc4`,
and all six owner-filtered Qdrant points retained SHA-256
`60ff96fcc03e559d10a5a37144f145b5b1f699f1ae47d6e18b4bdcae8c8e4f87`.
No relation changed. No additional external evaluation, staging, persistence,
or retrieval activation is authorized by this result.

## Offline repeated-sibling normalization

Repeated same-type sibling roles are now normalized deterministically from
validated source-local graph output. The rule applies only to named person
entities that are objects of `relationship.sibling_of` observations from the
validated `user:self` entity. At least two entities must share the same base
role (`family:sister`, `family:brother`, or `family:sibling`), and each must
have a distinct exact source position. The server then assigns `:1`, `:2`, and
subsequent ordinals in source order, replacing any model-proposed ordinals.

Single siblings, mixed sibling types, missing/invalid spans, tied source
positions, and sibling relationships not rooted at `user:self` remain
unchanged. The normalizer is zero-write and runtime-inactive. This offline
change authorizes no external evaluation, staging, persistence, projection, or
retrieval activation.

Commit `6e43a90150206ea4473b32d3545d30e8854b6794` passed all 129 offline
Memory V1 tests. Its hash-locked five-case preflight selected `v5-03`, `v5-04`,
`v5-10`, `v5-15`, and `v5-25` and made zero external model calls. The mode-0600
report at
`/home/ubuntu/memory-v1-reviews/v5-specialized-sibling-source-order-preflight-20260715T233109Z.json`
has SHA-256
`50847d1dbc71cb7d0cd7af6cc3d4765dfe0e5319a0ca4918157c69bb2f816431`.

The preflight zero-write proof passed. All 47 database relations retained
SHA-256
`437b38a8d85f771caec1eaf003356fde77775c26319bd463fc0f447fef67ddc4`,
and all six owner-filtered Qdrant points retained SHA-256
`60ff96fcc03e559d10a5a37144f145b5b1f699f1ae47d6e18b4bdcae8c8e4f87`.
No external evaluation, staging, persistence, projection, or retrieval
activation occurred.

## Production visibility reconciliation and saved-packet replay

The V5 production schema remained installed after recovery. A read through the
application DSN initially appeared to show only the pre-V5 tables because
PostgreSQL `information_schema.tables` exposes only objects visible to the
current role. On the same PostgreSQL postmaster, the maintenance role sees 79
`memory` tables, including all V5 objects; `brains_app` sees only its 46
permitted tables. The V5 predicate registry contains all 44 contracts. This is
the intended privilege boundary, not schema removal.

Commit `efcd560ed4e381e802863beee42a70295620556a` added a hash-bound,
owner-scoped replay path for archived `store=false` model packets and passed
all 130 offline Memory V1 tests. It reloads the source under forced RLS,
validates the saved report and source hashes, performs deterministic server
normalization, reevaluates the locked case contract, and records before/after
Postgres and Qdrant signatures. It constructs no model client and makes zero
external model calls.

The saved `v5-10` packet replay changed only the three repeated sibling roles
from `family:sister` to `family:sister:1`, `family:sister:2`, and
`family:sister:3`. The complete case contract then passed with no findings,
deterministic rejections, or integrity errors. The mode-0600 report at
`/home/ubuntu/memory-v1-reviews/v5-10-deterministic-sibling-replay-20260715T235705Z.json`
has SHA-256
`0d0452bd32fae4ce595742bb1e2b928793f333ef55eaf9fff53e039299e5b0d8`.

The replay zero-write proof passed. All 47 database relations retained SHA-256
`437b38a8d85f771caec1eaf003356fde77775c26319bd463fc0f447fef67ddc4`,
and all six owner-filtered Qdrant points retained SHA-256
`60ff96fcc03e559d10a5a37144f145b5b1f699f1ae47d6e18b4bdcae8c8e4f87`.

## Full-contract preflight

Commit `945769a9f53dacf8ff6b0955598fe078182734a5` added a preflight-only
selection containing exactly `v5-01` through `v5-25` in manifest order. The
selection is bound to the source manifest, case contract, prior evaluator
commit, and successful saved-packet replay report. Generic preflight-only scope
enforcement prevents this selection from entering an external-call path.

All 131 offline Memory V1 tests passed. The full-contract preflight selected
all 25 sources, made zero external model calls, and produced the mode-0600
report
`/home/ubuntu/memory-v1-reviews/v5-specialized-full25-preflight-20260716T000405Z.json`
with SHA-256
`ee61d625ec5e923ab127974d8c83d77e6c22c496cd5953cf96c0517f4576623d`.
Its selection SHA-256 is
`ccd0072fb1e3f3a9d2e2906c1f4be3bd7367cdf5b9b209bf6396f5bfbe3f5384`.

The preflight zero-write proof passed. All 47 database relations retained
SHA-256
`437b38a8d85f771caec1eaf003356fde77775c26319bd463fc0f447fef67ddc4`,
and all six owner-filtered Qdrant points retained SHA-256
`60ff96fcc03e559d10a5a37144f145b5b1f699f1ae47d6e18b4bdcae8c8e4f87`.
An external full-contract evaluation requires a separate manifest explicitly
binding this preflight result; this preflight does not authorize it.

## Authorized full-25 evaluation and offline reconciliation

Commit `8e9f8328cc242e0931acc7f0c4dbfe67ad1d3118` evaluated all 25
manifest sources once with `gpt-5.2` and `store=false`. The run made 98 model
calls, including 23 bounded repairs. There were no refusals, incomplete
responses, request errors, deterministic rejections, or integrity errors.
Sixteen cases passed and nine failed. The mode-0600 report at
`/home/ubuntu/memory-v1-reviews/v5-specialized-full25-authorized-live-20260716T002021Z.json`
has SHA-256
`615e73c137a5795f0ec64c58d463659a94b143af1cf055a1a3780ffae29dbd31`.

The zero-write proof passed. All 47 database relations retained SHA-256
`437b38a8d85f771caec1eaf003356fde77775c26319bd463fc0f447fef67ddc4`,
and all six owner-filtered Qdrant points retained SHA-256
`60ff96fcc03e559d10a5a37144f145b5b1f699f1ae47d6e18b4bdcae8c8e4f87`.

Failure reconciliation produced five deterministic corrections:

- `project_knowledge` now discards non-project deferrals because
  `temporal_content` is their sole authoritative lane;
- a packet with no observation and only `question_only`, `context_missing`, or
  `transient_state` is `no_observation`; the deferrals remain auditable but do
  not imply that a memory candidate exists;
- a recognized technical product name in an otherwise observation-free
  technical question is not an ambiguous personal-memory candidate;
- an explicit plural residence clause may create sibling residence edges only
  when one already validated, owner-linked sibling group is the unique
  antecedent and the complete atomic result fits the graph budget;
- case `v5-07` now follows the same rule as equivalent project cases: its direct
  statement that the app tracks nutrition and weightlifting is current project
  state even though the turn also asks a question.

The revised case contract intentionally invalidates every earlier external-call
selection hash. Historical authorization manifests remain checked in as audit
evidence and fail closed against the new contract; none was silently rebound.

All 139 offline Memory V1 tests pass. A zero-call in-memory replay of the
authorized report reevaluated all 23 rows that contain archived model packets;
all 23 pass the revised contract. Cases `v5-04` and `v5-15` have no archived
packet because the prior project pass failed closed. Their cross-lane failure
is covered by three-pass regression tests, but confirming their model output
requires a new separately authorized external evaluation. No staging,
persistence, projection, retrieval activation, or prompt change occurred.

Commit `c49d745f9f36797323263afdd37340a47ba9f0cf` contains this
reconciliation. Its mode-0600 offline report at
`/home/ubuntu/memory-v1-reviews/v5-specialized-full25-offline-reconciliation-20260716T010132Z.json`
has SHA-256
`e667056d086e262ed8082e7c7ec6e99359cf364bb0281a641d0f12d3bcb414cd`.
The report made zero external calls and repeated the unchanged 47-relation and
six-point Postgres/Qdrant proof above.

Commit `4b5988bb17f74ca78e8ccbe05f9ac9e726750b7d` adds a new
preflight-only selection containing exactly `v5-04` and `v5-15` in manifest
order. It is bound to the revised case contract and offline reconciliation
report. All 141 offline tests pass. The preflight made zero external calls and
produced the mode-0600 report at
`/home/ubuntu/memory-v1-reviews/v5-specialized-unarchived2-preflight-20260716T010357Z.json`
with SHA-256
`bec708d70bb98d1aa961424d18001b047e98939814fa90f2374b6df56a457313`.
Postgres and Qdrant retained the same hashes above. This selection rejects an
external-call path; confirming the two unavailable packets requires a separate
authorization-bound version-2 selection.

## Authorized final two-case confirmation

Commit `9ccf76f6366c2294d16f67d2f898dca2d6a3c83c` bound the user's
fresh informed authorization to exactly `v5-04` and `v5-15`, the successful
two-case preflight, the revised case contract, and the unchanged owner-scoped
source manifest. All 142 offline tests passed before execution.

The one authorized `gpt-5.2`, `store=false` run made six calls: the three
specialized passes for each record and no repair calls. Both cases passed with
no findings. There were no refusals, incomplete responses, request errors,
deterministic rejections, or integrity errors. `v5-04` retained the supported
`occupation.works_as` observation while separately deferring the uncertain
credential transcription. `v5-15` produced the corrective
`identity.name_canonical` observation and its required correction semantics.

The mode-0600 report at
`/home/ubuntu/memory-v1-reviews/v5-specialized-unarchived2-authorized-live-20260716T014805Z.json`
has SHA-256
`ed12a63e9c4b7da0e792e31040414e0d8927340a244f0f5e60f00ba09d6da3c7`.
The zero-write proof passed: all 47 Postgres relations retained SHA-256
`437b38a8d85f771caec1eaf003356fde77775c26319bd463fc0f447fef67ddc4`,
and all six owner-filtered Qdrant points retained SHA-256
`60ff96fcc03e559d10a5a37144f145b5b1f699f1ae47d6e18b4bdcae8c8e4f87`.
No database relation changed. No staging, persistence, projection, retrieval
activation, or prompt integration occurred.

The hash-bound composite confirmation combines the 23 passing archived-packet
replays with the two passing fresh confirmations. It verifies non-overlap,
exact `v5-01` through `v5-25` coverage, the current case-contract hash, both
component report hashes, all per-case pass results, and identical component
Postgres/Qdrant signatures. It made no additional external calls. The
mode-0600 report at
`/home/ubuntu/memory-v1-reviews/v5-specialized-full25-composite-confirmation-20260716T015004Z.json`
has SHA-256
`46f6ad4476d3d287dd4835794acb2a2b2ceeed5e042e4008915e1657b2308c74`.
This establishes complete 25-case contract coverage while preserving which 23
results are deterministic archived replays and which two are fresh model
confirmations; it does not misrepresent the evidence as one new 25-record model
run.
