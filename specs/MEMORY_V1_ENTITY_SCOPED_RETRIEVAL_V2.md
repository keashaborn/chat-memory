# Memory V1 Entity-Scoped Retrieval V2

Status: offline contract; production wiring disabled.

## Decision

The public prompt interface remains `MemorySelectionEnvelopeV1`. Entity scope
is an internal, server-resolved claim-selector boundary and does not become a
client field or a response-policy concern.

The trusted backend constructs `MemorySelectionRequestV1`, freezes its
`MemorySelectionRequestBindingV1`, resolves entity scope from owner-scoped
Postgres records, and binds both artifacts into
`MemoryClaimSelectorContextV2`. The V2 claim adapter applies the context after
Qdrant candidate discovery and after Postgres reload, but before ranking or
rendering.

`Qdrant -> candidate claim IDs -> owner-scoped Postgres reload -> predicate and entity-scope rejection -> rank -> MemorySelectionEnvelopeV1`

## Required read contract

The successor to `memory.read_governed_claims_v1(uuid[])` must additionally
return these typed fields from authoritative Postgres records:

- `subject_entity_id uuid not null`;
- `object_entity_id uuid null`;
- `subject_entity_type text not null`;
- `object_entity_type text null`.

The function remains `SECURITY DEFINER`, owned by the restricted reader role,
with an empty search path, owner equality derived from `app.user_id`, and no
caller-supplied owner parameter. It must never return a row whose owner differs
from the authenticated database session.

## Scope resolution

Only the authenticated backend may resolve scope. The client supplies no
entity UUIDs, relationship path, or scope mode.

The resolver produces:

- owner and selection trace binding;
- request-binding hash;
- closed mode;
- resolution-policy version;
- hashes of normalized entity hints, never raw query text in operational logs;
- exact predicate rules;
- exact allowed subject IDs per predicate;
- literal-only or exact allowed object IDs per predicate;
- canonical manifest hash.

Global subject and object allowlists are derived unions used for reconciliation;
they never replace the predicate-specific rules.

## Why predicate-specific rules are required

For a broad pet query:

- `identity.name` may allow only the resolved pet IDs as subjects;
- `pet.breed` may allow only the resolved pet IDs as subjects;
- `relationship.has_pet` may allow the self entity as subject and those pet IDs
  as objects.

A flat set containing self plus pet IDs would permit the user's own
`identity.name` record into a pet answer. Predicate-specific endpoint rules
prevent that error.

For family loss, `life_event.died` is allowed only on entities reached through
an authorized owner-scoped family relationship path. The system does not infer
that every death stored by the owner is a family death.

## Closed modes

- `self_profile`;
- `named_entity`;
- `family_profile`;
- `pet_profile`;
- `relationship_neighborhood`;
- `unscoped_predicate_only`, disabled until an explicit registry proof marks a
  predicate incapable of subject confusion.

## Fail-closed behavior

Selection rejects, rather than guesses, when:

- owner or request binding differs;
- a row lacks typed entity IDs;
- a predicate has no exact rule;
- a subject or object is outside its predicate rule;
- entity resolution is ambiguous;
- a relationship path crosses owner scope;
- a scope or selector-context manifest hash fails;
- unscoped mode lacks a registry proof.

The rejection code is `entity_scope`. It must be counted in sanitized selection
traces. No raw names, query text, claim prose, or prompt content are required in
the trace.

## Production wiring requirements

1. Add the governed claim read V2 database function and rollback/security
   suites.
2. Add a restricted owner-scoped entity resolver with exact relationship-path
   policies for each intent.
3. Add the V2 Postgres loader and V2 claim lane adapter.
4. Have `LiveGovernedMemoryAssemblyProviderV1` build the internal selector
   context while continuing to emit `MemorySelectionEnvelopeV1`.
5. Add `entity_scope` to the closed rejection registry and sanitized traces.
6. Run synthetic, production-clone, cross-owner, zero-write, and replay tests.
7. Keep runtime disabled until the coverage and entity suites pass together.

No legacy raw-memory or Vantage fallback is permitted.
