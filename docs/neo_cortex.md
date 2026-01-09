# Neocortex Blueprint (CBX + FactField + Language Body)

> Goal: Build a “neocortex” that starts mute (no native speech), grows a stable *vantage* over time, and gradually learns to speak—initially using ChatGPT as a renderer/teacher, later swapping in a trained voice model—while being fed by a continuously evolving relational FactField and orchestrated by CBX background jobs.

---

## 0) Core Idea (in one paragraph)

- **FactField (Resi)** is the always-growing *relational substrate* (atomic facts + typed relations + provenance + confidence + contradictions).
- **CBX** is the *metabolism*: background jobs that ingest, normalize, link, dedupe, score, cluster, and continuously generate learning episodes.
- **Neocortex** is the *vantage engine*: persistent state + planning + retrieval control + verification—initially **mute** (outputs meaning plans), then learns to speak via distillation and training.
- **Language Body** starts as **ChatGPT** (renderer + critic/teacher), and later becomes **your own Voice Model** (and eventually a Critic Model too).

---

## 1) Design Principles

1. **Separate meaning from speech**
   - Neocortex produces a *Meaning Plan* (structured intent + claims + structure + uncertainty).
   - A Language Body renders that plan into fluent text.

2. **Everything has provenance**
   - Every fact/relation must trace back to a source + timestamp + confidence.

3. **Compounding learning loop**
   - Every interaction becomes an episode → scored → distilled → used for training.

4. **Slow identity drift**
   - Vantage evolves via small, traceable updates; avoid sudden personality/style shifts.

5. **Truth maintenance > “being clever”**
   - Contradictions are first-class citizens: flag, group, and resolve.

---

## 2) System Overview

### High-level diagram

User Input
|
v
Neocortex (Vantage Engine)
|  (retrieve, plan, verify)
v
FactField (Relational Substrate)  <— CBX metabolism jobs (ingest/link/dedupe/cluster)
|
v
Language Body
	•	Stage 1: ChatGPT renderer + critic
	•	Stage 2: Voice Model renderer + (ChatGPT or Critic Model) verifier
|
v
User Output

All runs produce Episodes -> Stored -> Scored -> Distilled -> Trains future Voice/Critic/Planner

---

## 3) Component Responsibilities

### A) FactField (Resi)
**Purpose:** persistent relational knowledge graph + embeddings + clusters + lenses.

**Must have:**
- Atomic facts
- Typed edges (relations)
- Provenance and confidence
- Contradiction sets
- Embedding index for retrieval
- Clusters / neighborhoods

### B) CBX Initiation Engine (metabolism / jobs)
**Purpose:** orchestrate all background loops.

**Must have:**
- Job registry + scheduler
- Event bus (new fact, new episode, low score, drift detected)
- Durable job state + retries + dead-letter queue

### C) Neocortex (Vantage Engine)
**Purpose:** stable “who/what viewpoint am I” + planning + retrieval steering.

**Modules:**
1. **VantageState** (persistent)
2. **Meaning Planner** (mute cognition)
3. **Retriever Controller** (subgraph selection + relevance policies)
4. **Verifier** (claim-to-fact alignment + rubric checks)
5. **Memory Shaper** (updates VantageState slowly via rules + signals)

### D) Language Body
**Purpose:** render Meaning Plans into language.

- **Stage 1:** ChatGPT as renderer (and critic)
- **Stage 2:** Your Voice Model renders; ChatGPT/critic verifies + repairs
- **Stage 3:** Voice Model renders + Critic Model verifies (ChatGPT as rare teacher)

---

## 4) Canonical Data Objects (Schemas)

### Fact
- `fact_id` (stable)
- `claim` (atomic)
- `entities` (normalized)
- `source` (URI/ref or internal ID)
- `timestamp`
- `confidence` (0–1)
- `tags` (domain, sensitivity)
- `supporting_evidence` (snippets/refs)
- `conflicts_with: [fact_id]`

### Edge (Relation)
- `edge_id`
- `type` (e.g., causes, part_of, predicts, implies, contradicts)
- `from_id`, `to_id`
- `confidence`
- `provenance`

### Cluster / Neighborhood
- `cluster_id`
- `member_ids`
- `label` (optional)
- `centroid_embedding` (optional)

