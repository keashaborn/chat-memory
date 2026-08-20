# Vantage compatibility retirement boundary v1

This document separates the retired Vantage profile/routing system from two
things that must not be discarded by name alone: historical PostgreSQL rows
and Fractal Monism content that uses “vantage” as an ordinary philosophical
term.

## Verified state on 2026-08-20

The paired candidate parents are SeeBx `143a4458` and Verbal Sage `ca2c7fe`.
The frontend candidate deletes the uncalled `/api/chat/inspect` route and its
only `/vantage/query` request. The cleaned backend has no mounted Vantage,
cards, profile, or query route.

The current SeeBx candidate batch removes `vantage_id` from:

- new-thread bodies and thread-list/active query parameters;
- transcript ingestion and its PostgreSQL adapter;
- generic and trusted-web conversation inserts; and
- telemetry ingestion and inserts.

The response-policy deny list still names `vantage_id`. That is a security
control: it records and ignores an attempted retired client policy input. It
does not restore Vantage functionality.

The Fractal Monism runtime prompt also uses the word “vantage”. Those passages
describe relational perspective and are controlling content, not identifiers
or dependencies of the retired profile system.

## Live historical data

Read-only PostgreSQL inspection found:

- `public.chat_log.vantage_id`: 1,117 rows total, 10 historical values including
  NULL and retired labels; no index, view, or current candidate reader;
- `public.telemetry_event.vantage_id`: 851 rows total, only NULL and `default`;
  no index, view, metric, or current candidate reader;
- `public.vantage_answer_trace`: 1,046 rows, 1,728 KiB, first written
  2025-12-27 and last written 2026-07-21; no current candidate SQL caller; and
- four routines under the already-retiring `memory` schema whose definitions
  contain `vantage_id`.

`vantage_answer_trace` includes historical answer text. It therefore requires
the same restricted recovery handling as conversation history. Counts and
metadata may be recorded in audit evidence; row content must not be exported
into cleanup logs or source control.

## Current candidate invariant

After this batch, active code no longer declares, reads, or persists
`vantage_id`. Existing database bytes remain unchanged. A deployment can
therefore establish a
measurable no-new-writes interval without combining code and destructive data
changes.

Regression evidence:

- 32 focused owner, thread, transcript, persistence, search, and telemetry
  tests pass;
- the complete 1,142-test backend suite passes with the locked runtime
  dependencies;
- the frontend candidate passes all 360 tests plus the isolated production
  build and Turnstile artifact check;
- the synthetic 140-route set is unchanged with SHA-256
  `79f06546520a5f89ce7d97a8ee8e88be40865e44df41ffd6988a994c64c1b4dd`;
  and
- the OpenAPI SHA-256 changes from
  `e891a3e0efcefada32a4c7f5aed751e7968810c25f106deb4232280b2c1890c4` to
  `0c5154d19b8bbf29222c37a3ca667873125739ad16a5d8429783d3e39ddaf563`
  with exactly four differences: the new-thread body field and the three
  thread query parameters listed above are removed.

## Database retirement gate

No Vantage column, table, routine, or historical row is removed by this batch.
A later database action must be separately authorized and must:

1. freeze exact table/routine definitions, row counts, ACLs, RLS state, and
   content hashes in a restricted manifest;
2. produce an encrypted backup and prove a disposable restore with matching
   schemas and counts;
3. deploy the no-new-writes code and prove that the candidate created no new
   non-NULL Vantage metadata during the defined soak interval;
4. account for the four routines in the retiring `memory` schema;
5. apply an explicit, non-cascading migration with a precomputed rollback; and
6. re-run owner-isolation, conversation, telemetry, Zep, frontend, and recovery
   verification.

Until all six gates pass, these database objects are historical compatibility
data—not active architecture, but not disposable clutter.
