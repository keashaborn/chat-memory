from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import AsyncMock, patch

from scripts.verify_clean_backend_cutover import collect_database_state, evaluate_readiness, main


def ready_database() -> dict[str, object]:
    canonical = "conversation_sync_private.zep_turn_outbox"
    return {
        "zep_turn_outbox_present": True,
        "clear_history_present": True,
        "clear_message_tail_present": True,
        "clear_history_definition": canonical,
        "clear_message_tail_definition": canonical,
        "legacy_trigger_count": 0,
        "telemetry_retention_present": True,
        "memory_schema_present": True,
        "memory_ingest_schema_present": True,
        "memory_table_count": 160,
        "memory_ingest_table_count": 7,
        "external_legacy_function_refs": 0,
        "nonterminal_ingest": 0,
        "nonterminal_erasure": 0,
    }


class CleanBackendCutoverPreflightTests(unittest.TestCase):
    def test_candidate_and_retirement_pass_only_with_every_gate(self) -> None:
        result = evaluate_readiness(
            ready_database(),
            crontab_text="15 2 * * * /usr/local/bin/other-job\n",
            dependencies={"status": "pass"},
            backup_sha256="a" * 64,
            restore_receipt_sha256="b" * 64,
        )
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["candidate_deploy_ready"])
        self.assertTrue(result["legacy_schema_retirement_ready"])
        self.assertEqual(result["gaps"], [])

    def test_production_state_fails_closed_without_reading_user_content(self) -> None:
        database = ready_database()
        database.update(
            zep_turn_outbox_present=False,
            clear_history_definition="memory_ingest_private.memory_ingest_outbox",
            legacy_trigger_count=4,
        )
        result = evaluate_readiness(
            database,
            crontab_text="0 3 * * * cd /opt/chat-memory && ./eval_all_users.sh >> /tmp/log 2>&1\n",
            dependencies={"status": "fail"},
        )
        self.assertEqual(result["status"], "fail")
        self.assertFalse(result["candidate_deploy_ready"])
        self.assertIn("legacy_memory_cron_absent", result["gaps"])
        self.assertIn("zep_turn_outbox_present", result["gaps"])
        self.assertIn("backup_sha256_bound", result["gaps"])
        rendered = str(result)
        self.assertNotIn("/tmp/log", rendered)

    def test_commented_legacy_cron_does_not_block(self) -> None:
        result = evaluate_readiness(
            ready_database(),
            crontab_text="# 0 3 * * * cd /opt/chat-memory && ./eval_all_users.sh\n",
            dependencies={"status": "pass"},
        )
        cron = next(item for item in result["deployment_checks"] if item["name"] == "legacy_memory_cron_absent")
        self.assertEqual(cron["status"], "pass")

    def test_already_retired_schemas_are_reported_without_false_retirement_gate(self) -> None:
        database = ready_database()
        database.update(
            memory_schema_present=False,
            memory_ingest_schema_present=False,
            memory_table_count=0,
            memory_ingest_table_count=0,
        )
        result = evaluate_readiness(database, crontab_text="", dependencies={"status": "pass"})
        self.assertEqual(result["legacy_schema_state"], "retired")
        self.assertFalse(result["legacy_schema_retirement_ready"])
        self.assertEqual(result["deployment_gaps"], [])
        self.assertEqual(result["retirement_gaps"], [])

    def test_retired_schemas_do_not_hide_deployment_failures(self) -> None:
        database = ready_database()
        database.update(
            memory_schema_present=False,
            memory_ingest_schema_present=False,
            memory_table_count=0,
            memory_ingest_table_count=0,
            zep_turn_outbox_present=False,
        )
        result = evaluate_readiness(database, crontab_text="", dependencies={"status": "pass"})
        self.assertEqual(result["legacy_schema_state"], "retired")
        self.assertIn("zep_turn_outbox_present", result["deployment_gaps"])
        self.assertIn("zep_turn_outbox_present", result["gaps"])


class FakeConnection:
    async def fetchval(self, sql: str, *args: object) -> object:
        if "conversation_sync_private.zep_turn_outbox" in sql:
            return True
        if "clear_history(uuid,text,uuid,integer)" in sql:
            return 101
        if "clear_message_tail(uuid,uuid)" in sql:
            return 102
        if "pg_get_functiondef" in sql and args:
            return "conversation_sync_private.zep_turn_outbox"
        if "to_regclass('memory_ingest_private.memory_ingest_outbox')" in sql:
            return True
        if "to_regclass('memory_ingest_private.source_erasure_operation')" in sql:
            return True
        if "SELECT EXISTS" in sql and "nspname = 'memory_ingest_private'" in sql:
            return True
        if "SELECT EXISTS" in sql and "nspname = 'memory'" in sql:
            return True
        if "FROM pg_trigger" in sql:
            return 0
        if "enforce_telemetry_retention_v1" in sql:
            return True
        if "n.nspname = 'memory_ingest_private'" in sql:
            return 7
        if "n.nspname = 'memory'" in sql:
            return 160
        if "FROM pg_proc" in sql:
            return 0
        if "memory_ingest_outbox WHERE state" in sql:
            return 0
        if "source_erasure_operation WHERE state" in sql:
            return 0
        raise AssertionError(f"unexpected query: {sql}")


class CleanBackendDatabaseCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_collection_uses_only_metadata_and_counts(self) -> None:
        result = await collect_database_state(FakeConnection())
        self.assertTrue(result["zep_turn_outbox_present"])
        self.assertEqual(result["memory_table_count"], 160)
        self.assertEqual(result["memory_ingest_table_count"], 7)
        self.assertEqual(result["nonterminal_ingest"], 0)
        self.assertEqual(result["nonterminal_erasure"], 0)


class CleanBackendCliRedactionTests(unittest.TestCase):
    def test_unexpected_database_error_does_not_emit_exception_text(self) -> None:
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"CUTOVER_TEST_DSN": "postgres://user:secret@example/db"}),
            patch("scripts.verify_clean_backend_cutover.load_contract", return_value=((3, 12), {})),
            patch("scripts.verify_clean_backend_cutover.verify_contract", return_value={"status": "pass"}),
            patch("scripts.verify_clean_backend_cutover.asyncpg.connect", new=AsyncMock(side_effect=ValueError("postgres://user:secret@example/db"))),
            redirect_stdout(output),
        ):
            exit_code = main(["--dsn-env", "CUTOVER_TEST_DSN"])
        self.assertEqual(exit_code, 3)
        document = json.loads(output.getvalue())
        self.assertEqual(document["error"], "preflight_unexpected_error")
        self.assertNotIn("secret", output.getvalue())
