# Memory V1 Epistemic, Pattern, and Salience Contract V5.1

Status: contract and additive schema production-clone verified; production
schema, workers, readers, retrieval, and prompt influence remain inactive.

Server boundary: seebx backend. Private GPU inference may propose semantic
groupings, but Postgres remains authoritative. This contract does not install a
schema, activate a worker, change retrieval, write Qdrant, or influence prompts.

## Core decision

Memory V1 does not store absolute truth. It stores:

```text
source evidence
  -> atomic observations
  -> owner-scoped entities and relationships
  -> assertions/claims
  -> supporting, opposing, qualifying, and corrective links
  -> append-only epistemic assessments
  -> governed projections
```

A claim is a proposition whose evidential position can strengthen, weaken,
become contested, or be superseded. It is never made permanently true by one
extraction, repetition, popularity, an embedding score, or an LLM judgment.

Direct self-report is strong evidence that the owner reported or endorsed
something. It is not automatically proof of an external-world proposition.
Structured measurements are strong evidence of the recorded measurement under
its instrument and source conditions. They remain revisable when calibration,
identity, transcription, or provenance changes.

## Separation of concerns

These values must never collapse into one field:

| Concern | Question |
|---|---|
| Extraction quality | Did the extractor interpret this source correctly? |
| Evidence support | How much eligible evidence supports the assertion? |
| Evidence opposition | How much eligible evidence opposes the assertion? |
| Epistemic state | Is the assertion insufficient, emerging, supported, contested, superseded, or retracted? |
| Pattern support | Do distinct occurrences support a recurring pattern? |
| Retrieval relevance | Would this memory help answer this turn? |
| Surface policy | May it influence style, content, explicit recall, or nothing? |
| Sensitivity | What handling and review rules apply? |

Extraction confidence is source-processing metadata. Salience cannot change an
assertion's evidential state. Retrieval use cannot make an assertion true.

## Evidence assessment

Every assessment is append-only and binds exact observation/evidence hashes.
The assessment retains at least these independent components:

- supporting observation count;
- opposing observation count;
- qualifying and corrective observation counts;
- independent support and opposition cluster counts;
- source diversity;
- directness;
- source reliability for the proposition's scope;
- relevance to the exact assertion;
- temporal fit;
- specificity;
- extraction quality;
- support strength;
- opposition strength.

There is no net truth score. Support and opposition remain visible at the same
time. The assessment state is a governed interpretation of the vector, not a
replacement for it.

Independence is mandatory. Repeated copies, assistant quotations, one imported
document repeated in multiple chats, and paraphrases within one conversational
episode do not become independent corroboration. Independence keys are
owner-scoped and stored as hashes when the underlying identifier is sensitive.

## Pattern hypotheses

Recurring observations can support a new proposition about recurrence. They do
not automatically strengthen an unrelated identity claim.

Example:

```text
week 1: "I felt blocked today."
week 3: "I felt blocked again this week."
week 6: "The same blocked feeling came back."
```

The eligible derived hypothesis is approximately:

```text
the owner has reported recurring episodes of feeling blocked
```

The ineligible shortcut is:

```text
the owner is a blocked person
```

The original episodes remain evidence-backed observations with their own time
semantics. The pattern is a separately governed hypothesis linked to those
observations.

### Pattern kinds

V5.1 defines these types:

- `recurrence`: materially similar observations recur in distinct episodes;
- `persistence`: a state is independently reaffirmed across time;
- `transition`: evidence indicates a prior state ended or changed;
- `trend`: ordered measurements or observations move in a consistent direction;
- `co_occurrence`: two phenomena recur in the same governed time windows;
- `sequence`: one phenomenon repeatedly precedes another.

Only recurrence, persistence, and explicit transition may become automatically
review-eligible in the first implementation. Trend must use structured data when
available. Co-occurrence and sequence remain manual-review hypotheses because
they are especially vulnerable to spurious inference and causal overreach.

### Default proposal rules

Predicate-specific policy may be stricter. The default inferred-recurrence
proposal requires:

- at least three eligible occurrences;
- at least two independent evidence clusters;
- at least two distinct temporal buckets;
- an adequate span for the predicate (seven days for transient personal states);
- matching owner, subject, predicate family, modality, and compatible scope;
- no unresolved entity binding, authorship, or temporal conflict.

One explicit owner statement such as "this keeps happening" is direct evidence
for a pattern assertion, but it does not fabricate the individual occurrences.
It creates an emerging hypothesis that may be reinforced by later observations.

Repeated wording in one thread counts as one episode unless the source contains
distinct event times. Structured nutrition/training/measurement recurrence is
derived from the authoritative application tables, not reconstructed from chat
prose when structured rows exist.

