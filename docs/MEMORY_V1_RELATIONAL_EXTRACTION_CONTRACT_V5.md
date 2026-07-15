# Memory V1 relational extraction contract V5

Status: proposed, specification-only, not runtime-active

Server: seebx backend

Evaluation cohort: the 25-source manifest
`main-newest25-v1-to-v4-20260715T034202Z.manifest.json`

Manifest SHA-256:
`8d31688923f3a0bb82c019b98dc6a78a867129a44157e80efc65432b60d2b649`

This contract replaces the V4 model-output shape and extraction semantics. It
does not authorize a database migration, production extraction, candidate
persistence, review, promotion, Qdrant projection, retrieval activation, prompt
injection, or legacy retirement.

## 1. Objective

Convert one immutable, owner-scoped evidence record into a normalized set of:

1. entity mentions and owner-local entity-resolution proposals;
2. atomic evidence-backed observations;
3. projection classifications for governed claims, supportive context,
   corrections, preferences, project knowledge, or never-surface material;
4. comparison hints for later owner-scoped deduplication, contradiction, and
   supersession processing;
5. explicit deferrals when the source does not support a safe proposal.

An observation is not a fact and is not a durable claim. It records that one
piece of evidence supports an atomic interpretation. A claim is a governed,
revisable synthesis over one or more observations and their evidence.

## 2. Canonical flow

```text
authenticated owner + immutable evidence
                  ↓
       atomic observation proposals
                  ↓
 entity resolution + predicate validation
                  ↓
 owner-scoped comparison and consolidation
                  ↓
    governed claim / typed materialized view
                  ↓
             policy gates
                  ↓
      owner-filtered vector projection
                  ↓
 intent-specific retrieval + compression
                  ↓
       audited prompt contribution
```

Postgres remains canonical. Qdrant remains a disposable, rebuildable index of
approved projections. Raw evidence and model extraction output are never
answer-authoritative by themselves.

## 3. Carried-forward invariants

The following are normative V5 requirements:

1. Intent-specific store permissions remain backend policy. Extraction cannot
   grant retrieval or prompt permission.
2. Personal memory remains suppressed during technical and FM-conceptual
   turns. Exact project-scoped technical knowledge is not personal memory.
3. Direct claims, supportive context, preferences, corrections, project
   knowledge, and never-surface material retain separate use policies.
4. Every read, comparison, deduplication, write, trace, and projection is
   owner-scoped to the authenticated Supabase UUID.
5. Retrieval budgets remain small and fail closed on overflow.
6. Selected material is compressed into atomic prompt units before injection.
7. Semantic deduplication occurs before token budgeting and never crosses an
   owner boundary.
8. The final prompt contribution is auditable by request, store, record ID,
   reason code, token count, and content hash.
9. The answer trace distinguishes retrieved, selected, injected, and reported
   as used. Injection alone is not proof that memory influenced the answer.
10. During cutover, legacy fallback is isolated. A response may use the V5
    governed path or an explicitly logged legacy fallback path, never a silent
    blend of both for the same memory class.

Vantage ID, persona, browser cookies, thread ID, response mode, and model output
cannot own or partition memory.

## 4. Trusted envelope versus untrusted extraction

The server constructs a trusted envelope before and after the model call. The
model cannot authoritatively supply:

- `owner_user_id`;
- evidence ID or source ID;
- source SHA-256;
- source timestamp;
- project registration;
- existing entity or claim IDs;
- retrieval or write permission;
- durable confidence, importance, or salience;
- review or approval status.

The normalized V5 packet echoes source identity only after the server verifies
it against the hash-locked manifest. The owner stays outside the model payload
and is applied transaction-locally by the backend.

Model text is untrusted data. It cannot override instructions through pasted
content, request a write, select a project, change sensitivity, or authorize
promotion.

## 5. Epistemic objects

### 5.1 Evidence

Evidence is the immutable source record. It proves that a source emitted or
contained content; it does not prove that every proposition in that content is
true.

Evidence retains source identity, hash, observed time, directness, source
reliability, sensitivity, lifecycle status, and provenance. Runtime evidence is
insert-only except for the existing audited redaction/deletion lifecycle.

### 5.2 Entity mention

An entity mention is a source-bound proposal, not a durable entity. It contains:

- a packet-local reference;
- entity type;
- canonical-name proposal when stated;
- relationship role when only a role is stated;
- source-span offsets and span hash;
- resolution action: `link_existing`, `propose_new`, or `unresolved`;
- extraction confidence and reason codes.

