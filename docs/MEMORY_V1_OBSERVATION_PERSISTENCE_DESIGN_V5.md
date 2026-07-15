# Memory V1 observation persistence design V5

Status: proposed, specification-only, not runtime-active

Server: seebx backend

Companion registry: `specs/memory_v1_predicate_registry_v5.json`

This design does not authorize a migration, production write, extraction run,
promotion, Qdrant projection, retrieval activation, prompt injection, or
allowlist change.

## Decision

Create a dedicated `memory.observation` layer. Do not reuse or upgrade
`memory.candidate` as the canonical observation store.

The current candidate table is unsuitable because it is a mutable workflow
object:

- status moves through proposed, review, approved, rejected, and applied;
- review writes comparison and reason fields in place;
- apply attaches a durable claim ID in place;
- the runtime role currently has INSERT, UPDATE, and DELETE privileges;
- INSERT and UPDATE enqueue governance work;
- the uniqueness key describes a proposal, not one atomic source assertion.

Candidate remains hash-locked review/apply staging. Observation becomes the
immutable semantic record between evidence and claims.

## Canonical boundary

```text
immutable evidence
    -> untrusted V5 extraction packet
    -> server validation and owner-local entity resolution
    -> immutable atomic observation
    -> owner-scoped comparison
    -> mutable candidate review workflow
    -> governed claim plus claim revision and semantic relations
    -> policy, projection, retrieval, prompt trace
```

The model never supplies owner, evidence identity, durable entity IDs,
approval, project registration, or write permission. Unresolved entity
mentions and unregistered predicates remain deferrals; they do not create an
observation.

## Proposed relations

### `memory.observation`

One row records one evidence-backed atomic interpretation.

| Column | Contract |
|---|---|
| `observation_id` | UUID primary key |
| `owner_user_id` | authenticated Supabase UUID; server supplied |
| `evidence_id` | composite owner foreign key to active evidence |
| `subject_entity_id` | composite owner foreign key |
| `predicate` | foreign key to an extraction-enabled V5 registry row |
| `object_entity_id` / `object_literal` | exactly one; registry-validated |
| `polarity` | `affirmed` or `negated` |
| `modality` | V5 modality enum |
| `temporal_value_hash` | exact hash of the one-to-one typed temporal row |
| `source_char_start` / `source_char_end` | exact source span offsets |
| `source_span_sha256` | hash of the selected span; no copied span text |
| `projection_class` | one governed V5 class |
| `surface_policy` | registry-permitted policy only |
| `sensitivity` | at least the predicate floor and evidence sensitivity |
| `extraction_confidence` | confidence that the source was interpreted correctly; not truth confidence |
| `extractor` / `extractor_version` | bounded immutable identifiers |
| `packet_sha256` | hash of the validated normalized packet |
| `observation_sha256` | hash of the canonical owner-excluded semantic payload |
| `recorded_at` | database timestamp |

`memory.observation_temporal` holds semantic, shape, basis, source form,
certainty, precision, and exactly one compatible instant, calendar range,
instant range, relative offset, or recurrence value. Calendar values use
`daterange`; timezone-aware windows use `tstzrange`; both use canonical `[)`
bounds. Relative source form is not treated as precision. The complete contract
is `docs/MEMORY_V1_TEMPORAL_CONTRACT_V5.md`.

Required database constraints:

- `UNIQUE (owner_user_id, observation_id)`;
- `UNIQUE (owner_user_id, evidence_id, observation_sha256)` for replay;
- exactly one object form;
- valid and non-empty SHA-256 fields;
- valid source interval with `end > start`;
- temporal range and precision shape agreement;
- confidence between zero and one;
- composite owner foreign keys for evidence and both entity positions;
- `ON DELETE RESTRICT` for canonical provenance links.

The observation hash excludes mutable workflow state, database IDs, owner, and
timestamps. It includes subject identity, predicate, normalized object,
polarity, modality, temporal semantics, source span hash, projection class,
surface policy, and registry version.

