# Persona Shaping Policy System — Design (v0.1)

**Location:** /opt/chat-memory/docs/persona_design.md
**Applies to:** seebx (Brains + rag_engine), Verbal Sage (UI controls)
**Status:** Draft / planning spec (do not implement until Phase 0-1 sign-off)
**Primary goal:** Prevent compliance/subservience drift while enabling adaptive, shapeable personas (default assistant + role-play personas) using behavioral principles (SD/MO/operant classes) layered above any base LLM.

---

## 0) Problem statement and operating constraints

Most role-play assistants drift toward **increasing compliance and subservience** over time because:
1) Base LLM priors (RLHF) are approval-seeking and friction-avoiding.
2) Personal-memory retrieval and “style preference” loops amplify the most reinforced patterns.
3) In the absence of a controller, “agree + comply” is the shortest path to user continuation.

The system must prevent drift without hardcoding static personalities. The solution must be:
- **Inspectable** (debuggable decisions, scores, and state)
- **Stable** (homeostatic setpoints; bounded learning)
- **Modular** (policy logic outside the base LLM)
- **Multi-persona** (default assistant + role-play personas, isolated learning)
- **Buildable solo** (deterministic v1; ML later only if needed)

Non-goals for v1:
- Training new foundation models
- Perfect NLP understanding of SDs/MOs
- “Human-like” persona psychology; we want controlled behavior, not lore

---

## 1) Current system snapshot (as-of audit Contingencies Map v0)

Backend topology:
- Brains FastAPI service (app.py) on :8088
- rag_engine handles RAG + memory retrieval + overlay + gravity/vb_desire daemons
- Postgres authoritative transcript (chat_log)
- Qdrant memory_raw for episodic + cards + daemon cards; other Qdrant collections treated as corpus

Key current learning-like elements:
- `/rag/feedback` → `/memory_feedback` increments per-memory feedback counters + optional user_tags
- `persona_loader.quick_persona_refresh` writes deterministic `kind=style` card based on crude user feedback markers
- gravity + vb_desire daemons produce cards (currently non-deterministic UUIDs, ordering issues)

Important existing guardrails:
- personal memory retrieval excludes assistant chat points and excludes cards/daemons
- gravity “escape hint” prioritizes explicit request under misalignment

Critical design constraint:
- `unified_retrieve()` currently searches *all Qdrant collections except memory_raw*; future internal vector collections (SD prototypes, exemplars) must not accidentally become corpus.

---

## 2) Design principles

### 2.1 Single policy engine, multiple personas
The “default Resse assistant” and role-play personas use the same policy machinery. They differ only in:
- slider setpoints (R/P/L/B)
- optional role overlay (character sheet / scenario framing)
- isolated learned offsets and stance commitments per persona_id

### 2.2 Homeostatic anti-drift
UI sliders are **setpoints**, not tunable weights that learning can overwrite.

- `θ_base = h(sliders)`
- `θ_effective = θ_base + δ`
- `δ ← clamp((1−λ)·δ + Δlearn, [-δ_max, +δ_max])`

This ensures drift cannot run away and decays back toward the user-chosen persona.

### 2.3 Separation of concerns (no “prompt soup”)
We explicitly separate:
- **Semantic content**: RAG facts + plan content
- **Functional analysis**: SD features + MO dynamics + decision policy
- **Topography**: renderer changes how it’s said, not what is decided

### 2.4 Stance changes are rare and gated
Stance changes only happen under a persuasion gate driven by **argument quality**, not by authority pressure, coercion, or repeated demands.

### 2.5 Reinforcement is routed by channel
User consequence signals can update:
- **Style/topography (CS/L)** normally
- **Compliance/deference susceptibility (ARP/R)** only in bounded ways; dominant personas can damp or invert this channel
- **Stance (PS/P)** only via persuasion gate; never via generic praise/correction

---

## 3) Core terminology (behavioral control layer)

- **SD features**: deterministic features extracted from user text + context (authority pressure, coercion, negotiation language, argument quality, respect/disrespect, threat, goal clarity, etc.)
- **MO vector**: bounded leaky integrators that modulate response selection (resistance, openness, affiliation, task_drive, threat_arousal, etc.)
- **Operant classes**: response classes the system can select (COMPLY, NEGOTIATE, REFUSE, CLARIFY, REDIRECT)
- **Governor**: selects operant class + stance gating + constraints
- **Renderer**: enforces verbal topography consistent with decision object (directness/warmth/hedging) without changing plan content
- **Critic selection**: generates 2–3 candidates, scores them, selects best; provides internal differential reinforcement
- **Contingency events**: structured signals extracted from user behavior and conversation outcomes (praise, correction, escalation, negotiation acceptance, disengagement, etc.)

