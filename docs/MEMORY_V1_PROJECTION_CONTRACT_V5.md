# Memory V1 Observation Projection Contract V5

Status: frozen contract plus clone-verified staging migration; runtime inactive.

Server boundary: seebx backend. This contract does not change Verbal Sage,
RESSE, Resse-Train, production PostgreSQL, Qdrant, timers, retrieval, or prompts.

## Decision

Evidence and immutable observations are the canonical record. Claims,
preferences, and project knowledge are governed materialized views over that
record. They are not three independent extraction pipelines and they are not
the source of truth.

The shared workflow is:

```text
evidence
  -> atomic observation
  -> owner-scoped entity binding
  -> trusted projection plan
  -> policy/review decision
  -> typed durable revision + typed observation links
  -> retrieval policy
  -> Qdrant projection/outbox
  -> prompt attribution
```

The projection planner is trusted backend code. It consumes only applied V5
observations and owner-scoped bindings. It does not reread raw chat, call an LLM
to invent durable IDs, or accept owner identity from a request packet.

## Canonical and derived state

| Layer | Authority | Mutability |
|---|---|---|
| Evidence | Exact source record and source hash | Append-only; audited redaction/deletion only |
| Observation | Atomic interpretation of evidence | Append-only; rejected/superseded by new records |
| Entity binding | Owner-scoped resolution decision | Append-only apply record |
| Projection plan | Proposed durable effect | Append-only, hash-locked |
| Claim/preference/project revision | Governed retrieval view | Append-only revisions with mutable head only where already required |
| Retrieval/vector projection | Rebuildable serving index | Disposable and convergent |

An observation is not declared true merely because it exists. Claim state is
derived from supporting and opposing observation links plus assessments.
Extraction confidence remains source-processing metadata. It is not truth,
salience, importance, or authorization.

## One workflow, three typed lanes

The common plan carries observation provenance, relational identity, target
action, temporal handling, review state, relations, and an owner-bound replay
manifest. A typed payload carries lane-specific state.

| V5 projection class | Durable lane | Required policy |
|---|---|---|
| `direct_claim` | claim | Registry surface policy |
| `supportive_context` | claim | Registry surface policy |
| `correction` | claim | Manual target review and `corrects`/`supersedes` relation |
| `never_surface` | claim | `never`; may not be weakened |
| `life_preference` | preference | Relevant recommendation or explicit recall only |
| `response_preference` | preference | Zero-token control only; never answer content |
| `project_knowledge` | project knowledge | Exact trusted project scope only |

A predicate may map to exactly one durable lane. The current 25-predicate V5
registry satisfies this rule. A record with an unknown predicate is deferred.

The contract deliberately rejects one generic polymorphic target table. Such a
table cannot enforce a real foreign key to three target types. The future
database uses one common plan/review/apply ledger and typed payload/target-link
tables.

## Projection packet boundary

`specs/memory_v1_projection_plan_v5.schema.json` is a closed schema for trusted
planner output. Every object rejects additional properties. The packet does
not contain `owner_user_id`; the transaction-local authenticated actor is the
only owner authority.

Each projection includes:

- One or more immutable observation IDs and hashes, each with a stance.
- Bound subject/object entity IDs or a canonical literal hash.
- Predicate, polarity, and modality.
- A server-recomputed semantic identity hash.
- A create/reinforce/revise/relate/defer/reject target action.
- An optimistic revision lock for every existing target.
- Observation-temporal handling without copied free-form dates.
- Review state and reason codes.
- Explicit contradiction/correction/supersession relations.
- Exactly one typed lane payload.

The packet forbids owner identity, scalar truth/confidence, scalar salience,
importance, frequency/recency scores, direct `valid_from`/`valid_to`, and
client-supplied approval flags.

## Semantic identity

The database must recompute the semantic key from canonical values; it must not
trust the packet hash. The canonical input is:

