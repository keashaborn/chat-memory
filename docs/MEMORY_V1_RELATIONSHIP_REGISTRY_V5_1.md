# Memory V1 Relationship Registry V5.1

Status: proposed, offline only. This document does not activate extraction,
database persistence, retrieval, Qdrant projection, or prompt influence.

## Purpose

V5 initially governed only `relationship.parent_of`,
`relationship.sibling_of`, and `relationship.has_pet`. That was enough for the
original evaluation cohort but not for ordinary personal conversation. V5.1
defines a bounded relationship ontology before additional production
extraction is enabled.

The registry separates three things that natural language often collapses:

1. a structural connection, such as spouse, sibling, friend, or coworker;
2. a changing relational state, such as trust, tension, or estrangement;
3. an interaction event, such as an argument or reconciliation.

V5.1 governs the first two. Interaction events remain a separate future
contract and cannot silently become durable relationship states.

## Canonical graph model

Every stored observation remains owner-scoped and evidence-backed:

```text
owner-scoped entity
    -- governed relationship or social-state predicate -->
owner-scoped entity
    + source spans
    + modality and polarity
    + temporal validity
    + sensitivity and surface policy
    + perspective
```

Postgres remains authoritative. Qdrant may eventually suggest claim or entity
IDs, but it cannot create, reinterpret, or authorize a relationship.

Account-to-account LifeSwitch relationships and permissions are not evidence.
For example, a `training_partner` connection may permit sharing but must not
become a memory claim unless the owner also supplies eligible evidence.

## Relationship families

### Kinship and legal

The registry covers parent, sibling, spouse, romantic partner, grandparent,
aunt or uncle, cousin, guardian, in-law, and explicitly unspecified relative
connections.

Kinship is not inferred transitively. A known parent and sibling do not permit
the system to manufacture grandparent, cousin, aunt, or uncle edges. Those
edges require direct evidence or a later separately governed inference system.

`relationship.relative_of` is not an escape predicate. It is permitted only
when the source explicitly says that someone is a relative or family member
and does not establish a more precise relationship.

### Social connections

The registry covers friend, acquaintance, neighbor, roommate, and romantic
partner connections. These describe the owner's reported graph, not the other
person's private mental state. “Kelly is my friend” can support an owner-scoped
friend edge without asserting that Kelly independently made the same claim.

### Professional and support connections

The registry covers coworker, manager, mentor, coach, teacher, teammate,
collaborator, business partner, training partner, plan helper, caregiver, and
healthcare provider relationships.

Professional role language requires its relevant context. “Coach” in source
code, “support” in structural engineering, and “partner” in an unspecified
sentence do not establish interpersonal relationships.

Healthcare-provider relationships are restricted. They are not evidence of a
diagnosis, treatment, or clinical opinion. Professional clients and clinical
case relationships remain deferred pending a confidentiality-specific policy.

### Household and human-animal connections

The registry covers `lives_with`, `roommate_of`, and `has_pet`. Shared household
evidence must not be used to infer a precise address. A former or deceased pet
remains represented by `has_pet` plus temporal and life-event evidence rather
than a separate `former_pet` predicate.

### Relational states

The registry covers owner-reported closeness, trust, distrust, support,
dependency, tension, conflict, competition, avoidance, no contact,
estrangement, perceived adversary status, and feeling unsafe.

These are directed owner-perspective observations. They must not be promoted
as objective facts about another person. They use a fast retrieval-decay
profile while their evidence remains immutable. New evidence may close or
supersede their validity interval; it does not delete the earlier report.

## Direction and inverse normalization

Directional predicates have exactly one stored direction:

- parent to child;
- grandparent to grandchild;
- aunt or uncle to niece or nephew;
- guardian to guarded person;
- manager to managed person;
- mentor to mentored person;
- coach to coached person;
- teacher to student;
- caregiver to care recipient;
- healthcare provider to patient or client;
- helper to plan owner;
- supporter to supported person;
- dependent person to support person.

