from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "audit_platform_database_consumers_v1.py"
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("platform_database_consumers", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


class PlatformDatabaseConsumersV1Tests(unittest.TestCase):
    def test_platform_spec_is_exact_and_never_requests_content(self) -> None:
        spec = module.PLATFORM_SPEC
        self.assertEqual(spec.container, "brains-postgres-1")
        self.assertEqual(spec.database, "memory")
        self.assertEqual(spec.admin_role, "sage")
        self.assertFalse(spec.require_source_manifest)
        self.assertTrue(spec.include_governance_manifest)
        self.assertEqual(
            spec.runtime_excluded_paths,
            (
                Path("seebx/adapters/lifeswitch_catalog_postgres.py"),
                Path("seebx/adapters/lifeswitch_foods_postgres.py"),
                Path("seebx/adapters/lifeswitch_meal_plans_postgres.py"),
            ),
        )
        self.assertEqual(
            spec.scoped_schemas,
            (
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
        )

    def test_execute_uses_shared_engine_and_platform_spec(self) -> None:
        expected = {"status": "pass"}
        with patch.object(module, "execute_audit", return_value=expected) as execute:
            actual = module.execute(Path("/tmp/repo"), "20260822t120000z", "a" * 40)
        self.assertEqual(actual, expected)
        execute.assert_called_once_with(
            Path("/tmp/repo"),
            "20260822t120000z",
            "a" * 40,
            None,
            spec=module.PLATFORM_SPEC,
        )

    def test_main_failure_is_content_free(self) -> None:
        with patch.object(
            module,
            "execute",
            side_effect=module.AuditExecutionError("objects_failed"),
        ), patch("builtins.print") as output:
            status = module.main(
                [
                    "--repository",
                    "/tmp/repo",
                    "--run-id",
                    "20260822t120000z",
                    "--candidate-commit",
                    "a" * 40,
                ]
            )
        self.assertEqual(status, 1)
        self.assertEqual(
            json.loads(output.call_args.args[0]),
            {"error": "objects_failed", "status": "failed"},
        )


if __name__ == "__main__":
    unittest.main()