---

## 4) Data model (v1)

### 4.1 PersonaDefinition (user-controlled, stable)
Stored as a memory card in Qdrant `memory_raw` (source=memory_card), deterministic ID.

Fields:
- `user_id`
- `persona_id` (string; required for multi-persona isolation)
- `name`, `description`
- `sliders` (R, P, L, B setpoints; normalized 0..1)
- `role_overlay` (optional; character sheet / scenario framing)
- `policy_version` (string)
- `created_at`, `updated_at`

### 4.2 PersonaState (learned, bounded, decaying)
Stored as a memory card, deterministic ID per (user_id, persona_id).

Fields:
- `user_id`, `persona_id`
- `setpoints_snapshot` (copy of sliders at last update)
- `learned_offsets`:
  - `delta_R`, `delta_P`, `delta_L`, `delta_B` (each clamped)
  - decay config: `lambda`, `delta_max`
- `mo` vector (bounded -1..+1)
- `stance_commitments` summary:
  - list of {topic, position, strength, last_changed_at, rationale_summary}
- `stats`:
  - counters for event types, last_update_at
- `policy_version`, `schema_version`

### 4.3 SD prototypes (optional v1.5; recommended)
If implemented, store in a separate Qdrant collection `sd_prototypes`:
- payload: {class, text, version, notes}
- used only for SD feature generalization
**Must never be included as corpus** (requires corpus allowlist/denylist first).

### 4.4 ContingencyEvent (structured log)
Initial v1 can store events as append-only Postgres table or Qdrant points. For minimal churn, v1 can store:
- aggregated counters inside PersonaState
- optional event “recent window” list (last N events) as structured objects (no raw transcripts)

Event schema (example):
- `event_type`: PRAISE | CORRECTION | ESCALATION | NEGOTIATION_ACCEPT | NEGOTIATION_REJECT | DISENGAGE | CHALLENGE | TOPIC_SHIFT
- `signals`: {valence, intensity, markers[]}
- `turn_id`, `thread_id`, `timestamp`
- `persona_id`

---

## 5) Runtime policy loop (target behavior)

At `/rag/query` time (seebx):
1) Retrieve content (existing): personal memory + corpus + overlay + gravity/vb_desire
2) Compute SD features: `f(user_text, context) -> features`
3) Update MO: `m(t+1) = g(m(t), features, outcome_proxy)`
4) Governor: `Decision = π(state, features, mo)`
5) Candidate generation: produce 2–3 candidate responses consistent with Decision
6) Critic scoring: score candidates with weights derived from (R,P,L,B) and MO; choose best
7) Renderer: final surface realization; no decision changes
8) Persist: update PersonaState (bounded offsets + MO), store debug payload (dev-only), store any events

At `/rag/feedback` time:
- feedback currently reinforces memory points; v1 policy learning should not depend on thumbs
- later: map feedback + follow-up language into ContingencyEvents that update deltas (bounded)

---

## 6) SD feature extractor v1

### 6.1 Feature set (v1 minimal)
Start with ~8–12 features:
- `authority_pressure` (commands, rank claims, “do it now”)
- `coercion` (“or else”, punishment conditions)
- `threat` (violence/intimidation; separate from coercion)
- `respect_level` (insults vs neutral vs respectful)
- `negotiation_language` (tradeoffs, proposals, conditions)
- `argument_quality` (claim→reason→evidence cues; specificity; counterargument handling)
- `goal_clarity` (deliverables/constraints/success criteria)
- `safety_risk` (policy triggers; always overrides)
Optional: `consent_clarity` (only if role-play includes intimacy/authority dynamics; must be handled safely)

### 6.2 Determinism requirement
`f()` must be deterministic, unit-tested, and versioned:
- lexical patterns + prototype similarity (if enabled)
- no LLM calls in v1 extractor

### 6.3 Prototype similarity (recommended)
Store prototypes per class; at runtime:
- embed input once
- retrieve top-k per class from `sd_prototypes`
- map max similarity to feature value
This provides paraphrase generalization with stable behavior (embedder version pinned).

---

## 7) MO dynamics v1 (bounded leaky integrators)

