# Memory V1 deferred reconciliation scanner V5

Status: dry-run only. It creates no queue, review, assessment, or claim write.

Server: seebx backend.

The controlled scanner evaluates only the authenticated owner and returns at
most 100 fresh reconciliation candidates. A claim is eligible only when it is
an active V5 claim and every active supporting observation has a current
`deferred/source_contradicts_predicate` entailment decision. Claims containing
accepted, unassessed, stale, inactive, or semantically unresolved support fail
closed.

Previously reconciled claim/decision pairs are excluded. Each result is
revalidated through `preflight_deferred_entailment_reconciliation_v5` and
includes current revision, support-state hash, review manifest, and
reconciliation manifest. The scanner cannot apply those manifests.

`brains_app` receives execute access only to the owner-scoped scanner. The
internal eligibility helper is not exposed. Retrieval and prompt influence
remain inactive.