```json
{
  "identity_version": "memory_projection_identity_v5",
  "owner_user_id": "<transaction actor>",
  "lane": "claim|preference|project_knowledge",
  "subject_entity_id": "<owner-scoped entity>",
  "predicate": "<registered predicate>",
  "object_kind": "entity|literal",
  "object_entity_id": "<owner-scoped entity or null>",
  "object_literal_sha256": "<canonical typed literal hash or null>",
  "polarity": "affirmed|negated",
  "modality": "<V5 observation modality>",
  "lane_scope": "<typed scope below>"
}
```

Lane scope is empty for a claim. It is preference class/domain/key/scope for a
preference. It is project ID/kind/key for project knowledge.

Time is not part of semantic identity. Two observations about the same
proposition at different times can support one claim while retaining distinct
temporal provenance. Polarity and modality are part of identity: an asserted
claim, an uncertain report, a planned event, and a negated proposition do not
collapse into one record.

Multiple observations may reinforce one durable aggregate only when the
server-recomputed semantic key is identical. Similar prose or vector distance
is insufficient.

## Temporal rules

`memory.observation_temporal` remains authoritative. Claim and preference
projection uses `link_only`; no lossy timestamp pair is copied into the plan.

Project revisions may materialize an effective interval only when all of these
hold:

1. The lane is project knowledge.
2. A linked observation is named as the interval source.
3. Its temporal semantic is `state_validity`.
4. The typed range can be represented without increasing precision.
5. The apply function records the source observation ID.

Occurrence, planned time, observation time, relative offsets, recurrence, and
month/year precision may not be converted to generic `valid_from`/`valid_to`.
The future query layer derives effective temporal views from linked typed
observations.

## Claim lane

The relational proposition is separate from evidence state. Positive and
negated propositions receive distinct semantic identities. Contradiction is an
explicit relation between claims or an opposing observation stance; it is not
an overwrite.

Claim apply must:

1. Insert or lock the owner-scoped claim target.
2. Create an append-only revision when content or policy changes.
3. Insert `memory.claim_observation` rows with stance and relevance.
4. Create reviewed `claim_relation` rows for contradiction, correction,
   supersession, or qualification.
5. Record assessments separately from salience.
6. Enqueue a projection outbox event only after the durable transaction is
   valid.

The existing scalar confidence/importance/salience columns are legacy serving
fields. V5 does not accept values for them. A later migration may replace them
with derived views over independent assessment and salience dimensions.

## Preference lane

Life and response preferences remain separate classes over the same observation
substrate.

Life preferences may influence recommendations or explicit recall. Response
preferences may only produce structured, zero-token response controls. They
must never appear as factual answer content and must not supply user background.

The future apply path adds `preference_revision_observation`, keyed by owner,
revision, and observation. Existing evidence links remain readable during
cutover but V5 provenance requires the observation link.

## Project-knowledge lane

The planner cannot create or guess a project. `project_id` must resolve to an
active owner-scoped `memory.project_space` row before a plan is eligible.

Predicate-to-kind mapping is fixed:

| Predicate | Knowledge kind |
|---|---|
| `project.constraint` | `constraint` |
| `project.current_state` | `current_state` |
| `project.proposed_feature` | `proposed_feature` |
| `project.requirement` | `requirement` |

The future apply path adds `project_knowledge_revision_observation`. Project
supersession, conflicts, qualification, dependencies, and derivation remain
explicit relations. A newer document is not automatically more authoritative.

## Review and authorization

Automatic apply eligibility is narrow. It requires an active observation,
completed owner-scoped entity binding, registered predicate/class/policy,
identical server-recomputed semantic identity, no unresolved project, no
manual-review registry rule, no correction or relation mutation, and no
restricted policy escalation.

Manual authorization is required for:

- Named entity creation already gated by the V5 resolver.
- Corrections and any supersession/contradiction relation.
- Sensitive predicate rules from the registry.
- Ambiguous transcription or entity resolution.
- Revision of an existing target when its expected revision changed.
- Project state/authority changes that affect ratified knowledge.

Deferred and rejected plans are non-writing states and cannot request apply
authorization.

## Implemented staging database shape

The runtime-inactive staging migration adds these owner-scoped, forced-RLS
structures:

```text
projection_plan
projection_plan_item
projection_plan_observation
projection_plan_relation
projection_claim_payload
projection_preference_payload
projection_project_payload
projection_review
projection_apply_event
preference_revision_observation
project_knowledge_revision_observation
```

`claim_observation` already exists and remains the typed claim provenance link.
All owner-scoped foreign keys include `owner_user_id`; the global predicate
registry is the sole exception. The three payload tables have real foreign keys
to their lane-specific durable targets; no polymorphic target ID is accepted.

Indexes must lead with `owner_user_id`, including semantic identity, state,
observation lookup, project scope, and apply-manifest lookup. RLS is forced.
Policies target only the restricted writer role; `brains_app` receives no
direct table mutation grants.

## Owner-bound apply transaction

The apply function must execute as follows:

1. Derive actor from the authenticated transaction-local setting.
2. Load the plan by composite `(owner_user_id, plan_id)` key.
3. Lock the plan, review, observations, bindings, and existing target revision.
4. Revalidate active evidence and observation status.
5. Revalidate predicate, class, surface policy, modality, and object contract.
6. Recompute observation hashes, semantic identity, packet hash, and owner
   manifest.
7. Verify every entity, project, relation target, and prior revision belongs to
   the actor.
8. Apply exactly one typed lane mutation and its typed observation links.
9. Insert an append-only apply event containing the exact result IDs.
10. Enqueue the serving projection and commit.

The owner manifest is SHA-256 over this canonical server-side value:

```json
{
  "manifest_version": "memory_projection_owner_manifest_v5",
  "owner_user_id": "<transaction actor>",
  "packet_sha256": "<verified packet hash>"
}
```

Replaying the same owner manifest returns the recorded result with zero writes.
Using the same packet under another actor produces a different manifest and
semantic identity; composite foreign keys and forced RLS then reject all
foreign durable IDs.

## Serving projection

Qdrant is a rebuildable index, never the owner authority. Every point retains
`owner_user_id`, target ID, revision, status, sensitivity, and allowed intent
metadata. Retrieval must require an owner filter in the database and vector
query. Response preferences bypass answer-content retrieval and compile only
to structured response controls. Project knowledge requires exact project
scope.

Every answer trace must record which durable revisions and underlying
observations influenced the answer. A trace does not increase truth support;
retrieval history is a separate salience input.

## Existing structures and cutover

Reusable current structures:

- `memory.claim`, claim revisions/assessments/relations, and
  `memory.claim_observation`.
- `memory.user_preference` and append-only preference revisions.
- `memory.project_space`, project heads/revisions/relations.
- `memory.projection_outbox` and owner-filtered Qdrant serving.

Required changes before V5 activation:

- Install the clone-verified shared plan/review/apply ledger and typed lane
  payloads only after a separate production review.
- Install preference/project observation provenance links with that migration.
- Add controlled transactionally verified projection preflight/review/apply
  functions.
- Stop V4 from creating durable lane candidates directly.
- Revoke legacy direct durable-table mutation grants from `brains_app`.
- Keep older preference/project candidate tables readable only for controlled
  cutover and audit; do not feed them from V5.
- Prove two-owner isolation, stale-revision rejection, relation target
  isolation, and zero-write replay on a production-schema clone.

## Executable checks

`scripts/memory_v1_projection_v5_contract_test.py` verifies the closed schema,
all 25 registry predicates, deterministic lane routing, and a projection fixture
bound one-for-one to the existing 25 V5 source records.

`tests/test_memory_v1_projection_v5.py` contains adversarial packet tests for
owner-bound identity, cross-owner replay, response/life preference separation,
project scope, temporal non-collapse, revision locking, correction relations,
packet tampering, and forbidden scalar fields.

## Activation boundary

The staging migration and security suite are executable and pass on a disposable
PostgreSQL 16 production-schema clone. They are not installed in production and
start no worker. The next phase is controlled, transactionally verified
projection preflight/review/apply functions. Production installation, durable
apply, serving projection, retrieval, and prompts remain separately reviewed
boundaries.
