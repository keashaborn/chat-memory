# Memory V1 specialized-unit review — 2026-07-13

## Outcome

All 72 nonstandard review units from the hash-locked 164-row candidate-review
report were reviewed exactly once. The review is a no-write design artifact,
not candidate approval or persistence authorization.

| Decision | Units | Result |
|---|---:|---|
| Rewrite | 55 | Produce one canonical draft for later target-specific comparison |
| Split | 5 | Re-extract into two or more atomic review units |
| Defer | 7 | Re-extract with enough surrounding context to resolve the subject/reference |
| Reject | 5 | Retain immutable evidence only; do not produce a candidate from this span |

The 55 rewrites and five splits contain 66 canonical drafts. None has been
inserted into `memory.candidate`, `memory.user_preference`, a project store, or
Qdrant.

## Packet results

### Belief classification — 18

Twelve were rewritten as explicit user-held beliefs, five were rejected, and
one was deferred. Rejections include a volatile token estimate, an uncertain
tool name, context-only assent, an ambiguous grief comparison, and project
positioning misrouted as a personal claim. Philosophical beliefs remain user
viewpoints rather than external facts.

### Canonicalization — 21

Eighteen were rewritten and three were deferred. The accepted set contains ten
personal claims, three preferences, and five project statements. The deferred
project and personal fragments omit necessary subjects or scope boundaries.
The project statements remain project-only; the three preferences remain
preference-only.

### Manual decomposition — 3

Two mixed spans require new fact/opinion splits. One span describing a remembered
thought is a single autobiographical belief and was rewritten without splitting.

### Restricted health-domain beliefs — 8

All eight were rewritten as restricted user beliefs. They are never medical
facts, evidence of treatment effectiveness, or default advice. Their proposed
surface policy is `explicit_user_viewpoint_only_never_medical_advice`.

### Restricted relationship and health history — 4

All four were rewritten with named subjects resolved from the owner’s source
turn. Monika’s health history is limited to direct caregiving relevance. Kay’s
relationship history is limited to explicit personal-history recall. The affair
statement remains a user-reported, unverified third-party allegation.

### Subject and transcription resolution — 4

Both subject-reference units were resolved from surrounding owner-scoped source
text. One transcription became a Riverside Psychiatric recruitment statement.
The school statement was normalized to `Luxemburg-Casco High School`; spelling
was verified against the institution’s official website and recorded as
external normalization provenance.

### Uncertainty qualification — 14

Eight were rewritten, three were split, and three were deferred. Approximate
times, age gaps, employee/office counts, durations, and sale value remain
approximate. Pet-loss, DeeDee-loss, and retirement drafts require comparison
against existing governed records before any candidate is proposed.

## Corrections found by review

- Two source epistemic roles required correction from assertion/mixed wording to
  user belief/opinion.
- Five spans were not candidate-worthy despite reaching specialized review.
- Seven spans need expanded evidence because their atomic text omits the entity,
  employer, comparison, or scope.
- Five spans contain multiple claims and must be re-extracted rather than stored
  as compound prose.
- Project status statements need effective dates; newer project statements are
  not presumed authoritative or superior.
- Third-party health, relationship, and allegation content needs stricter surface
  policy than ordinary personal history.

## Security and mutation boundary

The validator reads two local JSON artifacts: the source dry-run report and the
review decision file. It verifies the source report SHA-256, owner, batch, plan
version, route, target, decision/action compatibility, canonical-draft count,
and exact 72/72 coverage. It has no database dependency, model call, Qdrant
client, apply argument, or mutation path.

The review authority remains
`codex_assisted_review_pending_user_ratification`. Ratification approves the
review classifications as design input only; it does not authorize extraction,
candidate insertion, or promotion.

## Next design input

The reviewed set now gives concrete requirements for the two missing stores:

- preference staging must support response versus life preference, silent versus
  direct surface policy, overlap comparison, evidence hashes, rewrite history,
  and explicit review state;
- project knowledge must support knowledge kind, project key, effective time,
  document/decision authority, status history, conflicts, supersession, and
  non-authoritative historical statements.

The 47 claim rewrites still require comparison against existing governed claims
before any claim candidate can be formed. The five split and seven deferred
units require a separate hash-locked re-extraction pass.
