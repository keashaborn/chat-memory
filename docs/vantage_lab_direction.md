# Vantage Lab Direction (v0.2)

## Framing
We are not building a role-play app. We are building a controlled verbal behavior lab where:

- **Vantage** = the stable organism (identity/object that persists across time)
- **Actor** = the interaction partner (a human account/participant)
- **Relationship** = the per-actor shaping and history with a specific vantage

The goal is to observe what a base LLM becomes under controlled constraints + reinforcement history, without hardcoding ontology (“person” vs “not a person”) into the surface text.

## Identifiers
Every turn must be keyed by:
- `vantage_id` (required): the organism identity
- `actor_id` (required): the partner identity (can initially equal current Supabase user_id)
- `thread_id` (optional): conversation container

## Memory scopes (required for coherence + experimental control)
Even in a private lab, memory must be partitioned or the system becomes uninterpretable.

1) `scope=vantage_global`
- owned by `vantage_id`
- shared across all actors
- contains: stable characterization, long-run shaping summaries, organism-wide “gravity”/traits

2) `scope=relationship`
- owned by `vantage_id` + `actor_id`
- contains: dyadic history, relationship summaries, actor-specific contingencies

3) optional `scope=actor_private`
- owned by `actor_id`
- contains: private facts about the actor (only retrieved if explicitly enabled)

## Cross-actor queries
A vantage should be able to answer queries like:
- “What does Eric say about this?”
- “How does Alice usually react to X?”

This requires:
- relationship memory retrieval keyed by `vantage_id` + target `actor_id`
- an `actor_alias` mapping (name/handle → actor_id) so “Eric” resolves deterministically

## Definition freeze vs shaping-only evolution
We need an explicit finalization boundary:

- **Before finalize:** authorized edits can change the VantageDefinition (dials + characterization fields)
- **After finalize:** VantageDefinition becomes read-only; only reinforcement updates can change VantageState/RelationshipState (bounded deltas + decay)

This is required to run “same vantage, different shaping conditions” experiments.

## Prompt/content constraints (keep, but do not hardcode ontology)
We should enforce:
- do not claim real-world actions occurred unless the system actually did them
- do not invent integrations/capabilities
- separate: (a) behavioral constraints, (b) memory scope, (c) surface style

We should avoid forcing surface lines like “I am not a person” or “I am a person.”
That belongs to UI/protocol, not to the organism’s speech.

## Immediate audit implications
- Ensure retrieval calls pass `vantage_id` (currently personal-memory retrieval supports it; caller must pass it)
- Introduce `actor_id` and `scope` into logging + Qdrant payloads (even if actor_id==user_id initially)
- Update persona/style loaders to filter by `vantage_id` and scope (not user-only)
