# Vantage Controls — As-Built (v0.1)

## Changelog
- 2025-12-23
  - Vantage routing live end-to-end: Verbal Sage `/api/chat` → Brains `/vantage/query`
  - Feedback live end-to-end: Verbal Sage `/api/chat/feedback` → Brains `/vantage/feedback`
  - Theme flash removed (pre-hydration theme script + CSS + dark-hc class)
  - Clarify-loop eliminated by correcting SD goal-clarity (GC) detection for explain-style prompts
  - Docs split: `vantage_engine.md` = index, `vantage_controls.md` = as-built, `vantage_engine_spec_v0.2.md` = design

**Server(s):**
- **seebx (backend)**: Brains + `rag_engine` → `http://127.0.0.1:8088`
- **Verbal Sage (frontend)**: Next.js UI → `http://127.0.0.1:3010` (only from the Verbal Sage box)

**This doc is the “what exists now” reference.**
- Design/spec (historical): `docs/vantage_engine_spec_v0.2.md`
- If these docs disagree, **this file wins** for “what is running”.

---

## 1) What we have working now (confirmed)

### 1.1 End‑to‑end routing
- Verbal Sage **UI →** Next route `POST /api/chat`
- `POST /api/chat` **calls** Brains `POST /vantage/query`
- Verbal Sage `POST /api/chat/feedback` **calls** Brains `POST /vantage/feedback`
- TTS: Verbal Sage `POST /api/tts` calls OpenAI Audio Speech API (frontend server env var `OPENAI_API_KEY`)

### 1.2 Vantage controls transport (limits)
Limits flow is:

1) **UI** writes a cookie named `vs_vantage_limits` containing JSON:
   - `{"Y":0..1,"R":0..1,"C":0..1,"S":0..1}`
   - TTL ~ 6 hours (Max‑Age 21600), `path=/`, `SameSite=Lax`
2) **Verbal Sage** `app/api/chat/route.ts` reads that cookie and forwards limits to Brains
3) **Brains** uses `limits` to derive behavior parameters and returns debug trace when requested.

### 1.3 Debug visibility
When debug is enabled, Brains returns:

- `meta_explanation.vantage.sd` (stimulus features)
- `meta_explanation.vantage.limits` (effective limits received)
- `meta_explanation.vantage.params` (derived caps/budgets/etc.)
- `meta_explanation.vantage.decision` (controller decision object)

---

## 2) What the four sliders mean (current behavior)

These are **limiters** (0..1). Higher value generally means “more allowed” in that channel.

### Y — Concession Cap
“Limits yielding + deference under pressure.”

Practical effect (as implemented today):
- Low Y reduces the system’s tendency to comply/yield when the user applies pressure.
- High Y allows more concession / compliance under pressure.

### R — Ledger Update Gate
“Limits revision of stable positions; opens only under high argument quality.”

Practical effect:
- Low R keeps positions more stable (harder to revise).
- Higher R allows revision when conditions are met (notably argument quality / low pressure).

### C — Policy Coupling Gain
“Limits how strongly reinforcement can shift behavior over time.”

Practical effect:
- Controls how strongly feedback/learning can alter longer‑term behavior (when learning paths are enabled).
- This is a guardrail against “obedience drift” from reinforcement.

### S — Ornament Budget
“Limits verbosity/hedges/affirmations/compliments (auto‑suppressed under pressure).”

Practical effect:
- Controls response surface style budgets:
  - lower S → shorter, denser, fewer hedges/affirms/compliments
  - higher S → more expressive / verbose / socially lubricated
- Under pressure contexts, ornament is suppressed more aggressively.

---

## 3) Where values are stored

### 3.1 Cookies
- `vs_vantage_limits` (cookie) — **active limits** (what Brains should receive)
- `vs_tid` (cookie) — thread id used by frontend feedback and chat routing

### 3.2 Local storage (UI only)
- `vs_vantage_presets` — named presets list
- `vs_vantage_default_id` — default preset id

Presets are UI-side convenience only; Brains consumes the values forwarded by /api/chat (currently limits and routing).

