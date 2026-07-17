# Memory V1 foundation archive

Source branch: `memory_v1_foundation`

Source commit: `e16f3dd357f367eede3f81e4a386f3c9e0131706`

Archived on: 2026-07-17

These files are retained as byte-exact implementation provenance. They are not
part of the active runtime, Python package, systemd configuration, migration
runner, or test discovery path. Do not import or execute them.

The active Phase 0/V5 implementation remains outside this directory. Conflicts
were resolved to the tested integration versions because the foundation branch
contains older routing, sensitivity, and owner-scoping behavior. In particular,
its `vantage_router.py` variant queried thread context without an owner filter
and was not restored.

The original relative path is preserved below this directory. Canonical schema,
review, manifest, and design provenance remains in `ops/`, `tests/`, and `docs/`
where it can be tied to the installed database objects.
