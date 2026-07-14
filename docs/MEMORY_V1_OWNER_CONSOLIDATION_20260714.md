# Memory V1 owner boundary and consolidation

Status: implementation complete in `memory_v1_owner_consolidation`; production
activation remains allowlisted and review-only.

## Ownership boundary

- The authenticated Supabase UUID is the only memory owner.
- `vantage_id`, persona names, browser cookies, aliases, `guest`, `debug`, and
  `anon` are not owners.
- PostgreSQL `chat_log.owner_user_id` and `threads.owner_user_id` are canonical
  UUIDs. New rows require matching compatibility `user_id` values; ownership
  and raw evidence fields are immutable.
- `chat_log` and `threads` are owned by `sage`, use forced RLS, and expose only
  rows matching `memory.current_actor_user_id()` to `brains_app`.
- Qdrant `memory_raw` payloads require matching `owner_user_id` and legacy
  `user_id`. Retrieval filters by the canonical owner and rejects the complete
  result set if any returned point lacks or conflicts with that owner.
- UUID-shaped legacy payloads can be hash-locked and backfilled. Named aliases
  remain quarantined and are not guessed into an account.

## Consolidation path

`public.chat_log` user turns enqueue one owner-scoped job:

`raw turn -> immutable evidence -> structured extraction -> review candidate`

Extraction routes candidates into three separate lanes:

- governed claim;
- response/life preference;
- project knowledge tied to an explicitly named, registered project.

Questions, transient commands, structured nutrition/training records, and
obvious pasted artifacts do not become ordinary claims. Pasted documents route
to artifact assessment. Project keys must match both the source text and the
configured project registry key.

The worker requires an explicit owner allowlist. It stages candidates for
review by default. Claim auto-apply requires both the command flag and server
setting and is restricted to explicit low/medium-sensitivity corrections that
pass the contradiction gate. The installed timer does not pass the command
flag, so automatic durable apply is disabled.

The worker stores a lease-verified extraction checkpoint before staging. A
post-model crash reuses the exact checkpoint; evidence and candidate writes are
idempotent. Queue events are append-only and forced-RLS protected.

## Deployment order

1. Stop `brains.service` to prevent writes during the owner-column transition.
2. Back up PostgreSQL, Qdrant `memory_raw`, code, and service definitions.
3. Install `20260714_raw_memory_owner_prepare.sql`.
4. Install `20260714_memory_v1_consolidation_queue.sql` while existing rows are
   still readable for deterministic queue backfill.
5. Generate and verify the Qdrant owner manifest; apply exactly that manifest;
   prove replay writes zero points.
6. Deploy application and worker code.
7. Install `20260714_raw_memory_owner_enforce.sql` to activate triggers and
   forced RLS.
8. Run SQL/unit/security tests, restart `brains.service`, and run health and
   two-account isolation probes.
9. Install but do not enable the timer until the synthetic account shadow run
   is reviewed. Initial allowlist: `557ea042-cb82-48f8-9429-472e96c957ef` only.

## Deletion behavior

Keep the synthetic secondary account as an isolation/regression fixture.
Existing raw delete endpoints refuse partial deletion when linked governed
records exist. A complete account-erasure workflow must transition evidence,
invalidate candidates/claims where required, remove raw/Qdrant source copies,
and retain only non-content audit tombstones. That workflow is not implemented
by the consolidation timer.

## Remaining activation work

- Review synthetic-account candidates and extraction precision.
- Add the controlled reviewer/promotion scheduler and consolidation/decay
  policy; do not equate lower salience with factual retraction.
- Add structured nutrition/training adapters rather than extracting those
  records from prose.
- Implement audited full-account erasure.
- After governed retrieval reaches parity, remove the remaining legacy raw/card
  prompt contributors and their frontend controls.
