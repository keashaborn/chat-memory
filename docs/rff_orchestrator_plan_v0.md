# RFF Orchestration Plan v0 (RESSE execution, seebx control plane)

## Objective
Build a deterministic, restartable, policy-driven pipeline that expands and improves the Relational Fact Field (RFF) without manual babysitting. The current “math/hard-science subset” runs are not the goal; they are a test harness to harden the workflow so it can scale to the full 11.6M facts with minimal operator attention.

Non-goal (for now): prose generation. We are prioritizing the relational field quality (concepts, members, edges, cross-links) and the automation reliability.

## Current state (as of now)
Execution server: RESSE.
Single-iteration orchestrator exists: `scripts/run_field_iter_v0.sh`.
Main stages exist: PassX (expand clusters), PassC (LLM structuring), deterministic salvage, PassM, PassMprime, PassX_link, PassX_link_v1 (score/filter), PassG merge, `_current` pointer update.

Known failure modes observed:
- OpenAI API timeouts can abort PassC mid-run.
- “Resume” must not truncate ok/bad logs.
- Some schema-valid outputs fail stricter validators (`contradicts_not_explicit`, weak `depends_on`).
- Cross-link noise exists; scoring+filter is required.
- Orchestrator should never crash because of a few bad clusters; worst case is “mark bad and continue.”

## Ground rules / invariants
1. Determinism: routing decisions are based on explicit error codes + metrics, not a free-form agent conversation.
2. Idempotence: rerunning a stage with the same inputs either produces the same output or safely appends missing work (never corrupts).
3. Resume: every long stage must resume from artifacts on disk without operator intervention.
4. Budgeting: never spend unbounded effort salvaging a single bad cluster. “90% easy fruitful moves” is a hard policy.
5. No batch abort: a single cluster failure cannot abort the overall run.

## Directory conventions (existing)
- `runs/passX_mini_v0_YYYYMMDD_HHMMSS/` : PassX output (clusters per domain)
- `evals/passC_prompt_v0_YYYYMMDD_HHMMSS/` : PassC ok/bad logs + review
- `field/passM_*` / `field/passMprime_*` / `field/passXlink_*` / `field/passG_*` : stage outputs
- `_current` pointers:
  - `field/_current/MERGED` (base-of-truth field)
  - `field/_current/EXP_LATEST` (latest expansion)
  - `field/_current/LINKS_LATEST` (latest raw crosslinks)
  - `evals/_current/PASSC_LATEST` (latest PassC)

## Pipeline stages (data-plane workers)
Each stage is a worker with a strict IO contract. The orchestrator only coordinates.

### Stage A: PassX Expand (clusters)
Input: `field_dir = field/_current/MERGED`
Output: `runs/passX_mini_v0_*/clusters_by_domain_passX/*.jsonl`
Purpose: sample N concepts and produce per-domain clusters (seed + members).
Failure behavior: if PassX fails → iteration fails early (safe).

### Stage B: PassC Evaluate (LLM structuring)
Input: clusters dir, domains list, prompt+schema
Output: `evals/passC_prompt_v0_*/ok.jsonl`, `bad.jsonl`, `review.md`
Requirements:
- Retry/backoff on transient OpenAI/network failures.
- Never abort the entire run on transient timeouts; after budget exhaustion, mark cluster bad and continue.
- Resume mode must:
  - Load done cluster_ids from ok/bad,
  - Open ok/bad in append mode,
  - Skip already processed cluster_ids,
  - Never truncate existing logs.

### Stage C: Deterministic salvage (no model calls)
Purpose: cheap, deterministic corrections to avoid wasting retries:
- downgrade `contradicts` when not explicit -> `refines`
- downgrade `depends_on` when dependency language not present -> `refines`
- fix `support_i_list` endpoints to include src/dst and dedupe
- prune disconnected keep graphs (keep canonical component)

Policy: salvage is allowed if cheap and deterministic. No “9-step rescue”.

### Stage D: PassM materialize
Input: PassC eval dir
Output: `field/passM_v1_*`
Purpose: build concept nodes/members/edges from ok.jsonl (and any remaining acceptable rows).
Failure behavior: deterministic; if fails, treat as fatal for iteration.

### Stage E: PassMprime normalize
Input: passM dir
Output: `field/passMprime_*`
Purpose: normalize/merge aliases, edge normalization, etc.

### Stage F: PassX_link v0 (generate cross edges)
Input: PassX run dir + PassMprime expansion dir
Output: `field/passXlink_* / concept_edges_cross.jsonl`
Purpose: connect new expansion concepts into existing field.

### Stage G: PassX_link v1 (score/filter cross edges)
Input: merged field dir + cross edges
Output: `field/passXlink_v1_* / concept_edges_cross_scored.jsonl`
Purpose: drop low-quality crosslinks early; prefer precision.

