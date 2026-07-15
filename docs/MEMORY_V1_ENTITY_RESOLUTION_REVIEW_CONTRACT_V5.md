# Memory V1 owner-scoped entity-resolution and review contract V5

Status: proposed, specification-only, not runtime-active

Server: seebx backend

Model packet schema: `specs/memory_v1_relational_extraction_v5.schema.json`

Trusted resolver schema:
`specs/memory_v1_entity_resolution_review_v5.schema.json`

Canonical sorted-JSON resolver schema SHA-256:
`b7b3d17056cbd10590743de9f183b49e5a4b1d62abba4926bd93aa6784574cb7`

Evaluation binding:
`evals/memory_v1_temporal_entity_resolution_v5_cases.jsonl`

Canonical sorted-JSON evaluation SHA-256:
`332783f8d293b607ae7b2a6006da40bf4f04778c94e6bc46fa24693099a70a4b`

This contract does not authorize a migration, entity creation, alias write,
merge, production resolution, observation persistence, promotion, projection,
retrieval, prompt influence, or allowlist change.

## Decision

Entity mention extraction and entity resolution are separate authorities.

The model may emit a packet-local mention with type, stated name text, role,
source spans, and extraction confidence. It cannot emit:

- owner or account identity;
- a durable entity ID or key;
- `link_existing`, `create_new`, merge, or approval decisions;
- a trusted project binding;
- an alias without evidence provenance;
- a cross-owner or global-person identity.

The previous V5 `resolution_action` and `existing_entity_id` model fields are
removed. Resolution is a server-generated, owner-scoped, hash-locked review
packet.

## Mention contract

Mention kinds are:

- `self_reference`: binds only through the trusted authenticated-owner self
  record;
- `named`: source explicitly supplies name text;
- `role_only`: source supplies a relationship role but no name;
- `anonymous`: source distinguishes an entity without a stable name or role.

`name_text` is evidence text, not a canonical name. A role phrase such as “my
father” cannot import a name from unrelated evidence during model extraction.

Source-local pronoun and coreference interpretation may connect packet-local
mentions. Durable owner-local linking happens only after extraction.

## Owner-local candidate generation

The resolver performs one owner per transaction:

1. set and verify the transaction-local authenticated Supabase UUID;
2. re-read active evidence through forced RLS;
3. hash and validate the mention and source spans;
4. normalize Unicode, case, whitespace, and punctuation under
   `memory_entity_normalization_v5`;
5. query only that owner's active entities and aliases;
6. require entity-type compatibility before comparison;
7. compute an inspectable feature vector for each candidate;
8. generate a hash-locked resolution proposal without writing an entity;
9. clear actor context at the transaction boundary.

Name similarity alone never crosses an owner boundary and never queries a
global personal-identity index.

## Feature vector, not one opaque score

Each candidate records:

- active status;
- entity-type match;
- exact canonical-name match;
- exact evidence-backed alias match;
- relationship-role support;
- source-local coreference support;
- owner-local graph-neighbor support;
- conflicting-attribute count;
- same-name candidate count;
- explicit exclusion reasons.

These features are not collapsed into a durable confidence score. Resolver
policy may rank candidates transiently, but the review packet preserves the
components and policy version.

Phonetic or embedding similarity may generate review candidates later. It
cannot auto-link a person, animal, or sensitive statement in V5.

## Allowed decisions

Resolution actions are `link_existing`, `create_new`, `defer`, and `reject`.

Auto-link is narrowly eligible only for:

1. the trusted self binding; or
2. one unique active owner-local candidate with matching type, exact canonical
   name or exact evidence-backed alias, no competing same-name candidate, no
   conflicting attributes, and no correction/sensitive-review rule.

Everything else requires review or defers. In particular:

- two entities with the same name remain distinct;
- a name-only match with different relationship context does not auto-link;
- third-party health, death, allegation, or intimate context requires review;
- correction targets require review and an exact prior entity/claim target;
- ambiguous transcription defers;
- an unresolved generic project reference cannot create a project entity;
- project entities resolve only through the trusted owner project registry or
  trusted thread binding.

New named entities may be proposed when no owner-local candidate exists and the
source explicitly supplies a stable name and type. Creation remains a separate
hash-locked apply decision.

Role-only and anonymous entities require schema support before creation. The
future entity table must distinguish `named`, `role_only`, and `anonymous`,
allow a nullable canonical name, retain a non-authoritative display label, and
use an opaque server-generated key. A fabricated name is forbidden.

## Review packet and transaction

The server-generated review packet contains source identity, registry and
normalization versions, mention hash, owner-local candidate IDs, feature
vectors, candidate-set hash, proposed action, review reasons, and decision
hash. Owner remains outside the model payload and is transaction-local.

Controlled apply requires:

- authorization manifest bound to owner, source hash, mention hash,
  candidate-set hash, resolver version, action, and decision hash;
- a fresh owner-scoped re-read under lock;
- an exact active candidate set;
- no newer resolution decision for that mention;
- all composite owner foreign keys;
- one transaction for entity, alias, observation links, audit event, and
  outbox effects;
- zero-write replay on the same manifest.

Candidate-set drift, inactive evidence, owner mismatch, entity status change,
or alias conflict fails the entire transaction.

## Durable entity and alias target

Entity keys are opaque server-generated identifiers and do not encode a name,
role, Vantage ID, thread, persona, or project phrase. Exactly one self entity
exists per owner and only trusted backend code may bind it.

Aliases are append-only provenance records with owner, entity, evidence, source
span, alias type, normalization version, and hash. Runtime roles do not receive
UPDATE or DELETE on entities or aliases. Redaction/deletion uses the audited
maintainer lifecycle.

The current `memory.entity` and `memory.entity_alias` tables have forced RLS and
composite owner keys, but are not yet sufficient because runtime has broad
mutation privileges, names are mandatory, alias lifecycle is not revisioned,
and current application code accepts model-shaped entity keys.

## Merge and correction

Entity merge is never an in-place destructive rewrite. A reviewed merge creates
an owner-scoped redirect/relation and immutable audit event, revalidates every
dependent claim, and rebuilds projections. The source entity remains available
for audit and rollback.

A name correction adds evidence-backed canonical-name and alias observations.
It does not silently rename every same-name entity. The prior name, correction
target, entity link, claim supersession, and review decision remain auditable.

## Security gate

Required production-clone tests include:

- candidate discovery cannot see another owner's entity or alias;
- a foreign entity UUID cannot be linked even if guessed;
- pooled connections cannot retain a prior actor;
- two owners may safely use identical normalized names;
- two same-owner entities may safely share a name without auto-merge;
- project scope cannot be model-generated;
- self cannot be model-created or rebound;
- correction, sensitive, fuzzy, role-only, and ambiguous matches cannot
  auto-link;
- replay is zero-write;
- Qdrant is never used as entity authority.

## Twenty-five-case gate

The companion fixture preserves the original 25 source identities without raw
conversation text. It requires:

- trusted self binding only;
- six distinct family mentions in the family case;
- three distinct pet entities plus self in the pet case;
- manual correction-target resolution in the name-correction case;
- unresolved project references to defer without project creation;
- ambiguous occupation/transcription material to defer;
- question-only cases to produce no entity-resolution writes.