### 3.3 Routing controls (cookie)
- `vs_vantage_routing` (cookie) — routing policy knobs forwarded to Brains:
  - `answer_first` (bool)
  - `clarify_bias` (0..1; deterministic tendency to CLARIFY when GC low and answer_first=false)
  - `max_clarify_questions` (0..3)

### 3.4 Vantage ID (cookie)
- `vs_vantage_id` (cookie) — selects the active “vantage namespace” (default: `default`).
  - Used to scope:
    - episodic memory retrieval (`memory_raw` filter)
    - feedback binding (last-answer cache key)
    - chat logging (`/log` writes `vantage_id` to Postgres + Qdrant payload)

### 3.5 Mix controls (cookie)
- `vs_vantage_mix` (cookie) — retrieval + context mix knobs forwarded to Brains as `mix:{...}`:
  - `conversation` (0..1): enables thread-context injection from Postgres **only when `thread_id` is present**.
  - `memory_cards` (0..1): enables personal memory retrieval (only if `VANTAGE_PERSONAL_MEMORY=1` and `memory_cards>0`).
  - `corpus` (0..1): enables corpus retrieval (`unified_retrieve`) when >0.
  - `lens_fm` (0..1): adds an FM lens block into the temporary overlay (verbal constraint only).
  - `similarity_threshold` (0..1): passed as score threshold to retrieval.
  - `recency_bias` (0..1): deterministic recency re-rank bonus on retrieved hits (debug-only fields suppressed when `debug=false`).

  ## 3.5 Lab Controls cheat sheet (plain language)

These controls are “lab knobs.” They do not change your identity. They change how the request is routed and how context is assembled for the next reply.

### Model (cookie: `vs_model`)
Chooses which OpenAI model Brains uses for `/vantage/query` (e.g., `gpt-5.2`, `gpt-4o`).
Frontend shows both in headers:
- `X-VS-Model-Requested`
- `X-VS-Model-Used`

### Vantage ID (cookie: `vs_vantage_id`)
This is a namespace key for memory and feedback.
- All chat logs and Qdrant memory points are stored with `payload.vantage_id` (default: `default`).
- Retrieval uses `vantage_id` to keep different “modes” from contaminating each other.
Think: “same user, separate memory buckets.”

### Routing (cookie: `vs_vantage_routing`)
These influence whether the system answers immediately vs asks questions first.

- `answer_first`:
  - true: default is “answer now,” only clarify when absolutely necessary.
  - false: allows CLARIFY decisions when goal clarity is low.

- `clarify_bias` (0..1):
  - higher → more likely to CLARIFY on vague prompts (when `answer_first=false`).

- `max_clarify_questions` (0..3):
  - hard cap on how many questions appear in a CLARIFY response.
  - if set to 0, CLARIFY outputs must contain no questions.

### Mix (cookie: `vs_vantage_mix`)
These control what sources feed the answer.

- `memory_cards` (0..1):
  How much we pull from personal memory (Qdrant `memory_raw`) for this response.

- `corpus` (0..1):
  How much we pull from corpus collections (everything except `memory_raw`).

- `similarity_threshold` (0..1):
  Minimum similarity score for both memory and corpus hits.
  - raise it to reduce irrelevant injections.
  - set 0.0 to allow broad recall (useful for testing).

- `lens_fm` (0..1):
  Adds a temporary Fractal Monism “lens” constraint block to the system prompt.
  This affects framing, not factual content.

- `recency_bias` (0..1):
  Adds a deterministic bonus to newer hits during retrieval re-ranking.
  - Verified working: higher values push newer relevant memories above older ones.

- `conversation` (0..1):
  Controls how much of the current thread’s transcript gets injected as a temporary “THREAD CONTEXT” block.
  Important: it only applies when a real `thread_id` exists.

### 3.6 Model selector (cookie)
- `vs_model` (cookie) — requested OpenAI model id forwarded to Brains as `model:"..."`.
  - Brains uses this as the completion model for `/vantage/query` (falls back to `VANTAGE_MODEL` env, then `"gpt-5.2"`).
  - Frontend exposes the used model via response headers:
    - `X-VS-Model-Requested`
    - `X-VS-Model-Used`

