from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest

from rag_engine.governed_memory.exclusive_cutover import (
    EXCLUSIVE_MODE_ENV,
    ExclusiveMemoryConfigurationError,
    ExclusiveMemoryMode,
    exclusive_memory_mode,
    legacy_memory_surfaces_enabled,
    successor_pilot_is_exclusive,
)


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app.py"


def _async_function_source(name: str) -> str:
    source = APP.read_text(encoding="utf-8")
    module = ast.parse(source, filename=str(APP))
    matches = [
        node
        for node in module.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    ]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one {name} function")
    extracted = ast.get_source_segment(source, matches[0])
    if extracted is None:
        raise AssertionError(f"could not read {name} source")
    return extracted


class ExclusiveCutoverContractTests(unittest.TestCase):
    def test_default_is_exact_legacy_mode(self) -> None:
        self.assertIs(exclusive_memory_mode({}), ExclusiveMemoryMode.LEGACY)
        self.assertTrue(legacy_memory_surfaces_enabled({}))
        self.assertFalse(successor_pilot_is_exclusive({}))

    def test_successor_pilot_retires_legacy_as_one_switch(self) -> None:
        values = {EXCLUSIVE_MODE_ENV: "successor_pilot"}
        self.assertIs(
            exclusive_memory_mode(values),
            ExclusiveMemoryMode.SUCCESSOR_PILOT,
        )
        self.assertFalse(legacy_memory_surfaces_enabled(values))
        self.assertTrue(successor_pilot_is_exclusive(values))

    def test_unknown_or_loosely_formatted_mode_fails_closed(self) -> None:
        for value in ("", "pilot", "SUCCESSOR_PILOT", " successor_pilot"):
            with self.subTest(value=value), self.assertRaises(
                ExclusiveMemoryConfigurationError
            ):
                exclusive_memory_mode({EXCLUSIVE_MODE_ENV: value})

    def test_legacy_routers_are_registered_only_as_one_group(self) -> None:
        source = APP.read_text(encoding="utf-8")
        self.assertIn("LEGACY_MEMORY_SURFACES_ENABLED =", source)
        self.assertIn(
            "if LEGACY_MEMORY_SURFACES_ENABLED:\n"
            "    app.include_router(vantage_router, prefix=\"/vantage\")",
            source,
        )
        self.assertIn(
            "if LEGACY_MEMORY_SURFACES_ENABLED:\n"
            "    app.include_router(\n"
            "        memory_v1_governed_claim_lifecycle_router_v1,",
            source,
        )
        self.assertIn(
            "        assistant_response_preferences_router_v1,",
            source,
        )

    def test_identity_compatibility_cannot_reach_embedding_in_successor_mode(self) -> None:
        source = _async_function_source("log_chat")
        identity = source.index(
            'if source == "frontend/identity" and text.startswith("FULL_NAME:"):'
        )
        retirement = source.index("legacy_identity_memory_retired", identity)
        embedding = source.index("client.embeddings.create", identity)
        transcript = source.index("# Stable transcript row id.")
        self.assertLess(retirement, embedding)
        self.assertLess(embedding, transcript)
        self.assertIn("INSERT INTO chat_log(", source[transcript:])
        self.assertIn("UPDATE public.chat_attachments", source[transcript:])

    def test_mutating_or_partial_legacy_operations_guard_before_store_access(self) -> None:
        expectations = {
            "admin_memory_health": (
                "admin_memory_health",
                "build_admin_memory_health_v1",
            ),
            "admin_memory_workbench": (
                "admin_memory_workbench",
                "list_admin_memory_workbench_v1",
            ),
            "admin_memory_workbench_feedback": (
                "admin_memory_workbench_feedback",
                "record_admin_memory_workbench_feedback_v2",
            ),
            "admin_memory_review_plan": (
                "admin_memory_review_plan",
                "build_personal_event_promotion_preview",
            ),
            "threads_delete": ("thread_delete", "asyncpg.connect"),
            "cards_list": ("cards_list", "get_qdrant"),
            "vantage_cards_list": ("vantage_cards_list", "asyncpg.connect"),
            "cards_upsert": ("cards_upsert", "get_qdrant"),
            "cards_delete": ("cards_delete", "get_qdrant"),
            "delete_all_user_data": ("delete_all_user_data", "asyncpg.connect"),
            "delete_recent_user_data": (
                "delete_recent_user_data",
                "asyncpg.connect",
            ),
            "export_user_data": ("export_user_data", "asyncpg.connect"),
        }
        for function_name, (operation, first_store_call) in expectations.items():
            with self.subTest(function=function_name):
                source = _async_function_source(function_name)
                guard = source.index(
                    f'_legacy_memory_retired("{operation}")'
                )
                store = source.index(first_store_call)
                self.assertLess(guard, store)

    def test_successor_log_does_not_refresh_legacy_owner_registry(self) -> None:
        source = _async_function_source("log_chat")
        guarded_write = (
            "if LEGACY_MEMORY_SURFACES_ENABLED:\n"
            "            await conn.fetchval(\n"
            "                \"\"\"\n"
            "                SELECT memory.register_authenticated_owner_v1"
        )
        self.assertIn(guarded_write, source)
        self.assertEqual(source.count("memory.register_authenticated_owner_v1"), 1)

    def test_health_separates_general_rag_from_declared_successor_identity(self) -> None:
        source = _async_function_source("health")
        self.assertIn('"general_rag_declared"', source)
        self.assertIn('"governed_memory_successor"', source)
        self.assertIn('"identity_status": "declared_not_verified"', source)
        for field in (
            "EXCLUSIVE_MEMORY_MODE.value",
            "LEGACY_MEMORY_SURFACES_ENABLED",
            "EXPECTED_POSTGRES_HOST",
            "EXPECTED_POSTGRES_PORT",
            "EXPECTED_POSTGRES_DATABASE",
            "EXPECTED_POSTGRES_ROLE",
            "EXPECTED_QDRANT_HOST",
            "EXPECTED_QDRANT_PORT",
            "QDRANT_ALIAS",
            "QDRANT_PHYSICAL_COLLECTION",
        ):
            with self.subTest(field=field):
                self.assertIn(field, source)
        self.assertNotIn("os.environ", source)
        self.assertNotIn("os.getenv", source)

    @unittest.skipUnless(
        all(
            importlib.util.find_spec(module) is not None
            for module in ("asyncpg", "fastapi", "jwt", "openai", "qdrant_client")
        ),
        "full Brains runtime dependencies are unavailable",
    )
    def test_successor_clean_process_never_imports_legacy_memory_graph(self) -> None:
        blocked = (
            "rag_engine.vantage_router",
            "rag_engine.assistant_response_preferences_router_v1",
            "rag_engine.memory_v1_governed_claim_lifecycle_router_v1",
            "rag_engine.raw_memory_ownership",
            "rag_engine.thread_deletion_v1",
            "rag_engine.admin_memory_health_v1",
            "rag_engine.admin_memory_workbench_v1",
            "scripts.review_promotion_plan",
        )
        script = textwrap.dedent(
            f"""
            import importlib.abc
            import sys

            blocked = {blocked!r}

            class BlockLegacy(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname in blocked:
                        raise ImportError("blocked legacy memory import: " + fullname)
                    return None

            sys.meta_path.insert(0, BlockLegacy())
            import app
            assert app.EXCLUSIVE_MEMORY_MODE.value == "successor_pilot"
            assert app.LEGACY_MEMORY_SURFACES_ENABLED is False
            assert not [name for name in blocked if name in sys.modules]
            """
        )
        environment = dict(os.environ)
        environment.update(
            {
                "POSTGRES_DSN": "postgresql://synthetic",
                EXCLUSIVE_MODE_ENV: "successor_pilot",
                "GOVERNED_MEMORY_CAPTURE_MODE": "off",
            }
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=completed.stderr or completed.stdout,
        )


if __name__ == "__main__":
    unittest.main()