### Pattern lifecycle

Pattern state is one of:

- `insufficient`;
- `emerging`;
- `supported`;
- `contested`;
- `ended`;
- `superseded`;
- `retracted`.

New counterexamples increase opposition or mark an interval boundary. They do
not delete prior occurrences. Absence of recent occurrences lowers recency; it
does not erase the pattern's historical evidence.

## Multidimensional salience

V5.1 stores these dimensions separately on append-only feature snapshots:

1. `importance` — explicit or policy-derived significance independent of repetition;
2. `frequency` — recurrence across eligible independent episodes;
3. `recency` — time relevance under a predicate-specific half-life;
4. `emotional_significance` — explicitly evidenced personal significance, never guessed from dramatic wording alone;
5. `goal_relevance` — relation to an active owner goal;
6. `future_utility` — likely usefulness for a known future decision or event;
7. `retrieval_utility` — prior answer-use outcomes, not mere exposure;
8. `contradiction_pressure` — unresolved opposition, correction, or supersession pressure.

No universal scalar salience is canonical. A route-specific retrieval policy may
compute an ephemeral ranking score from these dimensions plus semantic match,
surface policy, sensitivity, temporal fit, and token budget. The score and its
weights are logged with the retrieval trace and discarded or retained only as
serving audit—not written back as epistemic state.

Different routes require different weighting. Specific recall may select an old
but exact event despite low recency. Supportive conversation may value emotional
significance and current relevance. High-stakes turns prioritize directness,
source reliability, contradiction pressure, and current structured data; they
must not treat emotional significance or repetition as factual support.

## Fading and forgetting

Fading means reduced retrieval priority, not evidence destruction.

- Recency may decay.
- Future utility may expire after a known event.
- Goal relevance may change when a goal closes.
- Retrieval utility may weaken when use repeatedly leads to correction.
- Evidence, observations, prior assessments, and historical pattern occurrences
  remain unless an audited redaction or deletion action applies.
- A stale supported claim can remain supported while rarely surfacing.
- New opposing evidence changes assessment; time alone does not make the claim
  false.

## Retrieval feedback

The system distinguishes exposure from outcome:

```text
eligible -> selected -> injected -> influenced -> explicitly useful/corrected
```

Selection or injection alone never increases evidence support. A user correction
after influence increases contradiction pressure and creates evidence for
reconciliation. An explicit confirmation can support the confirmed proposition
only when the confirmed content and source span are captured as new evidence.

The future outcome ledger records hashes and governed identifiers, not query or
prompt prose. It links owner, retrieval trace, target revision, outcome type,
and source evidence when an explicit confirmation/correction exists.

## Proposed storage model

All tables are owner-scoped, forced-RLS, append-only except narrow status/head
pointers whose history is preserved.

### `memory.epistemic_assessment_snapshot_v5_1`

One immutable evidence assessment for a claim, preference, project revision, or
pattern hypothesis. It stores component counts, independent-cluster counts,
dimension values, assessment state, method/version, input manifest hash, and an
optional superseded snapshot reference.

### `memory.pattern_hypothesis_v5_1`

Stable owner-scoped identity: pattern kind, subject, governed pattern key,
sensitivity, and current revision pointer. It contains no raw evidence text.

### `memory.pattern_hypothesis_revision_v5_1`

Append-only pattern definition and lifecycle state. A revision cannot remove or
rewrite prior occurrence links.

### `memory.pattern_observation_link_v5_1`

Links an exact observation and hash with role `occurrence`, `counterexample`,
`boundary`, or `context`, plus a hashed episode/independence key. Duplicate
observation or episode replay is rejected.

### `memory.salience_feature_snapshot_v5_1`

Append-only eight-dimensional feature vector for an exact target revision and
explicit `as_of_date` bucket. It includes raw signal counts/hashes and method
version but no canonical final score.

### `memory.retrieval_outcome_signal_v5_1`

Append-only outcome signal bound to an owner-scoped retrieval trace and exact
target revision. Allowed outcomes are `selected`, `injected`,
`explicitly_helpful`, `explicitly_confirmed`, `corrected`, `not_relevant`, and
`caused_confusion`. Only explicit confirmation/correction may link new evidence.

## Job design

The background process is a closed-loop proposal system:

```text
new applied observations
  -> deterministic episode/independence grouping
  -> pattern proposal
  -> evidence assessment snapshot
  -> review/apply gate
  -> salience feature snapshot
  -> route-specific retrieval ranking
  -> retrieval outcome signal
  -> later reassessment
```

