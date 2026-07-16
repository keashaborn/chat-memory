# Memory V1 deferred-entailment claim reconciliation V5

Status: schema and production-clone testing first. Retrieval remains inactive.

Server: seebx backend.

The reconciliation path converts a governed, evidence-bound deferred
entailment decision into the existing governed claim-assessment review/apply
workflow. It does not directly delete a claim.

Automatic retraction is allowed only when:

- the selected decision belongs to the current actor and policy version;
- the decision is `deferred/source_contradicts_predicate`;
- its observation and evidence hashes remain current and evidence is active;
- the observation is linked to the claim with `stance=supports`;
- every active supporting observation has a current contradictory deferred
  decision; and
- the existing claim-assessment preflight permits the retraction.

If any accepted, unassessed, stale, or non-contradictory supporting observation
exists, reconciliation fails closed.

`claim_entailment_reconciliation_v5` is an append-only, force-RLS audit joining
the entailment decision, support-state hash, governed review, governed apply
event, resulting assessment, and claim revision. `brains_app` has no direct
table access. Exact function replay returns the existing audit with zero writes.

The initial application target is the incorrect occupation candidate supported
only by observation `9bf1e6b2-1840-4524-98dc-142567ebe013`.
