# Memory V1 V5 bounded extraction

Status: clone-tested; not installed or enabled in production.

## Data path

1. Existing owner-scoped intake dispatch creates a relational extraction job.
2. The bounded worker claims one job under an explicit owner allowlist and lease.
3. The backend reads immutable evidence metadata and the current append-only
   thread/project binding.
4. The provider receives source content and source time only. It never receives
   owner, thread, project, evidence, or job identifiers. `store=false` and SDK
   `max_retries=0` are mandatory.
5. Server validation injects the trusted project key only when the project
   subject is either the matching named project or the anonymous
   `project:current_thread` placeholder.
6. One transaction writes the append-only normalized packet, a hash/count-only
   queue summary, and a hash/count-only queue event, then moves the job to
   `review_required`.

No candidate, claim, durable projection, Qdrant, retrieval, prompt, or frontend
write occurs in this phase.

## Security invariants

- Owner comes only from authenticated `app.user_id`; no owner argument exists
  in mutation functions.
- Binding and packet tables use forced RLS and composite owner foreign keys.
- Binding is explicit and append-only. Thread titles and text are never used to
  infer a project.
- A named project that conflicts with the bound project remains unresolved.
- Direct table mutation by `brains_app` is denied. Only narrow
  `SECURITY DEFINER` functions are executable.
- The worker requires separate apply and external-call capabilities, processes
  at most ten jobs, makes at most one provider call per job, and defaults to one
  job and one attempt.
- Operator output contains hashes, counts, routes, outcomes, and rejection codes
  only. Source text and extracted prose remain out of logs and queue events.

## Dry run

Server: seebx backend, isolated or production after schema installation.

```bash
cd /opt/chat-memory
set -a
source .env
set +a
PYTHONPATH=. venv/bin/python \
  scripts/memory_v1_v5_bounded_extraction_worker.py \
  --owner-user-id OWNER_UUID \
  --run-id NEW_UUID \
  --max-jobs 1
```

Dry run performs no claim, provider call, or database write.

## Activation boundary

Production installation, project binding, external extraction calls, a timer,
candidate generation, durable projection, retrieval, and prompt influence are
separate later phases. This branch stops before all of them.