Inverse wording changes the edge direction, not the predicate. “My father is
Jerry” creates `Jerry --parent_of--> self`; “my son is David” creates
`self --parent_of--> David`. There is no separately stored `child_of` edge.

Symmetric predicates are stored once using a deterministic unordered entity
pair. The system must not write both A-to-B and B-to-A copies.

## Temporal rules

Relationship status is temporal data, not part of a predicate name:

- wife and husband normalize to `spouse_of`;
- ex-wife and former husband normalize to `spouse_of` with closed validity;
- late spouse normalizes to `spouse_of` plus independently governed death
  evidence;
- former friend normalizes to `friend_of` with closed validity;
- current tension is a dynamic state with open validity;
- “we reconciled” may close an earlier tension, avoidance, or conflict state,
  but is not itself proof of friendship or trust.

Parent, sibling, and other genealogical connections do not cease merely because
a person dies. Retrieval relevance may fade while the supported relationship
claim remains historically valid.

## Perspective and evidence rules

Structural connections use `owner_reported_connection`. Dynamic social states
use `owner_reported_state`.

By default, an eligible edge must include the owner's self entity. A directly
stated relationship between two third parties can be retained for review, but
cannot auto-promote. Third-party mental states remain deferred.

The following do not count as owner relationship evidence:

- questions without an assertion;
- assistant-generated statements copied without endorsement;
- song lyrics, book discussions, fictional characters, or role-play;
- programming identifiers or registry names;
- organizations, products, websites, games, or other non-person objects;
- metaphorical uses of friend, enemy, rival, partner, support, or relationship;
- one argument as proof of ongoing conflict;
- proximity as proof of friendship;
- LifeSwitch permissions as proof of a social relationship.

## Adversarial language

The registry intentionally forbids `relationship.enemy_of` and
`relationship.rival_of`.

“I consider Blake an enemy” may become the restricted, directed owner stance:

```text
self --social.perceives_as_adversary--> Blake
```

“Morgan and I are rivals in powerlifting” may become:

```text
self --social.competes_with--> Morgan
context retained in evidence: powerlifting
```

Neither predicate implies hatred, danger, dishonesty, or reciprocal feelings.
Feeling unsafe is also stored only as the owner's restricted reported state; it
is not automatically converted into an objective allegation.

## Retrieval policy

Stable structural connections may support direct recall and clearly relevant
answers. Dynamic social states require the person or relationship to be
explicit in the current turn. High-sensitivity states require additional
policy checks, and restricted states are limited to explicit owner recall.

Relationship retrieval must use small budgets and return atomic claims. It
must not inject an entire relationship history merely because a person's name
appears. The final prompt trace must identify every governed claim that
influenced the answer.

## Deferred scope

V5.1 deliberately defers:

- abuse, crime, harassment, and misconduct allegations;
- sexual relationship details;
- clinical client relationships and professional confidentiality;
- parasocial relationships with public figures;
- fictional and role-play relationships;
- third-party mental-state claims;
- interaction-event predicates such as arguments, reconciliation, betrayal,
  boundary setting, and support incidents.

Those areas need their own sensitivity, perspective, temporal, and retrieval
contracts rather than being forced through a generic relationship predicate.

## Offline validation and rollout sequence

1. Validate the proposed JSON registry and all synthetic cases offline.
2. Add source-role normalization and entailment guards without database writes.
3. Evaluate every predicate, inverse form, compound family sentence, temporal
   form, metaphor, question, quoted statement, technical statement, and
   fictional statement.
4. Create an additive predicate-registry migration and production-clone
   security suite.
5. Install schema contracts with backups and rollback-only tests.
6. Re-extract a bounded owner-scoped canary into immutable review packets.
7. Review entity resolution, temporal intervals, perspective, and false
   positives before staging.
8. Keep claims, Qdrant, retrieval, and prompt influence disabled until the
   reviewed canary passes.

## Separate future architecture item

Topic and project organization—music, programming, book writing, product
design, and other conversational domains—should be designed separately as
scoped components or topic contexts. It must not be conflated with the human
relationship ontology.
