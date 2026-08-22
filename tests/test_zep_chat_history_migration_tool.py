from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from scripts.prepare_legacy_memory_retirement_recovery import parse_postgres_dsn
from scripts.verify_zep_chat_history_migration import (
    MigrationContractError,
    MigrationExecutionError,
    build_schema_dump_command,
    build_psql_command,
    main,
    validate_forward_state,
    validate_forward_platform_disposition,
    validate_rollback_state,
    verify_package,
    verify_recovery_source,
)

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts/verify_zep_chat_history_migration.py"
PACKAGE = ROOT / "ops/migrations/20260819_zep_chat_history_cutover_v1/package.json"


def baseline_state() -> dict[str, object]:
    tables = [{"schema": "public", "table": "chat_log", "rows": 3}]
    return {
        "outbox_present": False,
        "history_definition_sha256": "a" * 64,
        "tail_definition_sha256": "b" * 64,
        "history_definition": "memory_ingest_private.memory_ingest_outbox",
        "tail_definition": "memory_ingest_private.memory_ingest_outbox",
        "legacy_telemetry": {
            "owner": "sage",
            "definition_sha256": "f" * 64,
            "body_sha256": "1" * 64,
            "config": ["search_path=pg_catalog, public, memory"],
            "brains_execute": True,
            "public_execute_revoked": True,
        },
        "canonical_telemetry": None,
        "legacy_triggers": [{"name": "legacy", "definition": "trigger"}],
        "table_manifest": tables,
        "table_manifest_sha256": "c" * 64,
    }


