# Memory V1 predicate registry V5

Status: proposed, specification-only, not runtime-active

Server: seebx backend

Machine contract: `specs/memory_v1_predicate_registry_v5.json`

Canonical sorted-JSON SHA-256:
`4d626433109c89c18d5ea374e173ca6785de6f9c20ecc05fef9f6447bfc671f4`

This phase freezes semantics only. It does not authorize a database migration,
production extraction, durable observation, candidate creation, promotion,
projection, retrieval, prompt influence, or allowlist change.

## Decision

The initial registry is deliberately closed and narrow:

- 25 extraction-enabled V5 predicates;
- 19 exact production predicates retained as read-only cutover compatibility;
- no model-created predicate or object type;
- unknown concepts return `defer_unregistered_predicate`;
- registry version is part of every observation hash.

The active set covers the relational extraction contract and all predicate
families required by the same 25-record evaluation cohort. It adds no generic
escape predicate such as `has_fact`, `relationship.kind`, or arbitrary JSON.

## Typed contract

Every active predicate fixes:

- allowed subject entity types;
- entity or literal object contract;
- cardinality and relation semantics;
- temporal semantics;
- epistemic modalities;
- projection classes;
- minimum sensitivity;
- permitted surface policies;
- deterministic manual-review rules.

Object contracts are named and reusable. Entity objects constrain entity type.
Literal objects constrain datatype, value shape, allowed units, and whether
approximation is permitted. `either` is not permitted.

Response preferences can only become `response_preference` and can only use
`zero_token_control_only`. Project predicates require an exact trusted project
scope. Health report predicates cannot use asserted modality, have a high
sensitivity floor, and require review.

## Legacy compatibility

The current production predicate rows remain readable during cutover so valid
existing claims do not disappear abruptly. V5 extraction cannot emit them.
Only exact, reviewed successor mappings are listed. An empty successor list
means the old claim stays readable until a later registry version defines a
safe atomization; it is not guessed into a new predicate.

Legacy compatibility is not permission to silently mix old raw-memory
retrieval with V5 governed retrieval. The existing fallback-exclusivity and
answer-trace requirements still apply.

## Database implementation target

The future migration should extend the existing global `memory.predicate`
registry rather than create a second authority. At minimum it needs registry
version, lifecycle, extraction permission, subject types, object contract,
cardinality, relation semantics, temporal semantics, modalities, projection
classes, sensitivity floor, surface policies, review rules, and a deterministic
configuration hash.

Runtime roles receive SELECT only. Registry changes are migration-owned,
transactional, conflict-checking, and auditable. Existing claim foreign keys
remain valid. A production-clone migration must prove all current predicate
rows are covered before any production install.

## Acceptance checks

- active and legacy names are unique and disjoint;
- the active set is exactly the reviewed 25-predicate set;
- all 19 current production predicates are covered;
- every predicate required by the 25 evaluation cases is active;
- object contracts are exact and never `either`;
- project, response-preference, health, sensitivity, and review invariants fail
  closed under mutation tests;
- owner, approval, write permission, scalar salience, importance, and truth
  confidence cannot appear in the registry;
- registry remains `runtime_active: false` until a separately authorized
  migration and shadow gate pass.