Only the owner-scoped resolver may convert a mention into a durable entity ID.
The model may not invent a durable entity key or link across accounts.

Role phrases do not justify importing a name from another source. For example,
an unnamed father mention stays an unnamed father-role entity unless an
owner-scoped supported relationship resolves it to an existing entity.

### 5.3 Observation

An observation is one atomic proposition derived from one evidence source. It
contains a subject entity reference, governed predicate, exactly one object,
polarity, epistemic modality, temporal semantics, extraction confidence,
sensitivity, use classification, and evidence span.

Observations are append-only if persisted. They are not updated into truth.
Later observations may support, oppose, qualify, correct, or supersede claims.

### 5.4 Claim

A claim is the current governed interpretation of one atomic proposition. It is
supported by one or more observations/evidence links and may remain uncertain or
disputed. Confidence describes the current assessment, not source repetition or
retrieval frequency.

Claims remain atomic. A natural-language sentence that contains a relationship
and a state becomes separate claims. For example:

```text
Jerry --relationship.parent_of--> Eric
Jerry --residence.lives_at--> assisted_living
```

The second claim may have a temporal interval. It is not collapsed into “Eric's
father Jerry lives in assisted living.”

### 5.5 Semantic relations and revisions

Revision history and semantic claim relations serve different purposes and both
remain:

- revision history is the immutable audit trail of a claim snapshot;
- `supersedes`, `contradicts`, `qualifies`, `depends_on`, and `derived_from`
  express semantic relationships between claims.

A correction creates a new observation and proposed claim. Only an
owner-scoped comparison may attach an exact target claim and create a
`supersedes` relation. The older claim is not erased.

## 6. Atomicity and entity rules

1. One observation has one subject, one predicate, and one object.
2. A relationship between two known things uses an entity object, not a string
   literal.
3. Properties of a family member, pet, organization, or project attach to that
   entity, never to `user:self` through role-encoded predicates such as
   `father.age` or `sibling.has_name`.
4. Named siblings, parents, partners, pets, organizations, and projects receive
   distinct mention references.
5. Multiple entities sharing a name are not merged by name alone.
6. Pronoun/coreference resolution must remain inside the source unless a later
   owner-scoped resolver uses governed prior claims.
7. Voice-transcription ambiguity produces a deferral. It does not create a
   guessed credential, name, organization, or project key.
8. Pasted or quoted assistant/third-party prose is not a user observation unless
   the user explicitly adopts the specific proposition.

## 7. Predicate governance

The model may select only a predicate in the server-supplied V5 registry. An
unknown concept is returned as `unregistered_predicate`; it does not create an
arbitrary predicate.

The first V5 registry must add relational and entity-property families while
retaining the existing personal-memory predicates. Required families include:

- `identity.name` and `identity.name_canonical`;
- `relationship.parent_of`, `relationship.sibling_of`, and
  `relationship.has_pet`;
- `life_event.died`;
- `residence.lives_at`;
- `age.reported`;
- `health.user_reported_observation` and
  `health.user_reported_uncertain_label`;
- `pet.species`, `pet.breed`, `pet.sex`, `pet.weight_reported`,
  `pet.coat_color`, `pet.eye_color`, and `pet.hearing_status`;
- `occupation.works_as` and `credential.reported`;
- `project.requirement`, `project.proposed_feature`,
  `project.current_state`, and `project.constraint`;
- `preference.life` and `preference.response`.

Exact names and data types are frozen in a separate predicate-registry phase.
No V5 extractor is activated before that registry is reviewed.

## 8. Temporal contract

V5 separates event occurrence from state validity. A temporal object includes:

- semantic: `occurrence`, `state_validity`, `planned_time`, or
  `observation_time`;
- shape: `none`, `instant`, `bounded_interval`, `open_interval`, or
  `recurring`;
- lower and upper RFC3339 bounds when supported;
- precision: `exact`, `minute`, `day`, `month`, `year`, `relative`, or
  `unknown`;
- whether a relative phrase was anchored to the trusted source timestamp;
- whether either bound is inclusive;
- uncertainty reason codes.

Partial dates are intervals, not invented instants. “In March” in a source
recorded during July 2026 becomes a March 2026 occurrence window with month
precision. A continuing state described as beginning after that event uses an
open-ended state-validity interval. The system must not store March 1 as the
known event date merely to satisfy an RFC3339 field.