class ZepChatHistoryMigrationToolTests(unittest.TestCase):
    def test_package_binds_exact_tool_and_migration_bytes(self) -> None:
        package, paths, dependencies = verify_package(ROOT)
        self.assertEqual(
            package["tool"]["sha256"],
            hashlib.sha256(TOOL.read_bytes()).hexdigest(),
        )
        for name, path in paths.items():
            self.assertEqual(
                package["inputs"][name]["sha256"],
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        self.assertEqual(
            set(dependencies),
            {
                "database_consumer_audit",
                "platform_clean_baseline",
                "platform_database_audit",
                "platform_database_disposition",
            },
        )
        for name, path in dependencies.items():
            self.assertEqual(
                package["tool_dependencies"][name]["sha256"],
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )

    def test_psql_commands_are_noninteractive_and_transaction_explicit(self) -> None:
        settings = parse_postgres_dsn(
            "postgresql://sage:secret@127.0.0.1:5432/memory"
        )
        command = build_psql_command(
            settings,
            "ls_zep_test",
            Path("/secure/forward.pgsql"),
            single_transaction=True,
        )
        self.assertIn("--no-psqlrc", command)
        self.assertIn("ON_ERROR_STOP=1", command)
        self.assertIn("--single-transaction", command)
        self.assertNotIn("secret", command)

    def test_schema_dump_is_schema_only_and_disposable_target_bound(self) -> None:
        settings = parse_postgres_dsn(
            "postgresql://sage:secret@127.0.0.1:5432/memory"
        )
        command = build_schema_dump_command(
            settings,
            "ls_zep_test",
            Path("/secure/forward-schema.pgcustom"),
        )
        self.assertIn("--schema-only", command)
        self.assertIn("--no-owner", command)
        self.assertIn("--no-privileges", command)
        self.assertNotIn("secret", command)
        with self.assertRaisesRegex(
            MigrationContractError,
            "schema_dump_database_identity_invalid",
        ):
            build_schema_dump_command(
                settings,
                "memory",
                Path("/secure/forward-schema.pgcustom"),
            )

    def test_recovery_source_requires_full_hash_bound_success_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            os.chmod(directory, 0o700)
            backup = directory / "backup.dump"
            backup.write_bytes(b"archive")
            os.chmod(backup, 0o600)
            receipt = directory / "restore-receipt.json"
            document = {
                "schema_version": "seebx-legacy-memory-recovery-receipt-v1",
                "status": "pass",
                "backup": {
                    "path": backup.name,
                    "scope": "full_database",
                    "sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
                },
                "restore": {
                    "verified": True,
                    "disposable_database_dropped": True,
                },
                "row_manifest": {
                    "ordinary_table_counts": {
                        "memory": 160,
                        "memory_ingest_private": 7,
                    },
                    "table_inventory_sha256": "d" * 64,
                },
            }
            receipt.write_text(json.dumps(document), encoding="utf-8")
            os.chmod(receipt, 0o600)
            result = verify_recovery_source(backup, receipt)
            self.assertEqual(result["backup_sha256"], document["backup"]["sha256"])
            document["backup"]["scope"] = "legacy_schemas_only"
            receipt.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(
                MigrationContractError,
                "recovery_receipt_binding_invalid",
            ):
                verify_recovery_source(backup, receipt)

    def test_forward_state_requires_empty_outbox_and_no_data_drift(self) -> None:
        baseline = baseline_state()
        forward = dict(baseline)
        forward["outbox_present"] = True
        forward["history_definition"] = (
            "conversation_sync_private.zep_turn_outbox"
        )
        forward["tail_definition"] = "conversation_sync_private.zep_turn_outbox"
        forward["legacy_telemetry"] = None
        forward["canonical_telemetry"] = {
            "owner": "sage",
            "definition_sha256": "0" * 64,
            "body_sha256": "1" * 64,
            "config": ["search_path=pg_catalog, public"],
            "brains_execute": True,
            "public_execute_revoked": True,
        }
        forward["legacy_triggers"] = []
        forward["table_manifest"] = [
            {
                "schema": "conversation_sync_private",
                "table": "zep_turn_outbox",
                "rows": 0,
            },
            *baseline["table_manifest"],
        ]
        summary = validate_forward_state(baseline, forward)
        self.assertEqual(summary["outbox_rows"], 0)
        forward["table_manifest"][0]["rows"] = 1
        with self.assertRaisesRegex(MigrationExecutionError, "forward_state_mismatch"):
            validate_forward_state(baseline, forward)

    def test_rollback_state_must_exactly_restore_baseline(self) -> None:
        baseline = baseline_state()
        summary = validate_rollback_state(baseline, dict(baseline))
        self.assertTrue(summary["exact_baseline_restored"])
        drift = dict(baseline)
        drift["history_definition_sha256"] = "e" * 64
        with self.assertRaisesRegex(MigrationExecutionError, "rollback_state_mismatch"):
            validate_rollback_state(baseline, drift)

    def test_forward_platform_disposition_requires_zero_legacy_dependencies(self) -> None:
        audit = {
            "status": "pass",
            "candidate_commit": "a" * 40,
            "database": {
                "name": "ls_zep_test",
                "transaction_read_only": True,
            },
            "object_count": 2,
            "objects": [
                {
                    "identity": "memory_ingest_private.retired_table",
                    "classification": "unproven",
                },
                {
                    "identity": "chat_history_private.clear_history(uuid,text,uuid,integer)",
                    "classification": "application_direct",
                },
            ],
        }
        disposition = {
            "status": "candidate_baseline_ready",
            "baseline_generation_allowed": True,
            "baseline_blockers": [],
            "object_count": 2,
        }
        summary = validate_forward_platform_disposition(
            audit,
            disposition,
            temporary_database="ls_zep_test",
            candidate_commit="a" * 40,
        )
        self.assertEqual(summary["legacy_ingest_reachable_dependency_count"], 0)
        audit["objects"][0]["classification"] = "database_internal_reachable"
        with self.assertRaisesRegex(
            MigrationExecutionError,
            "forward_platform_legacy_ingest_dependencies_present",
        ):
            validate_forward_platform_disposition(
                audit,
                disposition,
                temporary_database="ls_zep_test",
                candidate_commit="a" * 40,
            )

    def test_function_lookups_cast_regprocedure_to_numeric_oid(self) -> None:
        source = TOOL.read_text(encoding="utf-8")
        self.assertEqual(
            source.count("SELECT to_regprocedure($1::text)::oid"),
            3,
        )
        self.assertNotIn(
            '"SELECT to_regprocedure($1::text)"',
            source,
        )

    def test_main_returns_zero_after_success(self) -> None:
        with (
            patch(
                "scripts.verify_zep_chat_history_migration.execute",
                new=AsyncMock(return_value={"status": "pass"}),
            ),
            patch("builtins.print") as print_result,
        ):
            exit_code = main(
                [
                    "--backup", "/secure/backup.dump",
                    "--recovery-receipt", "/secure/receipt.json",
                    "--output-root", "/secure/output",
                    "--run-id", "zep-test-20260819",
                    "--candidate-commit", "a" * 40,
                ]
            )
        self.assertEqual(exit_code, 0)
        print_result.assert_called_once()


if __name__ == "__main__":
    unittest.main()
