# LifeSwitch Chat Data Source Map V1

Status: isolated candidate; no production wiring

## Objective

Give authenticated main chat access to the smallest sufficient slice of the
owner's structured LifeSwitch data. LifeSwitch data is neither personal Memory
V1 nor Qdrant retrieval. PostgreSQL remains authoritative.

The browser may request chat, but it may not choose an owner, data domain,
table, retrieval weight, or hidden prompt content. The backend derives the
owner from verified authentication, makes the data-intent decision, reads only
the authorized projection, and binds the projection to the final request.

## Decision order

1. Verify the authenticated actor and thread.
2. Apply response safety and controlling-domain policy.
3. Create a server-owned `LifeSwitchDataPlanV1` independently of response mode,
   Memory intent, web-search intent, and FM eligibility.
4. If the plan is `OFF`, perform no LifeSwitch query.
5. Resolve the current plan source when the selected projection requires plan
   targets.
6. Read only the selected, bounded owner-scoped projection.
7. Return `LifeSwitchDomainContextEnvelopeV1`.
8. Render one compact `lifeswitch_domain_context_v1` reference block.
9. Record content-free selection metadata in the response trace.

## Request-to-source map

| Question shape | Data intent | Authoritative sources | Projection |
|---|---|---|---|
| Unrelated question | `OFF` | none | no query, no prompt block |
| “What is my current plan?” | `PLAN` | `lifeswitch_agentic.plan_owner_state`, `lifeswitch_agentic.plan_versions`; explicit legacy fallback to `lifeswitch_plan.plan_profile` | current phase, goal, targets, dates, source status |
| “How did I do Monday?” / “Was protein low Monday?” | `NUTRITION_DAY` | `lifeswitch_nutrition.nutrition_day`, `nutrition_entry`, `my_food`, `my_food_serving`, `meal_item` | one local day of calorie, protein, carbohydrate, and fat totals plus applicable targets |
| “How have my macros been this week?” | `NUTRITION_RANGE` | same nutrition tables | bounded daily totals, averages, logged/missing days, target adherence |
| “How has training gone?” | `TRAINING_SUMMARY` | `lifeswitch_training.training_session_current_v`, `training_set_log`, `conditioning_session_current_v` | bounded sessions, active sets, strength/rehab separation, volume, conditioning duration, plan adherence |
| “Show Tuesday’s workout” | `TRAINING_SESSION` | current resistance-session view, active set log, and current conditioning-session view | bounded resistance and conditioning sessions for one local day |
| “How are squats progressing?” | `EXERCISE_PROGRESSION` | current training-session view and active strength set log | bounded per-session sets, reps, maximum load, total volume, unit |
| “How are my measurements changing?” | `MEASUREMENTS_SUMMARY` | `public.lifeswitch_measurement_entries` | bounded weight, waist, and body-fat trends with unit handling |
| “Am I meeting my plan, macros, and exercise goals?” | `OVERALL_STATUS` | resolved plan plus nutrition, training, conditioning, and measurement projections | compact plan-versus-actual summary with dates and data-sufficiency labels |

## Plan authority

The current agentic plan is authoritative when an active version exists. A
legacy profile may be read only when no active agentic version exists. The
envelope must label the source as `agentic_active`, `legacy_fallback`, or
`unavailable`. It must never merge active and legacy targets silently.

Audit snapshot on 2026-07-31 found one owner with an active agentic plan,
eleven owners with a legacy profile, and one owner represented in both.

## Default windows

- Nutrition summary: 21 local calendar days.
- Training summary: 28 local calendar days.
- Conditioning summary: 21 local calendar days.
- Measurements summary: 90 local calendar days.
- Exercise progression: 84 local calendar days, bounded to 200 result rows.
- Explicit day request: exactly one owner-local calendar day.

The trusted owner timezone must come from a server-controlled account or active
plan source. Free-form browser timezone headers are not sufficient authority for
historical-day interpretation in main chat.

## Prompt minimization

“Full access” means every relevant structured fact can be selected when needed;
it does not mean all available history enters a prompt.

- `NUTRITION_DAY`: target 250 tokens or fewer.
- Domain range/summary: target 450 tokens or fewer.
- `OVERALL_STATUS`: target 800 tokens or fewer.
- Hard maximum: 1,000 tokens for one LifeSwitch context block.

Raw database identifiers, free-form food notes, plan coach notes, and complete
set-by-set history are excluded unless a later typed projection explicitly
requires them.

## Independence boundaries

- Memory V1 may remember durable user facts, but it does not supply current
  nutrition, training, plan, or measurement totals.
- FM may affect eligible framing, but it cannot select or filter LifeSwitch data.
- Web search may supply external evidence, but it cannot replace owner data.
- Response mode controls answer behavior; `LifeSwitchDataPlanV1` controls which
  structured owner data, if any, is read.
- High-stakes policy controls how relevant data may be used. It does not erase
  directly relevant owner records automatically.

## Inspector contract

The safe trace may expose:

- whether LifeSwitch context was included;
- selected data intent and domains;
- local date window;
- plan source label;
- section and aggregate record counts;
- rendered token estimate;
- source-contract version and content hash.

It must not expose macro values, weight, measurements, plan text, exercise
details, raw SQL, owner UUIDs, or the assembled prompt.

## Known gaps before production integration

1. Main response requests do not currently carry a trusted owner timezone.
2. Prompt assembly currently accepts only Memory, FM, and prior-web blocks and
   caps context blocks at three; adding LifeSwitch requires a versioned contract
   change.
3. Nutrition and Training Analyze pages have historically calculated some
   metrics in browser code. Page and chat should consume the same backend
   analysis contract rather than preserve duplicate arithmetic.
4. Existing plan observation summaries omit carbohydrate/fat summaries and mark
   exercise progression as not computed.
5. LifeSwitch tables are service-only and the database role is non-superuser,
   but the tables do not currently enforce RLS. Backend owner binding remains a
   critical boundary; RLS hardening is a separate reviewed migration.