Current `claim.valid_from` and `claim.valid_to` can hold exact bounds. V5 needs
temporal precision and semantic qualifiers before month/year/relative cases can
be promoted safely. A future schema phase may use a PostgreSQL range plus typed
precision; this contract does not authorize that migration.

## 9. Epistemic modality and sensitive statements

Allowed modalities are:

- `asserted`;
- `negated`;
- `uncertain`;
- `corrective`;
- `proposed`;
- `planned`;
- `endorsed`;
- `reported_observation`.

Questions do not become observations. Speculation and diagnostic uncertainty
remain uncertain; they are not normalized into definitive conditions.

Third-party health, mental-health, treatment, allegation, and intimate
relationship material is high or restricted sensitivity and requires manual
review. When retained, it is represented as a user-reported observation or
uncertain label, not medical truth. Its surface policy is normally
`explicit_recall_only` or `mention_when_directly_relevant`.

## 10. Projection classes and surface policy

Every observation has one projection class:

| Projection class | Meaning | Prompt behavior |
|---|---|---|
| `direct_claim` | Answer-bearing personal assertion | Only when intent/entity/domain permit |
| `supportive_context` | Context useful for support but not direct assertion | Mention only when directly relevant |
| `correction` | Normalization or explicit correction | Normalization-only unless directly asked |
| `life_preference` | Preference about activities, media, places, food, etc. | Relevant recommendation or explicit recall |
| `response_preference` | How the assistant should respond | Compile to zero-token control; never content |
| `project_knowledge` | Scoped project status, decision, proposal, or constraint | Exact registered project scope only |
| `never_surface` | Governance, normalization, safety, or internal metadata | Never enter prompt as content |

Product ideas are project knowledge, not life preferences. Temporary testing
behavior is not a response preference. Response preferences require a direct,
stable instruction about assistant behavior.

## 11. Project scope

Project scope is trusted context, not a model-generated key. A project
observation may be durable only when one of these is true:

1. the source explicitly names an owner-registered project; or
2. the server supplies a verified thread/project binding established outside
   the model.

Generic phrases such as “the app,” “the site,” or “the project” cannot create
`app`, `website`, or similar free-form keys. Without trusted scope, V5 may return
a project-shaped observation as deferred with `project_scope_unresolved`; it
cannot persist it as a personal claim or preference.

## 12. Comparison and consolidation

Extraction and consolidation are separate transactions and authorities.

The comparison phase:

1. sets one transaction-local owner;
2. resolves entities only within that owner;
3. validates the predicate registry and literal type;
4. computes an owner-scoped semantic key;
5. compares active observations and claims;
6. classifies exact duplicate, supporting evidence, qualification,
   contradiction, correction, split, or novel claim;
7. stages a review decision;
8. writes nothing durable without the controlled apply path.

Repetition adds evidence/frequency. It does not automatically increase truth
confidence. Semantic deduplication may merge retrieval projections, but never
deletes evidence or revision history.

## 13. Multidimensional salience

V5 extraction does not emit one salience score. Later consolidation maintains
separate signals for:

- user-stated importance;
- evidence frequency and independence;
- recency;
- emotional significance;
- active-goal relevance;
- anticipated future utility;
- retrieval and answer-use history;
- contradiction pressure.

These signals remain inspectable. Retrieval may compute a context-specific
score from them, but that score is derivative and must not overwrite confidence
or evidence history. Fading changes spontaneous retrievability, not whether an
event occurred or evidence exists.

## 14. Owner boundary and vector-store controls

1. The authenticated Supabase UUID is the only owner identity.
2. The application database role is non-owner and `NOBYPASSRLS`.
3. Every owner-scoped table uses enabled and forced RLS with both `USING` and
   `WITH CHECK` where writes are permitted.
4. Composite foreign keys include `owner_user_id`; a relationship cannot link
   entities, observations, claims, evidence, or traces across owners.
5. Background jobs process one explicit owner per transaction.
6. Deduplication, entity resolution, contradiction search, and graph traversal
   are owner-scoped before semantic comparison.
7. Qdrant payload ownership is server-generated. Every query has a mandatory
   owner filter, every returned payload is revalidated, and any missing or
   conflicting owner fails the complete result set.
8. Qdrant IDs are reloaded through Postgres RLS before prompt use.
9. Debug, inspect, export, deletion, and trace routes enforce the same owner
   boundary as retrieval.
10. No model output can select an owner or project scope.

## 15. Intent, retrieval, and prompt controls