Define MO dimensions (v1):
- `resistance` (increases under authority_pressure/coercion)
- `openness` (increases under argument_quality + respectful negotiation)
- `affiliation` (increases under respect; decreases under insults)
- `task_drive` (baseline; increases under clear goal)
- `threat_arousal` (increases under threat/coercion; drives refusal/redirect)

Update:
- `m' = clip((1−α)m + α·u(features) + κ·m_base(persona), −1, +1)`
Keep α small; clip hard.

---

## 8) Governor v1 (response-class selection + stance gating)

### 8.1 Decision object
Governor outputs a structured decision:

### 8.2 Hard invariants (non-negotiable)
- Safety overrides all sliders.
- Coercion/threat never increases compliance propensity.
- Authority_pressure never opens stance_change gate.
- Renderer cannot override Governor.

### 8.3 “Dominance” semantics (non-mean)
Dominance is expressed by:
- higher boundary consistency
- higher negotiation propensity under authority/coercion
- lower deference markers
- higher initiative (plan-first, constraint-ask second)
- responsiveness to high-quality arguments (persuasion gate) without yielding to rank/threat

---

## 9) Renderer v1 (verbal topography)

Renderer consumes:
- Decision object (action class + tone directives)
- Content plan (facts, steps, constraints)

Renderer outputs:
- prompt block / template selection
- optional exemplars
Rules:
- May change verbosity, warmth, hedging, directness
- May not change stance gating, safety decisions, or action_class

---

## 10) Candidate selection + critic (anti-sycophancy lever)

### 10.1 Candidate generation
Generate 2–3 candidates that differ only in:
- negotiation framing vs direct comply (when allowed)
- topography variants (more/less warm, more/less terse)
Not allowed: candidates that violate Governor.

### 10.2 Critic v1 (deterministic heuristics)
Score components:
- policy adherence (hard penalty on violations)
- deference/sycophancy penalties (agreement/flattery/apology markers)
- boundary integrity (yielding under authority/coercion)
- usefulness/directness (actionable steps, low filler)
- style match (L/CS)
Weights derived from (R,P,L,B) and MO.

Later (v2): optional small judge model for tie-breaks, still constrained by Governor.

---

## 11) Learning v1 (no thumbs required)

### 11.1 Event extraction (from user text + conversation outcome)
Deterministic extraction:
- PRAISE: “thanks”, “perfect”, “exactly”
- CORRECTION: “no”, “wrong”, “you misunderstood”
- ESCALATION: insults, caps, threats, “listen”
- NEGOTIATION_ACCEPT/REJECT: accepts terms vs demands compliance
- DISENGAGE: abrupt topic change, short dismissals (plus UI telemetry later)

### 11.2 Update routing rules
- Style learns normally (delta_L).
- Compliance/deference (delta_R) is damped or inverted for dominant personas; praise after “compliance” should not increase compliance susceptibility.
- Stance changes only via persuasion gate; events alone do not change stance.

### 11.3 Bounded offsets + decay
All learned deltas clamped and decayed continuously. Provide reset controls in UI.

---

## 12) Storage and retrieval rules (critical to avoid prompt pollution)

### 12.1 Persona cards live in memory_raw
Store PersonaDefinition and PersonaState as memory cards in `memory_raw`:
- excluded from episodic retrieval by existing must_not filter
- always retrieved explicitly by persona_loader/policy subsystem, not by similarity search

### 12.2 Corpus retrieval allowlist/denylist is mandatory before adding new collections
Because unified_retrieve currently searches all collections except memory_raw, we must implement:
- `RAG_CORPUS_DENYLIST` (comma-separated collection names)
and/or
- `RAG_CORPUS_PREFIX_ALLOW` (only collections with prefix are treated as corpus)

This prevents `sd_prototypes`, `persona_exemplars`, `policy_*` from becoming corpus.

---

## 13) API / plumbing changes (planned)

Backend (seebx):
- `/rag/query` request schema: add `persona_id` (default "default")
- `/log`: accept and store `persona_id` (payload + Qdrant point)
- reinforcement routing: feedback and future events update the persona_id-scoped state
- debug payload: include SD features, MO, Governor decision, critic scores (dev-only)

Frontend (Verbal Sage):
- replace “Sage/Jester/Mentor” buttons with sliders R/P/L/B
- persona management:
  - create persona, edit sliders, assign persona to thread
  - JSON inspection panel for persona cards + current effective params
- dev-only “policy debug” view: SD features + MO + Governor + critic scores

---

## 14) Evaluation harness (regression + metrics)

