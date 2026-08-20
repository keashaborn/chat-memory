# Retired RESSE identifier migration

## Decision

RESSE is a retired product identity, not a backend capability. New SeeBx code
uses product-neutral conversation names. Historical database values remain
immutable until a separately versioned reader and writer cutover is verified.
This document is the single compatibility boundary; compatibility aliases or
parallel routers are not permitted elsewhere.

## Verified inventory

Read-only evidence collected on 2026-08-20 found:

- no Verbal Sage runtime consumer of the Python request-model name, endpoint
  function name, classifier environment key, or log label;
- no active protected environment setting named `RESSE_CLASSIFIER_MODEL`;
- 133 `public.chat_log` rows whose source is
  `backend/resse:assistant:v1`, spanning 2026-07-22 through 2026-08-19;
- 30 `public.chat_log` rows whose tags contain a RESSE identifier;
- 201 `public.chat_log` rows whose retired `vantage_id` is `RESSE`;
- six `public.telemetry_event` probe rows whose payload mentions RESSE;
- active frontend trace contracts still recognize `resse_response_v0_2` and
  `resse_response_v0_3`;
- active backend contracts still emit or verify the source, response-runtime,
  shadow-trace, safety-assessor, assistant-profile, and transcript-tag values
  listed below.

## Candidate-only internal rename

The current cleanup batch changes only non-persisted implementation names:

- `ResseResponseRequestV1` to `ConversationResponseRequestV1`;
- `resse_response_query` to `conversation_response_query`;
- `RESSE_CLASSIFIER_MODEL` to
  `CONVERSATION_SAFETY_CLASSIFIER_MODEL`;
- the `[resse_response]` log label to `[conversation_response]`.

The public route remains `POST /response/query`. Request and response field
behavior, authentication, memory selection, persistence, and model choice are
unchanged. The OpenAPI operation and schema names intentionally become
product-neutral; no current frontend code consumes them.

## Persisted identifiers not rewritten in place

The following values can participate in stored rows, hashes, traces, monitoring,
or frontend parsing and therefore stay unchanged in this batch:

- `backend/resse:assistant:v1`;
- `resse_response_v0_2`, `resse_response_v0_3`, and `resse_response_v0_4`;
- `resse_response_shadow_trace_v0_6`;
- `resse_safety_assessor_v0_2`;
- transcript tag `resse_v0_2`;
- assistant profile `RESSE`.

Historical attestations and transcript rows must never be mutated merely to
rename a product. Their exact bytes remain recovery and provenance evidence.

## Versioned cutover design

1. Define one product-neutral identifier registry owned by
   `seebx.contracts.conversation_provenance`. It must distinguish exact legacy
   stored values from the next canonical values and reject unknown values.
2. Introduce a new attestation contract and source identifier for new writes.
   Do not reuse `assistant_transcript_attestation_v1` with changed bytes.
3. Emit product-neutral response-runtime, shadow-trace, safety-assessor,
   response-profile, and transcript-tag identifiers from one release boundary.
4. Update Verbal Sage trace types, proxy headers, canary assertions, operational
   queries, and backend tests in the same paired candidate.
5. Read historical legacy identifiers only through the single registry. Do not
   add legacy routers, environment fallbacks, model aliases, or duplicate
   persistence paths.
6. Prove old records still verify, new records use only canonical identifiers,
   authenticated frontend chat and voice work, and rollback restores the prior
   writer without rewriting either generation of records.
7. Deploy the paired backend/frontend release atomically. Retire the legacy
   writer immediately; retain the centralized historical decoder for as long as
   immutable legacy records remain readable.

## Gates

Before production activation, require:

- exact legacy and new attestation verification fixtures;
- a read-only database inventory bound to row counts and content-free hashes;
- backend complete tests plus exact route and reviewed OpenAPI differences;
- frontend complete tests/build and authenticated text/voice checks;
- telemetry and synthetic-canary proof using canonical values;
- a paired rollback artifact that does not edit historical data;
