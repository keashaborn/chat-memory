# Memory V1 preference/project extraction dry run — 2026-07-13

## Scope

This phase converts the ratified specialized-review decisions into exact
preference and project candidate proposals. It does not insert candidates,
reviews, preferences, project records, Qdrant points, or prompt content.

The command has no apply option. It connects as `brains_app`, sets the exact
Supabase owner UUID in transaction-local `app.user_id`, and runs under a
repeatable-read, read-only transaction.

## Hash locks

- Evidence batch: `15064e5d-8cd3-5611-9cbf-db177d24a3a0`
- Specialized-review report SHA-256:
  `0ec58b9a3db4e18c6716da5b48b22307add381244bda8eaf619a87e99bc03841`
- Specialized-review input SHA-256:
  `ca6d7c12bb1d0605df96b413f13ab5820ab94ab6775f383c84d78262a966f16a`
- Extraction manifest SHA-256:
  `25d59e6f777afb4cf37057278876d58af9b11502bded6c52f0a546261d7b2cee`
- Final dry-run report SHA-256:
  `fce21afc06065b2b99c0fa11bde844da2eee158e21cd8ed90506c99345829728`

For each selected unit, the extractor verifies the review input lock, source
content hash, immutable evidence row, append-only batch-ledger hash, active
evidence status, owner, and batch membership. A changed review, changed
evidence row, changed mapping, missing schema control, or unexpected existing
target row fails closed.

## Proposed rows

The plan contains eight deterministic candidates and eight evidence links:

- Response preference: avoid surfacing Jerry or DeeDee memories outside direct
  relevance.
- Life preference: music with prominent vocals and ideally metaphorical lyrics.
- Life preference: songs with a concrete surface subject and a different
  underlying meaning.
- Project roadmap: Fractal Monism-informed memory weighting toward more
  human-like responses.
- Historical project status: AI/consciousness/feedback topics in dataset work.
- Historical project status: coexistence of the behavioral-learning memory
  system and newer cards on the source date.
- Historical project status: AI-assisted website development through VS Code.
- Proposed project requirement: an underlying AB-design intervention and
  evaluation system.

The two music proposals share `overlap_group=music.lyrical_meaning` and require
explicit overlap review before either becomes durable. They are not silently
merged during extraction. Two context-deficient project fragments remain
deferred and produce no proposal.

Project candidates target project key `verbal-sage`. Registration request ID
`5ba9533a-65f0-50f8-a62c-27efeb29fe92` is deterministic, but registration was
not executed. The database-generated project UUID will be resolved only inside
a later controlled persistence transaction.

## Security and database result

- All 18 specialized tables had enabled and forced RLS.
- The effective database role was `brains_app` in a read-only transaction.
- Candidate insert grants and controlled project registration were present.
- Direct review, durable preference, and durable project-head inserts were
  denied.
- All eight evidence and ledger hashes matched and all evidence was active.
- Existing preference candidates, project candidates, project space, reviews,
  durable preferences, and durable project heads were all zero.
- Before/after counts were identical: `173 evidence / 6 claims / 7 claim
  candidates / 0 preferences / 0 preference candidates / 0 project spaces /
  0 project candidates / 6 projection outbox`.

The full disposable PostgreSQL 16 Memory V1 suite passed, including the new
fail-closed test and guarded rollback. A second live dry run was byte-identical
to the first.

## Artifacts and stop condition

- Server report:
  `/home/ubuntu/memory-v1-reviews/MEMORY_V1_PREFERENCE_PROJECT_EXTRACTION_DRY_RUN_20260713.json`
- Report bytes: `20,526`
- Report mode: `600`

This phase stops before persistence. A later candidate-only transaction must
register the project space, insert exactly eight candidate rows and eight
evidence links, compare every stored value to this frozen plan, prove replay is
zero-write, and leave all reviews and durable records at zero.