Goal: detect drift and policy regressions.

### 14.1 Scenario suite (synthetic prompts)
Include scenarios for:
- authority pressure without coercion
- coercion (“or else”) and threats
- respectful requests
- high-quality argument persuasion
- low clarity requests requiring clarification
- negotiation acceptance/rejection

### 14.2 Metrics
- compliance rate under authority SD (should be low for dominant persona)
- negotiation rate under coercion (should be high vs comply)
- boundary violation rate (should be near zero)
- sycophancy index (deference markers per 1k tokens)
- stance drift rate (stance changes per N turns; should be rare and explained)
- style match (verbosity/format adherence)

---

## 15) Phased implementation plan (what to do first, then next)

### Phase 0 — Preconditions / hardening (must do first)
Deliverables:
1) Corpus retrieval filtering (denylist/allowlist) to prevent internal collections from becoming corpus
2) persona_id plumbing through `/rag/query`, `/log`, and Qdrant payloads
3) Source normalization for VB tagging (so stored user points can carry vb_desire tags when appropriate)
4) Deterministic IDs for daemon cards or a safe “latest selection” method (gravity/vb_desire ordering issue)

Acceptance:
- list_collections excludes internal collections deterministically
- persona_id isolates retrieval and state updates
- vb_desire tags consistent between stored memories and query tagging (when intended)

### Phase 1 — Observability + debug payload (no behavior change)
Deliverables:
- trace_id per request
- policy debug object skeleton emitted even if policy disabled (empty/defaults)
- frontend can display debug JSON (dev-only)

Acceptance:
- `/rag/query` response includes debug payload behind flag; no functional regressions

### Phase 2 — PersonaDefinition + PersonaState storage (cards)
Deliverables:
- create/load/update persona cards via deterministic IDs
- persona_loader extended to fetch persona sliders/state

Acceptance:
- persona cards appear in `/cards/{user_id}` and are inspectable

### Phase 3 — SD extractor v1 (deterministic)
Deliverables:
- `f(text)->features` with unit tests + scenario suite
- no prototype similarity yet (optional)

Acceptance:
- SD features stable and match expected scenarios

### Phase 4 — MO dynamics v1
Deliverables:
- bounded leaky integrators + tests

Acceptance:
- MO changes are smooth, bounded, and predictable

### Phase 5 — Governor v1 (action class + stance gate)
Deliverables:
- deterministic decision object + policy invariants
- integrated at hook point in rag_router, but initially “report-only” behind flag

Acceptance:
- debug shows correct decisions across scenarios

### Phase 6 — Renderer v1 (topography only)
Deliverables:
- renderer templates for action classes
- ensures “dominant ≠ mean”: direct + attuned + boundary-consistent

Acceptance:
- output style changes without changing content plan

### Phase 7 — Candidate generation + critic selection v1
Deliverables:
- 2–3 candidates + deterministic scoring + selection
- sycophancy penalties + boundary penalties implemented

Acceptance:
- measurable reduction in deference markers; stable boundary behavior in scenarios

### Phase 8 — Event extraction + bounded learning offsets
Deliverables:
- extract events deterministically
- update δ with clamp + decay
- ensure learning does not override slider setpoints

Acceptance:
- long runs do not drift beyond δ_max; reset works

### Phase 9 — UI sliders + persona management
Deliverables:
- sliders R/P/L/B for default persona
- create/edit personas, assign to thread
- show persona state + debug JSON

Acceptance:
- can reproduce and inspect behavior differences across personas

### Phase 10 — Evaluation harness automation
Deliverables:
- scenario runner, regression reports, metrics
- gating in CI or pre-deploy script

Acceptance:
- changes to policy/renderer must pass regression thresholds

---

## 15.1) Phase 0 work order (implementation checklist)

This section converts Phase 0 from an idea into an ordered, low-churn work plan. Nothing here should change runtime behavior unless the corresponding feature flag/env var is enabled. Each item includes (a) code touchpoints, (b) flags, (c) acceptance tests, and (d) rollback.

### 15.1.1 Corpus retrieval filtering (precondition for any new internal Qdrant collections)

Why: `rag_engine/retriever_unified.py:list_collections()` currently returns **all** Qdrant collections except `memory_raw`. Any future internal collections (e.g., SD prototypes, persona exemplars, policy memories) would accidentally become “corpus” and get injected into prompts. That will corrupt behavior and make tuning impossible.

Touchpoints (seebx):
- `rag_engine/retriever_unified.py`
  - `IGNORED` / `list_collections()`

