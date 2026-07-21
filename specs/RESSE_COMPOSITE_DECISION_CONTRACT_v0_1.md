# RESSE Composite Decision Contract v0.1

Status: proposed; isolated implementation only  
Date: 2026-07-19  
Runtime integration authorized: no

## Purpose

Resolve runtime behavior and user-preference selection as one deterministic,
internally consistent decision before any prompt, retrieval, memory, model, or
tool adapter runs.

## Inputs

The pure decision function accepts:

- a normalized `PolicyInput`;
- optional trusted `PolicySignals`;
- a bounded user preference/profile payload;
- trusted profile-relevance booleans.

Profile relevance and policy signals are server-side adapter inputs. They must
not be copied directly from client JSON.

## Decision order

1. Resolve the backend-owned response mode and closure.
2. Resolve FM eligibility, authority tiers, and hit budget.
3. Construct the preference selector context from the resolved mode.
4. Select bounded presentation preferences and relevant user profile fields.
5. Return governed-memory permission without selecting or retrieving memory.

The caller cannot pass an independent response mode to the preference
selector. This prevents a `HIGH_STAKES` runtime decision from being paired with
ordinary preference behavior.

## Invariants

- Assistant identity is always `RESSE`.
- Mode precedence remains `HIGH_STAKES > TECHNICAL > FM_EXPLICIT > COACHING > ORDINARY`.
- `HIGH_STAKES` suppresses FM, free-form instructions, and profile context.
- `TECHNICAL` suppresses FM but may apply safe presentation preferences.
- Governed memory and structured application data remain independently
  permitted in every mode; selection belongs to the Memory V1 intent router.
- `RESSE` never becomes a memory owner or memory filter.
- Legacy identity, routing, tuning, and mode fields have no authority.
- The composite module performs no prompt assembly, retrieval, model call,
  database access, logging, or state mutation.

## Output

The bundle contains the complete runtime decision, preference envelope,
governed-context permissions, fixed identity, and machine-checkable invariants.
It contains selected profile values because a future prompt adapter needs them;
observability adapters must never log those values.

## Integration boundary

Production wiring remains blocked until Memory V1 stabilizes and joint review
covers `prompt_builder.py`, `persona_loader.py`, `vantage_router.py`, memory
retrieval, authentication/owner resolution, and the frontend chat route.

## Acceptance

- Cross-policy fixture passes all 30 synthetic cases.
- Runtime mode and preference mode always match.
- High-stakes local rules cannot be disabled by a negative classifier signal.
- Governed memory remains independently available but is never accessed here.
- No shared runtime integration module is imported or modified.
