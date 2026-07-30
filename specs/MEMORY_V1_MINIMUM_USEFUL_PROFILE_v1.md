# Memory V1 Minimum Useful Profile v1

## Purpose

Make governed memory visibly useful before every extraction and consolidation
path is complete. This contract does not restore legacy cards or raw transcript
retrieval. It selects only supported, owner-scoped governed claims.

## Supported recall classes

### Self identity

Examples:

- `What is my name?`
- `Do you remember my name?`

Allowed predicate:

- `identity.name`

The entity scope is exactly the authenticated owner's trusted self entity.

### Broad self profile

Examples:

- `What do you know about me?`
- `Tell me what you remember about me.`

Closed predicate set:

- `identity.name`
- `occupation.works_as`
- `relationship.has_pet`
- `relationship.parent_of`
- `relationship.spouse_of`
- `stance.reported`

Broad profile recall deliberately excludes:

- death and loss claims;
- caregiving and health claims;
- raw evidence or transcript content;
- review-required, deferred, rejected, or staged records;
- project knowledge, which remains a separate governed lane;
- legacy Vantage cards and raw-vector memory.

## Security and authority

The authenticated actor UUID remains the memory owner. Entity scope requires
exactly one `trusted_owner_self` entity. Every relation must already be a
supported, owner-scoped graph edge. Qdrant proposes claim identifiers only;
PostgreSQL revalidation remains authoritative.

Technical and FM-conceptual turns retain their existing suppression rules.
Information-providing turns do not trigger retrieval.

## Budget

Specific self-name recall uses the normal claim budget. Broad self-profile
recall uses a conservative renderer-safe ceiling: at most four claim records
and 360 estimated claim tokens, still bounded by the global memory ceiling.
This limit may increase only after the estimator and exact rendered-token
accounting are reconciled.

## Initial data package

The first owner package should use existing supported claims wherever possible.
A missing self-name claim must be created from reviewed owner evidence and
linked to the trusted self entity. It must pass the existing controlled claim
materialization and projection path.

No claim is created merely because it appears in this specification.

## Acceptance

For each positive recall case:

- authenticated actor and owner IDs are identical;
- `memory_included` is true;
- `memory_record_count` is greater than zero;
- `memory_estimated_tokens` is greater than zero;
- final `memory_binding` is `bound`;
- selected records all belong to the authenticated owner;
- no legacy memory is present.

Negative cases must prove zero embeddings and zero memory access for technical,
FM-conceptual, unrelated, and information-providing turns.

## Release boundary

Code deployment and owner data application are separate operations. The code
may be deployed after unit, contract, and production-clone tests. Creating the
self-name claim, projecting it, or running live answer canaries requires an
explicit hash-locked production plan.
