# Memory V1 Relationship V5.1 Evaluation — 2026-07-20

Status: offline/private-inference verification complete; production registry and
runtime activation remain disabled.

## Bound inputs

- Seebx commit: `c8e9372f51cc6f473fe4158d6024e82ea2aac206`
- Relationship cases SHA-256: `5c72d46f4642b574c867abec5817eb161f5a7363361a6eb4acb73be85b9095a0`
- Relationship registry artifact SHA-256: `01d0045cf5607b55eb7d2b97611c5810b81c6098c6118435a2015d5cc8102a4f`
- Predicate integration artifact SHA-256: `12e132f03554adb041090fa74ed99c178a20c5625cade85dc1bfb52b43a61eaf`
- Provider registry canonical SHA-256: `7599840d3c7bcc39dbaa0211adf0f2a088bf9ecb03d843984c25a7ac30ed1615`
- Private model: `qwen3-14b-local-extractor`
- Runtime revision: `llama.cpp-b10066-86a9c79f8`
- Policy compiler: `memory_v1_relationship_policy_compiler_v8`

## Final proof

Five bounded batches evaluated all 60 cases after the final compiler changes.

| Batch | Cases | Model/compiled pass | Governed pass | Local model calls |
|---|---:|---:|---:|---:|
| 1 | 12 | 12 | 12 | 12 |
| 2 | 12 | 12 | 12 | 12 |
| 3 | 12 | 12 | 12 | 12 |
| 4 | 12 | 12 | 12 | 11 |
| 5 | 12 | 12 | 12 | 12 |
| Total | 60 | 60 | 60 | 59 |

One case was resolved by a deterministic pre-inference guard. The suite covers
all 41 registered relationship predicates plus 19 negative, technical,
question-only, one-off, third-party, compound, correction, and supersession
cases.

Every batch reported:

- external model calls: `0`
- database writes: `0`
- Qdrant writes: `0`
- staging writes: `0`
- prompt influence: `0`

## Deterministic verification

- 50 relationship registry, policy, observation, and provider-boundary tests:
  pass.
- Existing Memory V1 local-provider regression suite: pass.
- Production V5 behavior remains separate; V5.1 is selected only by the
  proposed `memory_predicate_registry_v5_1` registry version.

## Activation boundary

This result does not authorize or perform production schema installation,
writer activation, claim materialization, Qdrant projection, retrieval, or
prompt influence. Those remain later controlled phases.