Workers process one explicit owner per transaction. Private GPU inference may
suggest semantic grouping or classify a bounded hypothesis, but it cannot set
owner, durable IDs, assessment state, apply permission, retrieval eligibility,
or a salience vector. Backend policy recomputes all hashes and constraints.

Every job is lease-bound, replay-safe, input-hash locked, and writes an immutable
event. A worker never rewrites source evidence or observations.

Packet hashing is non-circular and deterministic. `packet_sha256` is the
SHA-256 of canonical JSON after removing the top-level `packet_sha256` field.
`input_manifest_sha256` binds the ordered, owner-scoped input revision and
signal hashes used to build the packet. The database recomputes both applicable
hashes before accepting a snapshot; a caller-supplied hash is never trusted.

## Security and isolation

- Owner identity comes only from the authenticated transaction-local actor.
- Every foreign key to evidence, observation, entity, claim, retrieval trace,
  target revision, and pattern includes `owner_user_id`.
- Background roles are `NOLOGIN`, `NOBYPASSRLS`, and receive only narrow
  function execution.
- No cross-owner pattern, independence cluster, retrieval outcome, or feature
  aggregation is permitted, including aggregate counts.
- Qdrant contains rebuildable approved projections only; it never decides
  pattern membership, evidence support, or owner scope.
- Every Qdrant result is revalidated through Postgres owner and revision state.
- High/restricted material cannot receive a weaker surface policy through
  pattern or salience derivation.

## Legacy boundary

The existing `salience_v1` worker computes one scalar from importance,
confirmation, and recency and updates `memory.claim.salience`. Its production
timer was audited disabled and inactive on 2026-07-20. It is legacy serving
logic and must not be enabled for V5.1 activation.

The legacy `confidence`, `importance`, and `salience` claim columns remain
readable during cutover. V5.1 extraction and projection packets may not set
them. The future serving adapter may derive temporary compatibility values from
the new snapshots until all readers use the multidimensional contract.

## Verified implementation checkpoint — 2026-07-20

The additive migration creates nine forced-RLS owner tables, seven closed enum
contracts, one restricted `NOLOGIN`/`NOBYPASSRLS` writer role, one exact packet
persistence API, and one retrieval-outcome API. `brains_app` receives function
execution only and has no direct table access. Pattern head and revision tables
are present in the additive design but deliberately have no application write
API until the controlled pattern review/apply phase is designed.

The PostgreSQL 16 production-schema clone passed migration replay, restricted
role and function ownership checks, canonical packet-hash recomputation,
bounded snapshot writes, zero-write replay, conflicting-request rejection,
missing-actor denial, cross-owner target denial, retrieval-outcome replay,
rollback-only fixtures, guarded empty rollback, and exact pre/post schema
comparison. No production schema, rows, Qdrant state, workers, retrieval, or
prompts changed.

Hash-locked executable artifacts:

- migration: `629a315135b0b0627f88944460adbaddb48efc7ec5e78681952333b2374dc20c`;
- rollback: `5343d67af7d7e8d784f776d9c8003aa7da4df21aa501de70c0b8496e9a2f9a6d`;
- adversarial SQL suite: `01a567399eddde4af41561fdb73c2dea50eb27fa6c08d5798a577d003b2ecfaa`.

## Required evaluation cases

The executable contract must cover at least:

- one transient state does not become identity or a pattern;
- repetition inside one episode does not inflate independence;
- recurrence across independent weeks creates an emerging hypothesis;
- copied/quoted sources remain one independence cluster;
- correction adds opposition without deleting evidence;
- stale support loses recency without losing support;
- retrieval exposure alone does not increase utility or support;
- explicit helpfulness changes utility only;
- explicit correction creates reconciliation evidence and contradiction pressure;
- emotional significance does not change evidence support;
- high-stakes ranking does not treat popularity or emotion as factual support;
- structured nutrition/training recurrence routes to structured aggregation;
- a newer project artifact does not automatically supersede an older one;
- nested and third-party patterns require owner/entity review;
- every cross-owner link and aggregation fails closed;
- no packet or snapshot contains a universal truth or salience score.

## Implementation sequence

1. Freeze the closed assessment/salience packet schema and evaluation cases.
2. Build a deterministic offline pattern policy and route-specific rank tests.
3. Design additive append-only tables and restricted writer functions.
4. Run production-schema-clone RLS, replay, rollback, and cross-owner tests.
5. Install schema only after a fresh backup; keep workers and readers disabled.
6. Shadow-generate pattern and feature snapshots for the admin owner.
7. Review false positives, especially identity, causality, and third-party edges.
8. Enable owner-bounded jobs without retrieval influence.
9. Activate route-specific retrieval experimentally with persisted traces.
10. Expand by owner only after isolation and usefulness evidence passes.
