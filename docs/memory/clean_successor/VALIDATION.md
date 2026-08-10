# Governed Memory successor disposable validation contract

The authoritative current result is the single
`SUCCESSOR_DISPOSABLE_RECEIPT=` JSON line emitted by
`tools/governed_memory_validation/run_disposable_successor.sh`. The receipt is not
checked into the candidate because it binds the candidate HEAD and tree; adding
that receipt to the same commit would change the tree it attests.

Run on the seebx backend only, from the clean candidate commit:

```bash
GM_VALIDATION_DISPOSABLE_AUTHORIZATION='019fe927:SUCCESSOR_DISPOSABLE_ONLY:NO_PRODUCTION_DATA:NO_PROVIDER_CALLS' \
GM_VALIDATION_RUNTIME_PYTHON='<exact source-bound candidate Python from runtime_manifest.json>' \
GM_VALIDATION_EXPECTED_ROOT='<absolute-candidate-worktree>' \
GM_VALIDATION_EXPECTED_BRANCH='<candidate-branch>' \
GM_VALIDATION_EXPECTED_HEAD='<candidate-head>' \
GM_VALIDATION_EXPECTED_TREE='<candidate-tree>' \
bash tools/governed_memory_validation/run_disposable_successor.sh full
```

The runner refuses any pre-existing successor resource and any occupied
disposable port. It rejects ambient Docker, Git, and Python authority
variables; binds Docker to the local `/var/run/docker.sock`; and verifies the
daemon identity. It verifies the exact Git commit/tree, migration manifest,
validation-runtime package manifest, checked-in runtime build receipt, canonical
package source-tree hash, and byte equality between every current and installed
successor Python source file. Cached pinned PostgreSQL and Qdrant
images use no persistent store, run on an `Internal=true` Docker network, and
publish no container ports. The integration process owns the two loopback TCP
relays. The ID-bound cleanup trap is installed before resource creation.

A passing receipt attests all of the following in one unchanged candidate run:

- forward, empty-only rollback, catalog absence, reapply, and equal normalized
  schema dumps for both databases;
- the complete synthetic Chat A to distinct Chat B integration, including cold
  worker reconstruction, locally signed Supabase-style JWT verification from
  an invocation-owned JWKS endpoint, authenticated HTTP review and lifecycle
  routes, projection, PostgreSQL revalidation, answer binding, correction,
  retention purge, cold rebuild, alias swap, retraction, verified Qdrant
  absence, and hard deletion;
- owner A/owner B HTTP and forced-RLS isolation, including malformed, expired,
  anonymous, wrong-audience, wrong-issuer, wrong-key, and forged-metadata JWTs;
- a nonempty `strace` connection audit restricted to exactly six network
  endpoints: four loopback application endpoints and two internal container
  endpoints. Exact local `/var/run/nscd/socket` attempts must fail with `ENOENT`;
  every trace byte is bound into the outer receipt SHA-256;
- `provider_external_calls=0`, `production_data_read=false`, and
  `production_endpoint_calls=0`;
- exact container/network/image/runtime/process identities, released ports, and
  removal of all invocation-owned resources; and
- an unchanged clean candidate HEAD and tree at completion.

This is disposable database/vector proof, not production activation proof. It
does not attest live Supabase session freshness or revocation, installed HTTP
routes, authenticated frontend behavior, a real provider or embedding adapter,
or a calibrated semantic retrieval-score threshold.
