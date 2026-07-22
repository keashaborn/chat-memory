# Memory V1 V5.2 Semantic Coverage Contract

Status: accepted for private shadow extraction and append-only review staging;
no downstream staging, projection, retrieval, or prompt activation.

## Purpose

V5.1 proved owner isolation, provenance, relational extraction, controlled
projection, Qdrant revalidation, and final-answer binding. V5.2 closes the
coverage gap exposed after legacy Vantage memory was removed from prompt
influence.

The canonical flow remains:

`evidence -> atomic observation -> entity resolution -> assessment -> governed projection -> retrieval`

Postgres is authoritative. Qdrant may return owner-scoped candidate IDs only.
Every selected record is reloaded and revalidated in Postgres before prompt
influence.

## Memory lanes

### Governed claim

Evidence-supported assertions about entities and events. Claims preserve
polarity, modality, temporal validity, supporting and opposing evidence, and
supersession. A claim is never synonymous with absolute truth.

Coverage required for V5.2:

- identity: name and reported age;
- location: current and historical residence;
- work: occupation and employment history;
- education: school attendance and reported credentials or degrees;
- relationships: family, pets, friends, collaborators, caregiving, and social
  tension;
- life events: deaths and other entity-bound events;
- pet profile: species, breed, sex, appearance, hearing status, and reported
  weight;
- health: explicitly user-reported observations with high-stakes surface
  controls.

### Reported stance

An explicit opinion, belief, interpretation, or position expressed by the
user. This is not projected as an objective-world fact and is not a response
preference.

Required representation:

- subject: the user or another explicitly identified speaker;
- topic: normalized topic key plus bounded display text;
- position: compact faithful statement of the expressed position;
- orientation: supports, opposes, mixed, or uncertain;
- context: optional bounded context;
- temporal validity and source evidence;
- modality: `reported_belief`;
- projection class: `reported_stance`;
- surface policy: relevant recall or explicit recall, never silent style
  control.

Rendered memory must identify the statement as a reported position, for
example: `The user has expressed the view that ...`. It must not present the
position as independently established truth.

### Life preference

Stable likes, dislikes, preferences, and aversions. These may influence
recommendations when relevant. They do not control response formatting.

### Response preference

Explicit preferences about how the assistant should communicate. These are
zero-content controls or style-only prompt inputs and must never be used as
biographical facts.

### Project knowledge

Owner-scoped project decisions, requirements, constraints, proposed features,
and current state. Project knowledge requires an exact registered project and
component scope.

## Predicate additions

The following governed predicates are required in addition to the V5.1
registry:

- `education.attended`: person or self -> organization entity;
- `employment.worked_for`: person or self -> organization entity;
- `stance.reported`: person or self -> `literal.reported_stance`.

`credential.reported` remains the representation for explicitly reported
degrees, licenses, certifications, or credentials. `occupation.works_as`
remains temporally scoped and must preserve past versus current employment.

Deaths remain `life_event.died` on the person or animal that died. A user's
loss is derived for retrieval through an owner-scoped relationship path; the
system must not duplicate the same death as an unbound generic loss fact.

## Entity-scoped retrieval

Predicate filtering alone is insufficient because `identity.name` may apply to
the user, a parent, a pet, a project, or another entity. V5.2 requires a typed,
server-resolved entity scope in the selector request.

The scope must contain:

- a closed scope mode;
- allowed subject entity IDs;
- allowed object entity IDs when relationship records are requested;
- the server-side resolution policy version;
- a canonical resolution-manifest hash.

The client may not supply entity IDs or relationship paths. The authenticated
backend resolves them from owner-scoped Postgres records. The claim adapter
must reject rows outside the resolved scope after Qdrant discovery and before
ranking or rendering.

Required scope modes:

- `self_profile`;
- `named_entity`;
- `family_profile`;
- `pet_profile`;
- `relationship_neighborhood`;
- `unscoped_predicate_only` only for predicates whose contract cannot confuse
  subjects.

## Retrieval intents

Minimum V5.2 intent coverage:

- `identity_recall`;
- `profile_recall`;
- `occupation_recall`;
- `education_recall`;
- `family_recall`;
- `pet_recall`;
- `loss_recall`;
- `stance_recall`;
- `preference_recall`;
- `project_recall` and existing project status, planning, and decision intents.

Each intent has a closed lane list, predicate list, entity-scope policy,
sensitivity ceiling, record budget, and token budget. Broad recall is still
bounded and never means inject every known record.

## Extraction and projection gates

The V5.2 runtime profile may create only immutable, owner-scoped extraction
packets in `review_required` state. Existing V5/V5.1 relational staging,
entity-resolution, projection, Qdrant publication, and prompt paths must reject
V5.2 until their own V5.2 compatibility contracts pass clone testing.

Automatic progression is allowed only when all applicable conditions hold:

- owner equality is enforced in the database session and every persisted row;
- evidence is an active direct user statement or another policy-approved
  source;
- the atomic span is exact and provenance hashes reconcile;
- the predicate, object contract, modality, projection class, and surface
  policy are registered;
- entity resolution is exact or policy-authorized;
- entailment is high confidence;
- temporal language is preserved, including past-state wording;
- no unresolved correction, ambiguity, contradiction, or review flag exists;
- replay produces zero writes.

Anything failing these gates remains append-only evidence, a deferral, or a
review item. It is not silently discarded and does not influence prompts.

## Backlog processing acceptance

Before processing the full owner backlog:

1. Contract, registry, local provider, validators, projection dispatcher, and
   selector entity scope pass synthetic and production-clone tests.
2. Batch workers remain owner allowlisted, sequential per GPU, quota bounded,
   circuit-breaker protected, and replay safe.
3. No external model calls are made.
4. Extraction and review may write append-only owner-scoped artifacts, but no
   record influences prompts until governed projection and Qdrant publication
   both succeed.

The full backlog run must report counts by predicate, lane, disposition,
entity-resolution state, temporal state, contradiction state, projection
eligibility, and rejection code. Raw source text is not written to operational
logs.

## Non-goals

- Do not restore legacy Vantage cards, profile cards, raw-memory injection, or
  persona-owned memory.
- Do not make FM a memory owner, extractor, filter, fact source, or retrieval
  fallback.
- Do not collapse evidence, observations, claims, stances, preferences, and
  projects into one undifferentiated vector collection.
- Do not treat extraction confidence or model fluency as evidence of truth.