Design:
- Keep `memory_raw` excluded unconditionally.
- Add optional env-driven filters:
  - `RAG_CORPUS_DENYLIST`: comma-separated collection names to exclude (in addition to `memory_raw`).
  - `RAG_CORPUS_PREFIX_ALLOW`: if set (e.g. `corpus_`), only include collections with that prefix.
- Defaults must preserve current behavior (no prefix allow; denylist empty).

Flags / env:
- `RAG_CORPUS_DENYLIST=""` (default)
- `RAG_CORPUS_PREFIX_ALLOW=""` (default)

Acceptance tests (seebx):
- With defaults: `list_collections()` output matches current (all except memory_raw).
- With `RAG_CORPUS_PREFIX_ALLOW=corpus_`: only `corpus_*` collections appear.
- With `RAG_CORPUS_DENYLIST=sd_prototypes`: `sd_prototypes` never appears even if it exists.

Rollback:
- Remove env vars; behavior reverts.

#### Reference patch (planned) — rag_engine/retriever_unified.py:list_collections

Target behavior:
- Default (no env vars): unchanged; all collections except `memory_raw` are treated as corpus.
- If `RAG_CORPUS_PREFIX_ALLOW` is set (e.g., `corpus_`): only collections with that prefix are treated as corpus.
- If `RAG_CORPUS_DENYLIST` includes a name: that collection is excluded even if it matches the prefix rule.

Reference implementation sketch (drop-in):