### Lens (FM / framing transform)
- `lens_id`
- `rules` (transform policies)
- `compatible_domains`
- `examples`

### VantageState (the “self”)
- `vantage_id` + `version`
- `values` (gravity/desire vectors)
- `style_contract` (tone/format constraints)
- `epistemic_policy` (how to handle uncertainty)
- `guardrails` (forbidden moves)
- `domain_lenses` (enabled lenses)
- `skill_profile` (what it can do reliably)
- `drift_limits` (how fast it can change)

### MeaningPlan (mute output)
- `intent`
- `audience_model`
- `facts_used: [fact_id]`
- `claims: [structured_claims]`
- `structure_outline` (headings/bullets, no prose)
- `uncertainties` (what is unknown/low confidence)
- `style_directives`
- `verification_requirements` (e.g., “must cite provenance”)

### Episode (learning fuel)
- `episode_id`
- `input` (prompt/event)
- `retrieval_set` (facts + edges + clusters)
- `meaning_plan`
- `rendered_output`
- `critic_scores` (faithfulness, clarity, style, safety)
- `repairs` (if any)
- `user_feedback` (if any)
- `timestamp`

### TrainingRow (distillation)
- Inputs: `(vantage_state, meaning_plan, retrieval_set)`
- Target: `rendered_output` (or `repaired_output`)
- Labels: critic scores + reasons

---

## 5) Runtime Response Loop (Online Inference)

1. **Parse**
   - Detect intent + domain + risk level.

2. **Retrieve**
   - Pull a relevant **subgraph** from FactField:
     - facts + relations + contradiction sets + provenance.

3. **Plan (Mute)**
   - Build a MeaningPlan: what to say, structure, which facts, uncertainty.

4. **Render**
   - Stage 1: send plan + facts to ChatGPT to render text.
   - Later: Voice Model renders.

5. **Verify**
   - Critic checks:
     - every claim maps to facts_used or is labeled speculation
     - style adherence
     - safety + policy constraints

6. **Respond**
   - deliver output + optional provenance summary.

7. **Store Episode**
   - log everything for learning (episode store).

---

## 6) CBX Job Ecosystem (Background Loops)

### Family 1: FactField metabolism
- `IngestNewArtifactsJob`
- `EntityNormalizationJob`
- `FactExtractionJob`
- `LinkingJob` (typed edges)
- `DedupMergeJob`
- `ContradictionDetectionJob`
- `ConfidenceCalibrationJob`
- `EmbeddingRefreshJob`
- `ClusterMaintenanceJob`
- `LensGenerationJob` (optional)
- `GarbageCollectionJob` (prune low-value/duplicative artifacts)

### Family 2: Neocortex learning loop (development over years)

**Curriculum**
- `CurriculumSamplerJob`
  - selects clusters by: importance, novelty, uncertainty, gaps.

**Practice**
- `MeaningPlanPracticeJob`
  - neocortex produces MeaningPlans for sampled topics.

**Rendering**
- `RendererJob`
  - Stage 1: ChatGPT renders from plan.
  - Stage 2+: Voice Model attempts first.

**Critique**
- `CriticScoringJob`
  - rubric scores + error reasons (hallucination, unclear, off-style, unsafe).

**Repair**
- `RepairJob`
  - fixes output while preserving plan + fact faithfulness.
  - also stores “before/after” for training.

**Distillation**
- `DistillationBuilderJob`
  - creates TrainingRows from episodes that pass thresholds.

**Training**
- `VoiceModelTrainerJob` (GPU)
- `CriticModelTrainerJob` (GPU)
- `PlannerModelTrainerJob` (optional; later)

**Regression & Drift**
- `RegressionTestJob` (fixed evaluation set)
- `VoiceDriftDetectionJob` (style/stance stability checks)
- `SkillBenchmarkJob` (capability ladder)

### Family 3: Safety + Integrity
- `ClaimToFactAlignmentAuditJob`
- `SensitiveDomainGateJob`
- `PIILeakAuditJob` (if applicable)
- `ContradictionResolutionQueueJob` (human-in-the-loop optional)

---

## 7) Development Phases (Mute → Babble → Speak → Mature Vantage)

