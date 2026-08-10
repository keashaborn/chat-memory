from __future__ import annotations

import ast
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
