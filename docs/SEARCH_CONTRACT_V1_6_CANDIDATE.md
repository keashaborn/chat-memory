# Search Contract v1.6 candidate

This candidate is backend-only and is not deployed.

## Contract changes

- `search_decision_v1_6` reports one of four execution modes:
  `chat_only`, `indexed_sources`, `live_web`, or `unsupported`.
- Indexed source retrieval reports external source access truthfully while
  remaining distinct from live web search.
- Requests outside the installed packs become `no_search` with
  `unsupported_search_scope`; they do not advertise an executable search.
- Search and source budgets are bound in server request state. Browser JSON and
  headers cannot increase them. Route-specific caps may reduce a plan budget.
- Current-news citation repair shares the total plan search budget instead of
  receiving a new unbounded allowance.

## RLS candidate

`20260731_search_contract_v1_6_rls_hardening.sql` narrows the existing forced-RLS
policy on `trusted_web.response_transcript_v1` to `TO brains_app`, retains the
owner UUID predicate, and explicitly revokes client and public table grants.
The SQL is review-only until separately approved and applied.

Both forward and rollback SQL use bounded lock, statement, and idle-transaction
timeouts. They fail closed unless `brains_app` is non-privileged, owns the
target table, RLS is enabled and forced, no other role has table grants, and
the rollback sees the expected `brains_app`-only policy.

## Retention design requiring owner approval

No deletion job is included in v1.6.

- `trusted_web.request_rate_window`: proposed retention is 48 hours.
- `trusted_web.retrieval_audit`: proposed retention is 90 days. It contains a
  query hash, provider identifier, and source metadata, but not answer text.
- `trusted_web.source_cache`: proposed retention is 30 days after `expires_at`,
  except records placed on an explicit investigation hold.
- `trusted_web.response_transcript_v1`: keep aligned with the authoritative chat
  thread lifecycle. Existing foreign-key cascades remove rows when their bound
  chat rows are deleted. Do not add an independent time purge until product
  transcript retention is approved.

Before any retention job is built, approve durations, legal holds, batching,
monitoring, rollback evidence, and whether deletes must be archived.