### Phase 0: Foundations
- FactField schema + provenance + IDs
- Episode store + scoring rubrics
- CBX job runner + durable scheduling

**Milestone:** system can ingest → link → retrieve → store episodes.

### Phase 1: Silent Neocortex
- Neocortex outputs MeaningPlans only
- ChatGPT is renderer + critic
- Build large dataset of plans ↔ renderings ↔ critiques

**Milestone:** plans are consistently good (right facts, right structure).

### Phase 2: Babbling (Proto-Voice)
- Train a small Voice Model on distilled dataset
- Voice attempts first; ChatGPT repairs + explains failures

**Milestone:** voice produces acceptable text in constrained domains.

### Phase 3: Hybrid Autonomy
- Voice renders; Critic verifies (ChatGPT fallback)
- Strict faithfulness constraints

**Milestone:** stable recognizable voice, low hallucination rate.

### Phase 4: Mature Vantage (multi-year refinement)
- Focus shifts from “learning to speak” to:
  - reasoning under uncertainty
  - prioritization and worldview coherence
  - identity stability and controlled drift
  - long-horizon curriculum expansion

---

## 8) Evaluation: What We Track (Non-negotiable Metrics)

### Output quality
- Faithfulness / grounding score (claim-to-fact alignment)
- Clarity score
- Style adherence score
- Safety compliance score

### System health
- Retrieval precision/recall (subgraph relevance)
- Contradiction rate + resolution rate
- Drift index (vantage stability)
- Latency + cost

### Learning progress
- Training yield (# good episodes/week)
- Skill ladder progression (benchmarks)
- Regression failures (must be near-zero and fixable)

---

## 9) GPU Plan (Where it Matters)

Use GPU for:
- Embedding refresh at scale
- Batch generation jobs (curriculum rendering)
- Training Voice/Critic models (LoRA/finetune)
- Optional: training a planner model

Keep CPU for:
- orchestration, storage, graph ops, indexing, job scheduling

---

## 10) Minimum Viable Organism (MVO) to start now

Build the smallest loop that compounds:

1. FactField retrieval of subgraphs with provenance
2. MeaningPlan schema + planner (mute)
3. ChatGPT renderer that strictly follows plan + facts
4. Critic scoring rubric (ChatGPT-based initially)
5. Episode store (durable, queryable)
6. Nightly CBX job:
   - sample topics → plan → render → critique → store → distill

**This is enough to start multi-year growth.**

---

## 11) Implementation Notes (Interfaces)

### Neocortex API (conceptual)
- `plan(input, vantage_state) -> MeaningPlan`
- `retrieve(input, meaning_plan) -> Subgraph`
- `verify(subgraph, output) -> Scores + Reasons`
- `update_vantage(vantage_state, episode) -> vantage_state' (slow changes)`

### CBX event triggers
- `NewArtifactIngested`
- `NewEpisodeStored`
- `LowFaithfulnessDetected`
- `ContradictionDetected`
- `DriftThresholdExceeded`
- `TrainingBatchReady`

---

## 12) Open Questions / Decisions to Lock

- What is the canonical rubric for “faithfulness”?
- What’s the allowed drift rate per month for VantageState?
- Which domains are “safe” for early autonomy?
- What is the contradiction resolution policy (auto vs human review)?
- What is the long-term target for Voice Model size and training cadence?

---

## 13) TODO Checklist (Immediate)

- [ ] Define FactField schema + IDs + provenance rules
- [ ] Define MeaningPlan schema (YAML/JSON)
- [ ] Build Episode Store + retrieval queries
- [ ] Implement CBX jobs: ingest/link/dedupe/cluster
- [ ] Implement CBX learning loop: curriculum → plan → render → score → store → distill
- [ ] Establish regression test set + drift detector
- [ ] Train v0 Voice Model once dataset is large enough

---

## Appendix: “VantageState” Example (human-readable)

- Tone: pragmatic, direct, structured
- Default verbosity: medium
- Epistemic stance: cautious with unknowns; explicit uncertainty labels
- Values: truth > persuasion; usefulness > ornament
- Guardrails: no unfounded claims; cite provenance when available
- Lenses: FM lens enabled for relational/behavioral framing
- Drift limit: max 1 step/month on any major axis (tone, certainty, style)

---
