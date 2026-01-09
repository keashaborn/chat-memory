# Initiation Engine (seebx) — Job Taxonomy

## Definitions
Initiation: autonomous, budgeted background action started without a user prompt that produces a durable artifact and improves at least one drive (homeostasis, epistemic uncertainty, competence, alignment) while respecting safety gates.

Job: parameterized initiation template with trigger, artifact, metric, budget, and safety gate.

Artifact: durable output written to Postgres (fact field / job log) and/or Qdrant (embeddings), plus metrics.

## Drives (scalar state)
Homeostasis error: uptime/latency/cost/queue depth outside bounds.
Epistemic uncertainty: low-confidence facts, missing relations, contradiction count.
Competence gap: eval failures, tool reliability issues, slow plans.
Alignment error: mismatch with user goals/preferences/constraints.
Novelty rate: useful new info gained per unit cost.

## Job Families
1) Maintenance (homeostasis): health checks, integrity verification, index rebuild, regression alerts.
2) Excretion (waste): pruning, dedup, quarantine, retention enforcement.
3) Move (foraging): targeted acquisition, probing, coverage expansion.
4) Affiliate (coordination): user-model updates, feedback harvest, multi-agent reconciliation.
5) Create (world-model + skills): fact extraction, truth maintenance, rule induction, grounded prose generation, dataset creation.
6) Sleep (consolidation): offline consolidation, re-embedding, distillation, replay sets.
7) Immune (security): adversarial detection, provenance enforcement, anomaly detection.

## Invariants (non-negotiable)
- Facts are stored with provenance and time.
- Contradictions are first-class objects (not silently overwritten).
- Every initiation writes an artifact + metric delta.
- Budgets are enforced (tokens/time/cost).
- Unsafe tools/actions are gated or require explicit human approval.
