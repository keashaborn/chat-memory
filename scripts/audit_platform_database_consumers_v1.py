#!/usr/bin/env python3
from __future__ import annotations

"""Build a content-free consumer map for the transitional platform database."""

import argparse
import json
from pathlib import Path

from database_consumer_audit_v1 import (
    AuditContractError,
    AuditExecutionError,
    AuditSpec,
    execute as execute_audit,
)


PLATFORM_SPEC = AuditSpec(
    schema_version="seebx-platform-database-consumer-audit-v1",
    container="brains-postgres-1",
    database="memory",
    admin_role="sage",
    output_root=Path(
        "/var/backups/seebx-cleanup/platform-database-consumers-v1"
    ),
    scoped_schemas=(
        "ai_operations",
        "catalog_dev",
        "chat_history_private",
        "chat_integrity",
        "lifeswitch_usage",
        "memory",
        "memory_ingest_private",
        "public",
        "trusted_web",
        "user_settings",
    ),
    runtime_roots=(Path("app.py"), Path("seebx")),
    operational_roots=(Path("scripts"),),
    migration_roots=(Path("ops/sql"),),
    require_source_manifest=False,
)


def execute(repository: Path, run_id: str, candidate_commit: str) -> dict:
    return execute_audit(
        repository,
        run_id,
        candidate_commit,
        None,
        spec=PLATFORM_SPEC,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--candidate-commit", required=True)
    arguments = parser.parse_args(argv)
    try:
        result = execute(
            arguments.repository,
            arguments.run_id,
            arguments.candidate_commit,
        )
    except (AuditContractError, AuditExecutionError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
