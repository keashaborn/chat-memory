# Vantage — Next Work Order (v0.1)

**Purpose:** the next incremental build steps for expanding the canary control layer beyond Y/R/C/S, in a safe, testable order.

---

## 0) Current baseline (done)
- `/vantage/query` and `/vantage/feedback` live
- limits `{Y,R,C,S}` wired end-to-end (UI cookie → `/api/chat` → Brains)
- debug trace available: `meta_explanation.vantage.{sd,limits,params,decision}`
- docs split:
  - `vantage_engine.md` = index
  - `vantage_controls.md` = as-built truth
  - `vantage_engine_spec_v0.2.md` = design history

---

## 1) Next build order (do in this order)

### Phase 1 — Routing knobs (answer strategy)
Add explicit routing controls (separate from Y/R/C/S):
- `answer_first: boolean` (default true)
- `clarify_bias: number` 0..1 (default 0.10)
- `max_clarify_questions: number` 0..3 (default 1)

**Acceptance:**
- broad explainers (e.g. “Tell me about X from FM perspective”) default to COMPLY
- truly underspecified prompts (“help me with my thing”) CLARIFY, capped to N questions
- routing decision is deterministic (no sampling variance)

**Implementation sketch:**
- Frontend: store routing in cookie `vs_vantage_routing` (or fold into a unified `vs_profile`)
- `/api/chat`: forward routing fields to Brains
- Brains: modify `decide()` to consult routing overrides:
  - if `answer_first=true`, prefer COMPLY when safe even when GC is low
  - apply `clarify_bias` only when ambiguity exists
  - cap clarifying questions in renderer/enforcement block

---

### Phase 2 — Mix knobs (dataset weighting)
Add retrieval/mixing controls:
- conversation vs memory cards vs corpus vs FM lens
- similarity thresholds + per-source token budgets

**Acceptance:**
- shifting weights measurably changes injected context source mix
- no prompt pollution (internal collections not treated as corpus)

---

### Phase 3 — Generation knobs (safe subset)
Expose generation parameters:
- answer temperature, top_p, max tokens
- keep decision temperature fixed at 0

**Acceptance:**
- higher temperature increases surface variance without changing routing

---

## 2) Instrumentation / regression
Before enabling knobs broadly, add:
- a small scenario set for routing decisions
- one-liner probes that print `decision + sd + routing knobs`

---

## 3) Notes
- Do not attempt “disable safety” knobs.
- Do not implement identity mimicry controls; implement rhetorical dimensions instead.
