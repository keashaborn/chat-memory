from __future__ import annotations

import hashlib
import json
import stat
import tempfile
import unittest
from pathlib import Path

from scripts.prepare_legacy_memory_retirement_recovery import (
    CREATEDB,
    DROPDB,
    EXPECTED_DATABASE,
    EXPECTED_PORT,
    EXPECTED_ROLE,
    EXPECTED_TABLE_COUNTS,
    PG_DUMP,
    PG_RESTORE,
    RecoveryContractError,
    RecoveryExecutionError,
    build_dump_command,
    canonical_bytes,
    compare_restored_manifest,
    connection_arguments,
    parse_postgres_dsn,
    prepare_output_directory,
    replace_dsn_database,
    subprocess_environment,
    validate_source_manifest,
    write_pgpass,
)

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts/prepare_legacy_memory_retirement_recovery.py"
PACKAGE = ROOT / "ops/retirements/20260819_legacy_memory_v1/package.json"


def manifest() -> dict[str, object]:
    tables = []
    for schema, count in EXPECTED_TABLE_COUNTS.items():
        for index in range(count):
            tables.append({"schema": schema, "table": f"table_{index:03d}", "rows": index})
    tables.sort(key=lambda item: (item["schema"], item["table"]))
    return {
        "schema_version": "seebx-legacy-memory-row-manifest-v1",
        "database": "memory",
        "schemas": ["memory", "memory_ingest_private"],
        "ordinary_table_counts": dict(EXPECTED_TABLE_COUNTS),
        "tables": tables,
        "table_inventory_sha256": hashlib.sha256(canonical_bytes(tables)).hexdigest(),
    }


class LegacyMemoryRecoveryToolTests(unittest.TestCase):
    def test_package_binds_exact_tool_bytes(self) -> None:
        package = json.loads(PACKAGE.read_text(encoding="utf-8"))
        binding = package["recovery"]["tool"]
        self.assertEqual(binding["path"], "scripts/prepare_legacy_memory_retirement_recovery.py")
        self.assertEqual(hashlib.sha256(TOOL.read_bytes()).hexdigest(), binding["sha256"])

    def test_production_target_and_clients_are_hard_bound(self) -> None:
        self.assertEqual(EXPECTED_DATABASE, "memory")
        self.assertEqual(EXPECTED_ROLE, "sage")
        self.assertEqual(EXPECTED_PORT, 5432)
        self.assertEqual(
            (PG_DUMP, PG_RESTORE, CREATEDB, DROPDB),
            (
                "/usr/bin/pg_dump",
                "/usr/bin/pg_restore",
                "/usr/bin/createdb",
                "/usr/bin/dropdb",
            ),
        )
        source = TOOL.read_text(encoding="utf-8")
        self.assertNotIn('parser.add_argument("--expected-database"', source)
        self.assertNotIn('parser.add_argument("--expected-user"', source)
        self.assertNotIn('parser.add_argument("--pg-dump"', source)

    def test_dsn_parsing_keeps_password_out_of_command_arguments(self) -> None:
        settings = parse_postgres_dsn(
            "postgresql://sage:p%40ss@127.0.0.1:5432/memory?sslmode=disable"
        )
        self.assertEqual(settings.password, "p@ss")
        arguments = connection_arguments(settings)
        self.assertNotIn("p@ss", arguments)
        environment = subprocess_environment(
            settings,
            Path("/tmp/test.pgpass"),
            dsn_environment_name="LEGACY_MEMORY_ADMIN_DSN",
        )
        self.assertNotIn("LEGACY_MEMORY_ADMIN_DSN", environment)
        self.assertNotIn("POSTGRES_DSN", environment)
        self.assertNotIn("PGPASSWORD", environment)
        self.assertEqual(environment["PGPASSFILE"], "/tmp/test.pgpass")
        self.assertEqual(environment["PGSSLMODE"], "disable")

    def test_pgpass_is_mode_0600_and_escapes_delimiters(self) -> None:
        settings = parse_postgres_dsn(
            "postgresql://sage:p%3Aa%5Css@127.0.0.1:5432/memory"
        )
        path = write_pgpass(settings)
        self.addCleanup(path.unlink, missing_ok=True)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(
            path.read_text(encoding="utf-8"),
            "127.0.0.1:5432:*:sage:p\\:a\\\\ss\n",
        )

    def test_output_root_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            target = base / "target"
            target.mkdir(mode=0o700)
            link = base / "link"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(
                RecoveryContractError,
                "output_root_symlink_rejected",
            ):
                prepare_output_directory(link, "run-20260819")

    def test_unknown_or_duplicate_dsn_options_fail_closed(self) -> None:
        with self.assertRaisesRegex(RecoveryContractError, "unsupported"):
            parse_postgres_dsn(
                "postgresql://sage:secret@127.0.0.1/memory?options=-cstatement_timeout=1"
            )
        with self.assertRaisesRegex(RecoveryContractError, "duplicate"):
            parse_postgres_dsn(
                "postgresql://sage:secret@127.0.0.1/memory?sslmode=require&sslmode=disable"
            )

    def test_restore_dsn_changes_only_database(self) -> None:
        source = "postgresql://sage:secret@127.0.0.1:5432/memory?sslmode=disable"
        restored = replace_dsn_database(source, "ls_mem_restore_20260819")
        self.assertEqual(
            restored,
            "postgresql://sage:secret@127.0.0.1:5432/ls_mem_restore_20260819?sslmode=disable",
        )

    def test_dump_command_is_exactly_scoped_and_snapshot_bound(self) -> None:
        settings = parse_postgres_dsn(
            "postgresql://sage:secret@127.0.0.1:5432/memory"
        )
        command = build_dump_command(
            settings,
            snapshot_id="00000003-0000001B-1",
            output_path=Path("/secure/legacy.dump.partial"),
        )
        self.assertEqual(command.count("--schema"), 2)
        self.assertIn("memory", command)
        self.assertIn("memory_ingest_private", command)
        self.assertIn("--snapshot", command)
        self.assertIn("00000003-0000001B-1", command)
        self.assertIn("--format=custom", command)
        self.assertNotIn("secret", command)
        self.assertNotIn("--clean", command)

    def test_row_manifest_validation_and_restore_comparison(self) -> None:
        source = manifest()
        restored = json.loads(json.dumps(source))
        restored["database"] = "ls_mem_restore_20260819"
        validate_source_manifest(source)
        compare_restored_manifest(source, restored)
        restored["tables"][0]["rows"] += 1
        with self.assertRaisesRegex(
            RecoveryExecutionError,
            "restored_table_or_row_count_mismatch",
        ):
            compare_restored_manifest(source, restored)

    def test_wrong_table_shape_fails_closed(self) -> None:
        document = manifest()
        document["ordinary_table_counts"]["memory"] = 159
        with self.assertRaisesRegex(
            RecoveryContractError,
            "legacy_schema_table_shape_mismatch",
        ):
            validate_source_manifest(document)

    def test_canonical_receipt_bytes_are_stable(self) -> None:
        self.assertEqual(canonical_bytes({"b": 2, "a": 1}), b'{"a":1,"b":2}')