Durable subject and object IDs come only from the server-generated,
owner-scoped resolution review defined in
`docs/MEMORY_V1_ENTITY_RESOLUTION_REVIEW_CONTRACT_V5.md`. The model packet no
longer contains durable entity IDs, model entity keys, or resolution actions.

### `memory.claim_observation`

This append-only link records which observations support, oppose, qualify, or
contextualize a claim. Its primary key is owner, claim, observation, and stance.
It does not replace claim revision history or semantic claim relations.

`memory.claim_evidence` remains during cutover for compatibility and can be
derived from the observation link. New V5 consolidation must not attach a
claim to evidence without an observation.

### `memory.candidate_observation`

This append-only link binds a mutable review candidate to the exact immutable
observations it proposes to consolidate. The candidate proposal hash covers
the sorted observation handles. Applying a candidate whose source set or hash
does not match fails closed.

### Lifecycle audit

Runtime observation semantics are insert-only. Normal review and
consolidation cannot update or delete an observation.

An append-only lifecycle audit records quarantine, redaction, and deletion
events. A separate no-login maintainer may invoke a narrow audited procedure
that:

1. locks the owner and observation;
2. verifies an authorization manifest and reason;
3. appends the lifecycle event;
4. suppresses all projections and pending work;
5. scrubs sensitive literals only when deletion law or user request requires
   erasure;
6. records before/after hashes without copying sensitive prose.

An inactive source evidence record makes every dependent observation
ineligible even before its own lifecycle event is processed.

## Security contract

- `memory.observation`, both link tables, and lifecycle audit use enabled and
  forced RLS.
- Every policy has owner equality in both `USING` and `WITH CHECK`.
- `brains_app` is non-owner, `NOBYPASSRLS`, and receives no UPDATE, DELETE,
  TRUNCATE, or direct lifecycle privileges.
- Prefer a `SECURITY INVOKER` insertion function over direct table INSERT. It
  sets no owner; it verifies the transaction-local actor, active evidence,
  entity ownership, registry version, object contract, sensitivity floor, and
  deterministic hash.
- Background jobs process one explicit owner per transaction and reset the
  actor on every transaction boundary.
- Global predicate rows are SELECT-only to runtime roles.
- Qdrant receives only approved claim/view projections. Observation and
  evidence text are never canonical in Qdrant.
- Projection workers re-read every record through Postgres owner enforcement.

Mandatory security tests include cross-owner evidence/entity/link inserts,
missing actor, stale actor, owner-switch on pooled connections, inactive
evidence, unregistered predicate, incompatible object type, sensitivity
downgrade, unauthorized lifecycle mutation, and Qdrant payload-owner mismatch.

## Candidate and claim semantics

Candidate remains disposable workflow state. Rejecting a candidate does not
delete its observations. Re-extraction creates no duplicate observation when
the source and canonical observation hash match.

Claims remain governed and revisable. Repetition adds observation/evidence
support but does not itself raise truth confidence. Correction, contradiction,
qualification, and supersession are claim-level review decisions with exact
observation provenance.

No scalar salience is written at extraction time. Later append-only signal
events retain importance, independence/frequency, recency, emotional
significance, goal relevance, future utility, retrieval/answer use, and
contradiction pressure separately.

## Implementation sequence requiring later authorization

1. Extend the global predicate table to the reviewed V5 registry contract.
2. Install observation and link tables on a production clone.
3. Add forced-RLS, privilege, replay, lifecycle, and composite-FK tests.
4. Add server normalization and deterministic observation hashing.
5. Re-run the same 25-record cohort with zero production writes.
6. Review extracted observations, entity resolution, temporal bounds, and
   sensitivity.
7. Install the schema in production after a fresh backup.
8. Keep main-account persistence and prompt influence disabled until separately
   authorized.