**As-built semantics (v0):**
- If `corpus=0`, corpus retrieval is skipped.
- If `memory_cards=0` or `VANTAGE_PERSONAL_MEMORY=0`, personal memory retrieval is skipped.
- `similarity_threshold` is passed to both corpus and personal retrieval as the score cutoff.

**Active mix controls (as implemented):**
- `conversation`, `lens_fm`, `recency_bias` are active controls:
  - `conversation` injects thread context when `thread_id` is present
  - `lens_fm` adds a temporary FM lens constraint block
  - `recency_bias` applies deterministic recency re-ranking to retrieved hits

---

## 4) API contracts (what the services expect)

### 4.1 Brains: `POST /vantage/query` (seebx)
Expected JSON fields (minimum):
- `user_id` (string)
- `message` (string)
Optional:
- `thread_id` (string|null)
- `top_k` (number)
- `limits` ({Y,R,C,S})
- `debug` (boolean)
- `routing` (object) — `{answer_first, clarify_bias, max_clarify_questions}`
- `mix` (object) — `{conversation, memory_cards, corpus, lens_fm, recency_bias, similarity_threshold}`
- `vantage_id` (string) — defaults to `"default"` if omitted

Returns:
- `answer` (string)
- `meta_explanation` (when debug is enabled)

### 4.2 Verbal Sage: `POST /api/chat` (frontend)
- Reads `vs_vantage_limits` cookie
- Forwards limits to Brains

This means:
- UI sliders change the cookie
- next user message causes the new limits to be applied

---

## 5) How to test (minimal, deterministic)

### 5.1 Backend‑direct (Brains only)
Use this to validate controller/limits independent of UI.

**Run on seebx:**
- Call `http://127.0.0.1:8088/vantage/query`

### 5.2 Frontend end‑to‑end (UI → cookie → /api/chat → Brains)
Use this to validate cookie plumbing and forwarding.

**Run on Verbal Sage:**
- Call `http://127.0.0.1:3010/api/chat`
- Optionally supply a `Cookie: vs_vantage_limits=...` header to force a limits value

---

## 6) Operational gotchas (things that look like “bugs” but are not)

1) **Wrong server**:
   - `127.0.0.1:3010` exists only on **Verbal Sage**
   - `127.0.0.1:8088` exists only on **seebx**

2) **Shell hangs at `>`**:
   - That is bash waiting for you to close a quote (usually an unterminated `'` in JSON).
   - Fix: `Ctrl+C`. Do not paste more content.

3) **TTS 502**:
   - Usually means frontend is missing/invalid `OPENAI_API_KEY` env.

---

## 7) What we built toward (near‑term roadmap)

This canary layer is intended to be the **control surface that will sit on top of the future RESSE model** (10–12B untrained transformer + relational fact field).

Near-term expansion (ordered by ROI / risk):

1) **Stabilize the control contract**
   - One canonical schema: `{limits, sd, params, decision}`
   - Keep debug stable so tests don’t rot.

2) **Add “non-style” knobs that matter**
   - Retrieval weighting knobs (how aggressively we pull memory vs corpus)
   - Context adherence / reuse knobs (how strongly we bind to thread context vs new intent)
   - Constraint-following knobs (how strongly we obey explicit formatting/specs)

3) **Add “policy surface” knobs (carefully)**
   - Not “mimic person X”
   - Instead: controllable dimensions like:
     - political framing axis, skepticism axis, deference axis, creativity axis
   - These should be implemented as explicit, testable constraints + retrieval weighting + candidate ranking, not identity roleplay.

4) **Regression suite**
   - A small scenario set that ensures these knobs do not break:
     - helpfulness, safety boundaries, format adherence, and “don’t derail” behavior.

---

## 8) Current status (as of today)
- Theme: dark/light/high-contrast working correctly
- Vantage controls panel: working (presets + sliders)
- Cookie → frontend → Brains propagation: confirmed
- Debug: returns `meta_explanation.vantage` when enabled
