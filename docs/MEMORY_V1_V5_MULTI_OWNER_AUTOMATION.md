# Memory V1 V5 multi-owner automation canary

The private local V5 pipeline is enabled for exactly three owner-controlled
accounts:

- `1240822d-ac9a-4096-95aa-e2b24d36ef50`
- `557ea042-cb82-48f8-9429-472e96c957ef`
- `d839b4bc-0bd2-4f2d-aafe-0f3f75883db8`

The allowlist is repeated explicitly in the seven local pipeline service
definitions: inference, packet routing, auto-stage, entity validation,
auto-resolution, entailment, and claim projection. Each oneshot service has one
sequential `ExecStart` per owner. Every worker invocation receives exactly one
owner UUID, derives the transaction actor independently, and completes before
the next owner begins. This prevents first-owner starvation without sharing a
cursor or transaction between accounts. All durable tables retain forced RLS.

This does not enable V5 prompt influence, all-authenticated shadow retrieval,
the external OpenAI extraction scheduler, deletion, or another owner's data.
The external extraction service remains admin-only. Local extraction continues
through the privately controlled Resse-Train endpoint with one job per cycle,
zero retries, and the existing per-owner quota/circuit breaker.

Before installation, preserve a fresh Postgres backup, Qdrant signature, timer
state, and owner-scoped row counts. Quiesce only the seven affected timers,
install the service files, reload systemd, verify the exact commands, restore
the timer states, and run one bounded cycle. Re-run the activation-readiness
gate and account-isolation checks after active V5 claims are available.
