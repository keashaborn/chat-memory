# Auditability Contract — Brains (seebx backend)

This repository guarantees end-to-end request traceability.

## Required Invariants (DO NOT BREAK)

1) Request correlation:
- Every HTTP request MUST have an `x-request-id` echoed back.
- Middleware SHOULD set `request.state.request_id`.

2) Transcript persistence:
- `/log` MUST store `request_id` in Postgres `chat_log.request_id`.
- Qdrant payloads SHOULD include `request_id`.

3) Telemetry:
- `/telemetry/event` MUST store `payload.request_id` (either provided or stamped server-side).

4) Answer trace:
- `/vantage/query` MUST write `public.vantage_answer_trace.request_id`
  and it MUST match the inbound `x-request-id` when provided.

These invariants enable deterministic debugging and auditability.

Last verified: 2026-01-14
