# Phase 2 disposable validation contract

The authoritative Phase 2 result is the single
`PHASE2_DISPOSABLE_RECEIPT=` JSON line emitted by
`tools/governed_memory_phase2/run_disposable_phase2.sh`. The receipt is not
checked into the candidate because it binds the candidate HEAD and tree; adding
that receipt to the same commit would change the tree it attests.

Run on the seebx backend only, from the clean candidate commit:

```bash
GM_PHASE2_DISPOSABLE_AUTHORIZATION='019fe927:DISPOSABLE_ONLY:NO_PRODUCTION_DATA' \
GM_PHASE2_EXPECTED_HEAD='<candidate-head>' \
GM_PHASE2_EXPECTED_TREE='<candidate-tree>' \
bash tools/governed_memory_phase2/run_disposable_phase2.sh full
```

The runner refuses any pre-existing Phase 2 resource or occupied disposable
port. It verifies the exact Git commit/tree and migration manifest, uses cached
pinned PostgreSQL and Qdrant images with no persistent store, and installs its
ID-bound cleanup trap before resource creation.

A passing receipt attests all of the following in one unchanged candidate run:

- forward, empty-only rollback, catalog absence, reapply, and equal normalized
  schema dumps for both databases;
- the complete synthetic Chat A to distinct Chat B integration, including cold
  worker reconstruction, explicit review, projection, PostgreSQL revalidation,
  answer binding, correction, retention purge, cold rebuild, alias swap,
  retraction, verified Qdrant absence, and hard deletion;
- `provider_external_calls=0` and `production_data_read=false`;
- exact container/network/image identities, released ports, and removal of all
  invocation-owned resources; and
- an unchanged clean candidate HEAD and tree at completion.

This is disposable database/vector proof, not production activation proof. It
does not attest Supabase identity propagation, installed HTTP routes,
authenticated frontend behavior, a real provider or embedding adapter, or a
calibrated semantic retrieval-score threshold.
