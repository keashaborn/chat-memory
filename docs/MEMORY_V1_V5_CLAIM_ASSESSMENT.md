# Memory V1 V5 claim assessment and status transition

Status: production-clone proven. No production installation or claim mutation.

Server: seebx backend.

Projection materialization creates a `candidate`; it does not establish truth.
V5 assessment re-reads the current claim, projection event, observation hashes,
active evidence hashes, evidence stances, and temporal hashes before a reviewed
status transition.

Supported transitions are:

- `promote_supported`;
- `promote_uncertain`;
- `mark_disputed`;
- `quarantine`; and
- `retract`.

Review and apply are separate hash-locked transactions. Both are owner-scoped,
idempotent, and append-only. Apply updates the mutable claim head, then writes a
new `claim_assessment`, `claim_revision`, V5 apply event, and operation request.
It does not create a Qdrant or prompt dispatch.

The initial occupation candidate must be retracted, not promoted. Its source
states that the user has personal-training education but does not do personal
training for a living. The candidate text “The user works as a personal
trainer” overstates the source. The predicate registry currently lacks a
training/education predicate, so corrected projection remains deferred.