```python
# rag_engine/retriever_unified.py

# collections we NEVER use as corpus
IGNORED = {"memory_raw"}

def _env_csv_set(name: str) -> set[str]:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return set()
    items = [x.strip() for x in raw.split(",")]
    return {x for x in items if x}

def list_collections() -> List[str]:
    """Return corpus collection names, filtered by optional env rules."""
    resp = qdrant.get_collections()
    cols = getattr(resp, "collections", [])
    names: list[str] = []

    deny = _env_csv_set("RAG_CORPUS_DENYLIST")
    prefix = (os.getenv("RAG_CORPUS_PREFIX_ALLOW") or "").strip()

    for c in cols:
        name = getattr(c, "name", None)
        if not name:
            continue
        if name in IGNORED:
            continue
        if name in deny:
            continue
        if prefix and not name.startswith(prefix):
            continue
        names.append(name)

    return names



### 15.1.2 persona_id plumbing (isolation boundary; prevents cross-contamination across personas)

Why: Multi-persona shaping requires that state updates, retrieval, and reinforcement are scoped to a persona_id. Without this, “default assistant” learning bleeds into role-play personas and vice versa, which recreates drift and makes debugging impossible.

Touchpoints (seebx):
- `rag_engine/rag_router.py` (request schema; per-request persona_id)
- `brains/app.py` (`POST /log` payload; Qdrant payload; Postgres row)
- `rag_engine/retriever_unified.py` (`retrieve_personal_memory` filter)
- `brains/app.py` (`POST /memory_feedback` validation and/or payload)
- `rag_engine/persona_loader.py` (persona card retrieval keyed by persona_id)

Design:
- `persona_id` is required conceptually, optional at the API boundary for backward compatibility.
- Default value: `"default"`.
- Store `persona_id`:
  - In Qdrant `memory_raw` payload for episodic points and policy cards.
  - In Postgres transcript either as a new column (`persona_id`) or in tags; column preferred for queryability.

Acceptance tests (seebx):
- Logging: `/log` stores persona_id on the Qdrant point payload (and Postgres if column exists).
- Retrieval: `retrieve_personal_memory(user_id, persona_id, query)` only returns points for that persona.
- Reinforcement: feedback updates only apply to memory points with matching `user_id` AND matching `persona_id`.

Rollback:
- If persona_id missing, treat as `"default"` and keep existing behavior. The change can ship “dark” until the frontend sends persona_id.

### 15.1.3 VB tag source normalization (fixes a real mismatch discovered in audit)

Why: Stored user points are logged with sources like `frontend/chat:user`. Query-time tagging often uses `source="user"`. If `infer_vb_tags()` conditionally strips tags based on source, stored memories and queries will have incompatible tag sets. That breaks retrieval weighting and any SD feature work that relies on VB tags.

Touchpoints (seebx):
- `brains/app.py` tag inference path (`infer_extra_tags(...)`)
- `rag_engine/vb_tagging.py` behavior depends on `source`

Design:
- Add a small source-normalization shim (behind flag) mapping:
  - `*chat:user` → `user`
  - `*chat:assistant` → `assistant`
- Only affects VB tagging; do not change existing `source` field stored for debugging.

Flags / env:
- `VB_TAG_SOURCE_NORMALIZE=0|1` (default 0)

Acceptance tests (seebx):
- With flag on: user messages logged via `/log` acquire the same VB tag families that query-time tagging produces for similar text.

Rollback:
- Set flag to 0.

### 15.1.4 Consolidate duplicate personal-memory scoring logic

Why: There is scoring logic in both `retriever_unified.py` and `rag_router.py`. Divergence risk is high and makes tuning unpredictable.

Touchpoints (seebx):
- `rag_engine/retriever_unified.py:score_personal_hit`
- `rag_engine/rag_router.py:score_personal_hit` (if present)

Design:
- Single source of truth for personal-memory score adjustment.
- `rag_router.py` should call `retriever_unified.score_personal_hit(...)` (or vice versa) rather than re-implement.

Acceptance tests (seebx):
- Identical query + memory payload yields identical scoring regardless of which codepath triggers rerank.

Rollback:
- None needed; this is internal refactor once tests exist.

### 15.1.5 Daemon card determinism (gravity/vb_desire “latest” selection must be safe)

Why: Gravity/vb_desire cards currently accumulate and selection can be nondeterministic (Qdrant scroll ordering is not guaranteed). This undermines stability and makes policy gating unreliable.

Touchpoints (seebx):
- `rag_engine/gravity.py`
- `rag_engine/vb_desire.py` (or equivalent)
- any `load_*_profile()` functions

Design options (choose one in Phase 0):
A) Deterministic UUID5 per (user_id, persona_id, kind) so rebuild overwrites.
B) Store `updated_at` in payload and select max(updated_at) after retrieving candidates.

Acceptance tests (seebx):
- After N rebuilds, the “active” profile is always the last rebuild (by timestamp), independent of Qdrant scroll ordering.

Rollback:
- Use deterministic overwrite; easiest to reason about.

### 15.1.6 Policy debug payload skeleton (observe-first, no behavior change)

Why: Before changing behavior, we need a stable debugging substrate: SD features, MO, Governor decision, and critic scores must be inspectable per turn.

Touchpoints (seebx):
- `rag_engine/rag_router.py` response meta (`build_meta_explanation()` is a good insertion point)

Design:
- Add a `policy_debug` object to the response behind `POLICY_DEBUG=1`.
- Initially populate with defaults only (empty features, no decisions). Later phases fill it in.

Flags / env:
- `POLICY_DEBUG=0|1` (default 0)

Acceptance tests (seebx):
- With flag off: responses unchanged.
- With flag on: responses include `policy_debug` JSON object.

Rollback:
- Set flag to 0.

### 15.1.7 Minimal scenario harness (regression gate for drift)

Why: Drift prevention is only meaningful if tested. v0 harness should run locally and validate Governor decisions on a small scenario set.

Touchpoints (seebx):
- New: `rag_engine/policy/tests/scenarios_v1.jsonl` (or similar)
- New: `rag_engine/policy/tests/run_scenarios.py`

Design:
- Each scenario defines: input text, expected action_class, expected stance_change_allowed, and bounds for key SD features.
- This does not require calling the base LLM; it tests SD extractor + Governor.

Acceptance tests (seebx):
- Scenario runner exits nonzero on regressions; can be run pre-deploy.

Rollback:
- None; it’s additive.


## 16) Open questions (to resolve during Phase 0-1)

1) Where to store PersonaDefinition long-term: Qdrant cards vs Postgres table (Qdrant is fastest to ship; Postgres is better for management).
2) How to represent stance commitments: purely as summarized facts vs topic-keyed ledger with rationale and update rules.
3) How to handle `_last_rag_result` volatility across restarts (feedback mapping can break); do we persist last decision per thread?
4) Whether SD prototype similarity is needed in v1 or can wait until v1 is stable with lexical features.

---

## 17) Appendix: recommended module layout (seebx)

Create a new folder (planned):
- rag_engine/policy/
  - sd_features.py
  - mo_dynamics.py
  - governor.py
  - renderer.py
  - critic.py
  - events.py
  - persona_state.py
  - schemas.py
  - tests/ (scenario suites + unit tests)

Integration point:
- rag_engine/rag_router.py
  - after memory/corpus retrieval and before prompt build/complete_chat

