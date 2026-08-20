# SeeBx refactor batch 07: trusted-web monitoring repository boundary

Date: 2026-08-20

## Scope

This candidate-only batch removes the trusted-web monitoring query from the
search capability. The metadata models, aggregation, threshold evaluation,
bounded window, and public loader signature remain in
`seebx.capabilities.search.monitoring`. The new
`seebx.adapters.trusted_web_monitoring_postgres` adapter owns the exact
PostgreSQL view query and injected connection call.

Existing report and scheduled-monitor callers require no changes. The private
view, selected columns, time-window expression, ordering, arguments, metadata
only output, and fail-closed behavior are unchanged.

## Changed files

- `seebx/capabilities/search/monitoring.py`
- `seebx/adapters/trusted_web_monitoring_postgres.py`
- `tests/test_trusted_web_monitoring_v1.py`
- `docs/SEEBX_REFACTOR_BATCH_07_V1.md`

## Verified invariants

- The capability contains no database fetch or monitoring-view SQL.
- The adapter retains the exact private view, selected metadata columns,
  bounded interval, and deterministic ordering.
- The existing fake connection proves the query still receives exactly one
  bounded-hours argument.
- Focused monitoring and provider-boundary tests pass 7/7.
- The complete sealed-runtime suite passes 1,182/1,182.
- Direct-database capability files decrease from 16 to 15.
- The candidate still exposes 143 routes and 129 OpenAPI paths.
- Candidate route SHA-256 remains
  `c8d1df9cf48611e6c849614a521979b4a6109b0b4ef14f99ae55f4e85915d7d6`.
- Candidate OpenAPI SHA-256 remains
  `17aaa3a4a18bfe3a3d3671c1fee02654270524d0b5d764cd4c6eea6627429a8f`.
- Application import succeeds without an OpenAI key under synthetic,
  non-connectable database configuration; the optional client remains absent.

## Deployment state

Candidate only. Production source, services, databases, containers, systemd,
AWS controls, credentials, listeners, and frontend are unchanged.
