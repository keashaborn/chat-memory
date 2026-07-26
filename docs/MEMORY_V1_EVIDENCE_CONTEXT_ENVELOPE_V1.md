# Memory Evidence Context Envelope V1

## Purpose

`memory_evidence_context_envelope_v1` restores bounded sibling context for an
already-admitted evidence span without converting neighboring text into an
independent fact. It is an extraction-time boundary, not retrieval memory.

## Authority

- Supabase-authenticated `owner_user_id` is the only owner.
- `public.chat_log` is the immutable raw source used to verify offsets and the
  full-source hash.
- `memory.evidence` spans remain the only admitted evidence units.
- Postgres and its forced owner RLS are authoritative.
- Qdrant, Vantage IDs, personas, cookies, and browser state have no role.

## Contract

The caller supplies an expected owner, target evidence ID, expected target
content hash, one owner-scoped raw source row, and the active evidence spans
bound to that source. The assembler verifies:

1. one owner across source, target, and siblings;
2. one source record, thread, request, and full-source hash;
3. exact Unicode offsets and content hashes;
4. non-overlapping, bounded, deterministic source order;
5. exactly one target evidence span.

The envelope assigns `target_assertion_source` to the target and
`disambiguating_context_only` to every sibling. Only the target evidence ID is
listed in `allowed_assertion_evidence_ids`. Raw source text is used transiently
for validation and is not retained in the envelope or sanitized audit.

## Integration boundary

The existing V5.2 provider still accepts one `TrustedExtractionSource`. Do not
silently concatenate sibling text into that source: it would invalidate source
offsets and provenance. The next integration step must add a typed provider
request input that carries:

- the unchanged target `TrustedExtractionSource`;
- this verified context envelope;
- explicit instructions that sibling spans may resolve pronouns, referents,
  lane, and project scope but may not originate observations.

Any proposed observation must continue to cite the target evidence span. A
future multi-evidence observation requires a separate reviewed contract, not an
implicit expansion of this envelope.

## Current exact evaluation

The first bounded case contains three spans from one Verbal Sage/LifeSwitch
design statement:

- technical project context;
- contextual project intent;
- the target user viewpoint.

For the exact target, the supported atomic assertion is the user's reported
stance that the philosophy can help people. The sibling spans disambiguate the
topic as Fractal Monism and its Verbal Sage/LifeSwitch project setting. The
larger response-policy goal spans multiple admitted evidence units and must be
formed later through an explicit multi-evidence project synthesis; it cannot be
smuggled through this target-only envelope. Neither result is a personal
biographical claim, a response-style preference, or an FM corpus authority
record.

This phase performs no extraction call, staging, durable claim creation,
Qdrant write, retrieval activation, or prompt influence.