### Stage H: PassG merge
Input: base field + expansion field + cross edges + scored cross edges
Output: `field/passG_*`
Purpose: produce new merged field with monotonic growth in quality.

### Stage I: Health and audits (gating + reporting)
Run (non-blocking) metrics:
- `field_health_report_v0.py`
- `passXlink_audit_v0.py`
Track per-run metrics for regression:
- ok_rate, bad_rate
- nodes/members/edges/seeds/aliases deltas
- rel_type distribution shifts
- crosslink keep/drop rate
- audit flagged rate

Gating policy (initial):
- If PassC ok_rate < threshold (e.g. 0.98), pause loop and alert.
- If crosslink keep_rate collapses, pause and adjust thresholds/prompts.

## Control plane vs data plane
### Data plane (RESSE)
Runs all heavy jobs and owns all artifacts.
Runs as:
- `tmux` sessions for interactive iteration
- eventually `systemd` service for unattended daemon

### Control plane (seebx)
Owns job requests, schedules, and UI.
Does not need full dataset locally.
Two implementation options:

Option 0 (fastest): seebx triggers RESSE via SSH
- Start job: `ssh RESSE "tmux new -d -s rff_loop '...'"`.
- Status: seebx reads `manifest.json`, counts, and tails `run.log` over SSH.

Option 1 (clean): Postgres-backed job queue on seebx
Tables:
- `rff_jobs(job_id, created_at, status, params_json, resse_host, run_tag, current_stage, metrics_json)`
- `rff_job_events(job_id, ts, level, message, payload_json)`

RESSE runs a small worker:
- Polls seebx `/jobs/next`
- Executes pipeline
- Posts heartbeats/progress
- Marks done/failed

## “90/10 rule” policy (do not oversalvage)
At full scale, the correct posture is:
- Preserve throughput.
- Quarantine failures.
- Spend limited budget salvaging only common, high-yield issues.

Per-cluster budget (recommended v0):
- 1 primary PassC call
- deterministic salvage
- 1 repair retry
- if still invalid → write bad record (with errors) and continue

Per-run budget (recommended v0):
- Allow “expensive lane” (higher tokens, extra retries, alternate model) for <= 1–5% of clusters.
- If quota hit, all remaining problematic clusters go directly to bad/quarantine.

Quarantine concept:
- Store bad clusters with provenance + error codes.
- Periodically sample and improve prompts/validators based on top failure modes.
- Do not block the main pipeline on quarantine backlog.

## Scaling to 11.6M facts: the key missing piece
We need a deterministic way to cover the full corpus without reprocessing:
- Define the unit of work: seed_fact_id (or seed_fact_uuid) -> cluster -> concept update
- Maintain a “todo” frontier and a “done” set
- Shard by hash range for parallelism
- Persist state in Postgres (preferred) or local sqlite/flatfiles (v0)

Until we have the global frontier, PassX sampling is useful for hardening but not a complete coverage strategy.

## Orchestrator layering plan
Layer 1 (today’s state): `run_field_iter_v0.sh` = single iteration
- Must be robust enough that it can run unattended and complete end-to-end.

Layer 2 (tomorrow): loop/daemon wrapper
Create: `scripts/run_field_loop_v0.sh` or `scripts/rff_orchestrator_v0.py`
Responsibilities:
- Compute next run_tag and params (n_concepts schedule)
- Launch `run_field_iter_v0.sh`
- On failure: capture logs + backoff + resume strategy
- Enforce “one active run” lock
- Write per-iteration manifest index (append-only)

Layer 3: seebx integration
- seebx provides job start/stop/status
- RESSE executes and reports

## Retention/archiving policy
Goal: keep working set small while preserving reproducibility.

Keep:
- `_current` targets + last K iterations (K ~ 3–5 initially)
Archive:
- Older `runs/`, `evals/`, `field/` dirs into `archives/YYYYMMDD/`
- Use `tar --zstd` (or `tar -czf`) and write an `archives/index.jsonl` referencing:
  - original dir name
  - sha256 of manifest
  - counts
  - created_at
Never delete without an archive record.

## Tomorrow’s implementation sequence (strict order)
1) Make PassC unkillable:
   - Resume must not truncate.
   - Transient API errors must not abort the run.
   - Provenance logging must be correct (fact_id + dup_group_id).
2) Make single-iter orchestrator fully end-to-end:
   - Confirm it uses scored cross edges in PassG merge.
   - Confirm manifest.json is written for merged output.
3) Add loop wrapper:
   - lockfile + backoff
   - iteration naming
   - stop/resume commands
4) Add minimal seebx trigger/status path (SSH-based first).
5) Only after stability: raise batch size / parallelize PassC.