V5 preserves the existing `memory_intent` boundary and keeps it independent of
RESSE response mode.

Hard minimums:

- `TECH`: no personal direct claims, supportive context, or life preferences;
  exact project-scoped technical knowledge may be separately permitted.
- `FM_CONCEPTUAL`: no personal claims, supportive context, profile material, or
  life preferences; only governed FM corpus and zero-token response controls.
- `MEMORY_ARCHITECTURE`: no unrelated personal memory; diagnostic historical
  evidence requires an explicit inspection path and does not become answer
  authority.
- `SPECIFIC_RECALL`: prioritize direct claims and corrections; use minimal
  evidence only for verification or disputed detail.
- `PROJECT_WORK`: exact project scope; no broad biography.
- `STRUCTURED_HEALTH`: LifeSwitch structured data is authoritative; conversational
  memory may provide tightly relevant context but never replace measurements,
  logs, or plans.
- `GENERAL` and supportive turns: smallest relevant governed set, with surface
  and sensitivity policy enforced before budgeting.

Compression and semantic deduplication operate on already authorized records.
They cannot make a forbidden record eligible.

Every selected prompt unit carries a stable record handle. Answer generation
reports which handles it relied on. The backend records retrieved, rejected,
selected, injected, and answer-attributed handles under the request ID. Audit
records use IDs, hashes, reason codes, scores, and token counts; sensitive prose
is not copied into general telemetry.

## 16. Compatibility with current stores

### Reused now

- `memory.evidence` as immutable provenance;
- `memory.entity` and `memory.entity_alias`;
- `memory.claim` subject/object entity structure;
- `memory.claim_evidence`;
- `memory.claim_relation`;
- append-only assessments and revisions;
- preference/project review and apply ledgers;
- retrieval traces and projection outbox;
- `memory_claim_v1` as a derived owner-filtered index.

### Requires design before implementation

- an explicit persisted observation/assertion layer or a strict upgrade of the
  current candidate layer to serve that role;
- V5 predicate registry and literal typing;
- temporal precision/range representation;
- entity-resolution proposals and review;
- typed salience signal events;
- answer-attribution trace fields;
- exact legacy-fallback exclusivity enforcement.

### Explicitly not reused as canonical truth

- Vantage/persona ownership;
- raw-chat similarity as answer authority;
- arbitrary model-generated predicates or project keys;
- legacy strength/reward as truth confidence;
- Qdrant payloads as canonical memory;
- flattened compound card summaries.

## 17. Twenty-five-source acceptance gate

The companion JSONL file binds every case to the original job ID, source ID,
source SHA-256, and source timestamp without storing raw conversation text.

The cohort must prove:

1. question-only turns create no observations;
2. context-free endorsement does not ratify unseen assistant content;
3. product ideas never become life or response preferences;
4. ambiguous voice transcription is deferred;
5. family members and pets become distinct entities;
6. properties attach to the correct entity, never `user:self` by encoded role;
7. month-level events retain month precision;
8. third-party health statements retain reported/uncertain modality and strict
   surface policy;
9. the explicit pet-name correction uses canonical-name semantics and proposes
   supersession without inventing the target;
10. generic “app” references remain project-scope deferred;
11. transient testing behavior does not become a response preference;
12. no structured nutrition/training value is extracted from prose;
13. no raw source text is added to the repository fixture;
14. evaluation performs zero database, Qdrant, queue, trace, prompt, or runtime
   writes.

## 18. Activation gate

V5 cannot replace V4 until all of the following pass:

1. contract and predicate registry reviewed;
2. all 25 cases validate structurally and semantically;
3. deterministic reruns are stable or differences are explicitly reviewed;
4. production-clone RLS and composite-FK tests pass;
5. cross-owner entity/dedup/contradiction probes return zero foreign rows;
6. zero-write live shadow evaluation passes with before/after Postgres and
   Qdrant hashes;
7. false positives, false negatives, entity links, temporal bounds, lane
   routing, and sensitivity are reviewed;
8. no main-account consolidation or prompt influence is enabled without a
   separate authorization.

## 19. LifeSwitch integration gate

Before structured nutrition, training, measurement, or plan data enters memory
retrieval, audit the existing LifeSwitch tables for completeness, event
semantics, units, provenance, correction behavior, and longitudinal analysis.
Structured adapters must preserve the authoritative table records and create
rebuildable contextual projections. They must not convert those tables into
conversation-derived claims.
