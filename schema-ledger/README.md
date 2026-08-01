# Governed Memory Schema Ledger V1 candidate

This directory is a self-contained, activation-ready candidate. It is not installed in `/opt/chat-memory`, is not committed, and applies no migration.

Authority is deliberately split:

- PostgreSQL is authoritative for live schema and live governed-memory state.
- `ledger/governed-memory-schema-ledger-v1.json` is the proposed authority for intended schema and migration history after a separately approved activation commit.
- Qdrant is recorded only as a derived, rebuildable index. The snapshot reads collection configuration and payload-schema field definitions, never points or payload values.
- GPU extraction remains proposal-only and is not schema or answer authority.

The candidate binds production commit `d554a3d9756ede7cd71c76b2a8de08bf26b14886`, tree `5e747e056221b1a037c723e57db51885424c8245`, 304 refs, and 216 registered worktrees. The production database has no migration-history relation. Accordingly, 491 production-oriented SQL sources are classified `unverifiable`, 173 test fixtures are `source-only`, and 3 repeated source hashes are `duplicated`. Nothing is guessed to have been applied.

Local validation:

```sh
# Mac/local candidate; no SSH or database access
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=schema_ledger_candidate/tools \
  /Users/seebx/.pyenv/versions/3.12.6/bin/python3.12 -B \
  schema_ledger_candidate/tools/validate_schema_ledger.py \
  --root schema_ledger_candidate
```

CI additionally supplies `--repository <checkout> --repository-commit <commit>` so every tracked SQL blob is compared by path, Git blob, SHA-256, mode, size, declarations, transaction classification, and unsafe-operation classification.

`tools/source_record_projection.py` is the sole SQL source-record projector. Both the remotely executed read-only inventory and the offline/CI validator import that exact reviewed source. The SSH controller injects the shared module and inventory helper into the remote Python process in memory with independent source hashes; it creates no remote file. The complete 667-record production inventory must compare with zero field differences and one exact canonical record-array hash.

See `docs/SCHEMA_LEDGER_V1.md` for the evidence model and `docs/ACTIVATION_RUNBOOK.md` for the separately gated activation and rollback.
