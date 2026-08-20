# SeeBx refactor batch 04: application-root ownership

Date: 2026-08-20

## Scope

This candidate-only batch makes the application root identify the reusable
backend as `SeeBx API` and removes the final direct OpenAI SDK construction
outside `seebx/adapters`.

`app.py` now requests its optional thread-title client through
`get_optional_openai_client()`. The shared adapter remains the sole owner of
OpenAI credential lookup, base URL selection, key-fingerprint cache identity,
and SDK client construction. A missing or blank key still disables optional
title generation without affecting application startup.

## Changed files

- `app.py`
- `seebx/adapters/openai.py`
- `tests/test_app_composition.py`
- `tests/test_openai_adapter.py`

## Verified invariants

- Direct `OpenAI(...)` construction exists only in
  `seebx/adapters/openai.py`.
- The application root has no direct route decorators.
- Missing credentials return no optional client; required adapter calls still
  fail closed.
- Configured clients still use the existing shared cache without retaining the
  plaintext key in the cache identity.
- API route count remains 143.
- OpenAPI path count remains 129.
- The canonical OpenAPI SHA-256 is
  `17aaa3a4a18bfe3a3d3671c1fee02654270524d0b5d764cd4c6eea6627429a8f`.

Focused application-root and adapter tests passed 11/11. The complete candidate
suite passed 1,176/1,176 tests. Python compilation and `git diff --check` also
passed.

## Deployment state

Candidate only. The running SeeBx service, production source tree, systemd
configuration, databases, containers, timers, credentials, and listeners are
