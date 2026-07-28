# Memory V1 Universal Identity Activation V1

## Authority

Memory eligibility follows authenticated Verbal Sage chat access. It does not
follow a Vantage ID, persona, cookie, legacy memory row, or static runtime
allowlist.

For browser text chat:

1. Verbal Sage verifies the Supabase JWT signature and required claims.
2. Verbal Sage performs a fresh Supabase `/auth/v1/user` lookup.
3. Verbal Sage forwards the original bearer token, service token, and exact
   Supabase user UUID to seebx.
4. seebx re-verifies the bearer token and requires its subject, the asserted
   actor UUID, and the requested owner UUID to match.

For Realtime voice, seebx requires the service token, exact actor/owner match,
and an active owner-bound voice-session lease.

## Enrollment

The successful transcript write path refreshes
`memory.authenticated_owner_registry_v1`. The registry is private operational
state containing UUIDs, verification timestamps, an authorization contract
version, and a request-ID hash. It contains no chat or memory content.

The six accounts explicitly confirmed as current on 2026-07-27 are bootstrapped
once by migration. New accounts self-enroll on their first authenticated stored
chat turn. Workers process registry owners verified during the preceding 90
days. Dormant owners resume automatically when they return.

Workers never discover owners by scanning `chat_log`, evidence, claims, Qdrant,
legacy Vantage data, or deleted-account remnants.

## Isolation

Registry enumeration exposes only UUIDs to the internal `brains_app` worker
session. Every actual read or write still sets transaction-local
`app.user_id` and is constrained by existing owner predicates, forced RLS,
restricted functions, and Qdrant owner filters. Authentication does not broaden
the memory scope beyond the authenticated UUID.

## Activation

The governed claim, preference, and project selectors may be enabled for all
authenticated actors. Existing intent routing, sensitivity gates, explicit
recall requirements, predicate allowlists, token budgets, Postgres
revalidation, and final-answer binding remain mandatory.

## Account retirement

A deleted account cannot make a fresh authenticated request. Its worker
eligibility expires after the activity window. Full account deletion must
continue to invoke the existing owner-scoped deletion workflow for Postgres,
Qdrant, transcripts, governed memory, and derived records; registry expiry is
not a replacement for data deletion.
