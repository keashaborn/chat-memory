# SeeBx refactor batch 10: atomic thread creation repository boundary

Date: 2026-08-20

## Scope

This candidate-only batch moves the final conversation capability transaction
out of the thread HTTP router and into the existing owner-scoped conversation
thread PostgreSQL adapter. The HTTP capability retains verified actor/owner
authority, request validation, response shaping, and the exact route contract.
The adapter atomically creates the new thread and promotes it to the active
thread inside one transaction using the existing active-selection primitive.

## Changed files

- `seebx/adapters/conversation_threads.py`
- `seebx/capabilities/conversation/thread_routes.py`
- `tests/test_conversation_threads_adapter.py`
- `tests/test_active_thread_selection_v1.py`
- `docs/SEEBX_REFACTOR_BATCH_10_V1.md`

## Required verification

- Thread creation and active selection occur in exactly one adapter-owned
  transaction with no nested transaction.
- The new thread remains the selected owner-scoped visible thread.
- The HTTP route contains no transaction or database query.
- Missing/invalid identity still fails before database access.
- The ten thread lifecycle routes and all request/response contracts remain
  unchanged.
- Direct-database conversation capability files decrease from one to zero.
- Focused and complete locked-dependency runtime tests pass.
- Route and OpenAPI hashes remain identical to Batch 09.

## Verified results

- Forty-one focused adapter, active-selection, identity, lifecycle-surface, and
  conversation-router tests pass.
- All 1,186 locked-dependency runtime tests pass.
- Thread creation and active selection use exactly one adapter-owned
  transaction; the promotion primitive does not open a nested transaction.
- The thread HTTP route retains actor verification before opening an
  owner-scoped connection and contains no transaction or direct query.
- No SQL or database-client call remains in the conversation capability. The
  sole textual `.execute(` match is the typed LifeSwitch composition-stage
  invocation, not a database operation.
- The candidate still exposes 143 routes and 129 OpenAPI paths.
- Candidate route SHA-256 remains
  `c8d1df9cf48611e6c849614a521979b4a6109b0b4ef14f99ae55f4e85915d7d6`.
- Candidate OpenAPI SHA-256 remains
  `17aaa3a4a18bfe3a3d3671c1fee02654270524d0b5d764cd4c6eea6627429a8f`.
- Application import succeeds without an OpenAI key under the synthetic,
  non-connectable database configuration; the optional client remains absent.

## Deployment state

Candidate only. Production source, services, databases, containers, systemd,
AWS controls, credentials, listeners, and frontend are unchanged.
