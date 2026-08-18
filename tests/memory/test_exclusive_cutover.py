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
    def test_contract_parser_is_import_safe_and_has_no_global_mode(self) -> None:
        source = (
            ROOT
            / "rag_engine"
            / "governed_memory"
            / "exclusive_cutover.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("EXCLUSIVE_MEMORY_MODE =", source)

    def test_missing_mode_fails_closed(self) -> None:
        with self.assertRaises(ExclusiveMemoryConfigurationError):
            exclusive_memory_mode({})
        with self.assertRaises(ExclusiveMemoryConfigurationError):
            legacy_memory_surfaces_enabled({})
        with self.assertRaises(ExclusiveMemoryConfigurationError):
            successor_pilot_is_exclusive({})

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

    def test_legacy_routers_and_imports_are_absent(self) -> None:
        source = APP.read_text(encoding="utf-8")
        self.assertIn("LEGACY_MEMORY_SURFACES_ENABLED =", source)
        for retired in (
            "rag_engine.vantage_router",
            "rag_engine.assistant_response_preferences_router_v1",
            "rag_engine.memory_v1_governed_claim_lifecycle_router_v1",
            "rag_engine.raw_memory_ownership",
            "rag_engine.thread_deletion_v1",
            "rag_engine.admin_memory_health_v1",
            "rag_engine.admin_memory_workbench_v1",
            "scripts.review_promotion_plan",
        ):
            with self.subTest(retired=retired):
                self.assertNotIn(retired, source)

    def test_identity_compatibility_cannot_reach_embedding_in_successor_mode(self) -> None:
        source = _async_function_source("log_chat")
        retirement = source.index("legacy_identity_memory_retired")
        authentication = source.index("await require_memory_actor_v1")
        identity = source.index('if source == "frontend/identity"')
        transcript = source.index("# Stable transcript row id.")
        self.assertLess(retirement, authentication)
        self.assertLess(identity, transcript)
        self.assertNotIn("client.embeddings.create", source)
        self.assertNotIn("memory_raw", source)
        self.assertIn("INSERT INTO chat_log(", source[transcript:])
        self.assertIn("bind_attachments_to_message(", source[transcript:])

    def test_retired_legacy_operations_contain_no_store_access(self) -> None:
        expectations = {
            "admin_memory_health": "admin_memory_health",
            "admin_memory_workbench": "admin_memory_workbench",
            "admin_memory_workbench_feedback": "admin_memory_workbench_feedback",
            "admin_memory_review_plan": "admin_memory_review_plan",
            "cards_list": "cards_list",
            "vantage_cards_list": "vantage_cards_list",
            "cards_upsert": "cards_upsert",
            "cards_delete": "cards_delete",
            "export_user_data": "export_user_data",
        }
        for function_name, operation in expectations.items():
            with self.subTest(function=function_name):
                source = _async_function_source(function_name)
                self.assertIn(f'_legacy_memory_retired("{operation}")', source)
                for forbidden in (
                    "asyncpg.connect",
                    "get_qdrant",
                    "client.embeddings",
                    "memory.",
                ):
                    self.assertNotIn(forbidden, source)

    def test_legacy_chat_deletes_require_canonical_erasure_before_store(self) -> None:
        expectations = {
            "threads_truncate_from_message": (
                "message_tail_delete",
                "message_tail",
            ),
            "threads_delete": ("thread_delete", "thread"),
            "delete_all_user_data": (
                "delete_all_user_data",
                "all_conversations",
            ),
            "delete_recent_user_data": (
                "delete_recent_user_data",
                "recent",
            ),
        }
        for function_name, (operation, selector) in expectations.items():
            with self.subTest(function=function_name):
                source = _async_function_source(function_name)
                guard = source.index("_conversation_erasure_required(")
                self.assertIn(f'"{operation}"', source[guard:])
                self.assertIn(f'"{selector}"', source[guard:])
                for forbidden in (
                    "parse_uuid",
                    "_require_actor_for_user",
                    "_require_actor_for_thread",
                    "asyncpg.connect",
                    "get_qdrant",
                    "DELETE FROM",
                ):
                    self.assertNotIn(forbidden, source)

    def test_successor_log_does_not_refresh_legacy_owner_registry(self) -> None:
        source = _async_function_source("log_chat")
        self.assertNotIn("memory.register_authenticated_owner_v1", source)

    def test_health_separates_general_rag_from_declared_successor_identity(self) -> None:
        source = _async_function_source("health")
        self.assertIn('"general_rag_declared"', source)
        self.assertIn('"governed_memory_successor"', source)
        self.assertIn('"identity_status": "declared_not_verified"', source)
        self.assertIn('"conversation_bridge"', source)
        self.assertIn('"capture"', source)
        self.assertIn('"startup_validated": True', source)
        self.assertIn("GOVERNED_MEMORY_CAPTURE_SETTINGS.mode", source)
        self.assertIn("CONVERSATION_BRIDGE_IDENTITY", source)
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
        blocked_exact = (
            "rag_engine.vantage_router",
            "rag_engine.raw_memory_ownership",
            "rag_engine.thread_deletion_v1",
            "rag_engine.governed_memory_provider_v1",
            "rag_engine.openai_chat_request_v3",
            "rag_engine.response_lifeswitch_integration_v1",
            "rag_engine.lifeswitch_prompt_integration_v1",
            "scripts.review_promotion_plan",
        )
        blocked_prefixes = (
            "rag_engine.memory_v1",
            "rag_engine.memory_prompt_",
            "rag_engine.assistant_response_preference",
            "rag_engine.admin_memory_",
        )
        script = textwrap.dedent(
            f"""
            import importlib.abc
            import sys

            blocked_exact = {blocked_exact!r}
            blocked_prefixes = {blocked_prefixes!r}

            def legacy(fullname):
                return (
                    fullname in blocked_exact
                    or any(fullname.startswith(prefix) for prefix in blocked_prefixes)
                )

            class BlockLegacy(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if legacy(fullname):
                        raise ImportError("blocked legacy memory import: " + fullname)
                    return None

            sys.meta_path.insert(0, BlockLegacy())
            import app
            assert app.EXCLUSIVE_MEMORY_MODE.value == "successor_pilot"
            assert app.LEGACY_MEMORY_SURFACES_ENABLED is False
            assert not [name for name in sys.modules if legacy(name)]
            """
        )
        environment = dict(os.environ)
        environment.update(
            {
                "POSTGRES_DSN": (
                    "postgresql://brains_app:synthetic@127.0.0.1:5432/memory"
                ),
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

    @unittest.skipUnless(
        all(
            importlib.util.find_spec(module) is not None
            for module in ("asyncpg", "fastapi", "jwt", "openai", "qdrant_client")
        ),
        "full Brains runtime dependencies are unavailable",
    )
    def test_retired_routes_refuse_before_auth_or_resource_construction(self) -> None:
        script = textwrap.dedent(
            """
            import asyncio
            from starlette.requests import Request
            import app

            calls = []

            async def async_bomb(*args, **kwargs):
                calls.append(("async", args, kwargs))
                raise AssertionError("retired route reached async resource")

            def sync_bomb(*args, **kwargs):
                calls.append(("sync", args, kwargs))
                raise AssertionError("retired route reached sync resource")

            app.asyncpg.connect = async_bomb
            app.get_qdrant = sync_bomb
            app._require_actor_for_user = async_bomb
            app._require_actor_for_thread = async_bomb
            app.SUCCESSOR_LIVE_AUTHORITY_FACTORY = sync_bomb
            app.client = sync_bomb

            request = Request({
                "type": "http",
                "method": "GET",
                "path": "/retired",
                "query_string": b"",
                "headers": [],
            })

            async def main():
                checks = (
                    app.admin_memory_health(request),
                    app.admin_memory_workbench(request),
                    app.admin_memory_workbench_feedback(request),
                    app.admin_memory_review_plan(request),
                    app.threads_delete("not-a-uuid", request),
                    app.cards_list("not-a-uuid", request),
                    app.vantage_cards_list("not-a-uuid", request),
                    app.cards_upsert("not-a-uuid", request),
                    app.cards_delete("not-a-uuid", "card", request),
                    app.delete_all_user_data("not-a-uuid", request),
                    app.delete_recent_user_data("not-a-uuid", request),
                    app.export_user_data("not-a-uuid", request),
                )
                responses = await asyncio.gather(*checks)
                assert all(response.status_code == 409 for response in responses)
                assert calls == []

            asyncio.run(main())
            """
        )
        environment = dict(os.environ)
        environment.update(
            {
                "POSTGRES_DSN": (
                    "postgresql://brains_app:synthetic@127.0.0.1:5432/memory"
                ),
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

    @unittest.skipUnless(
        all(
            importlib.util.find_spec(module) is not None
            for module in ("asyncpg", "fastapi", "jwt", "openai", "qdrant_client")
        ),
        "full Brains runtime dependencies are unavailable",
    )
    def test_successor_startup_refuses_invalid_capture_or_bridge_identity(self) -> None:
        valid_dsn = (
            "postgresql://brains_app:synthetic@127.0.0.1:5432/memory"
        )
        cases = (
            (
                {
                    "POSTGRES_DSN": valid_dsn,
                    EXCLUSIVE_MODE_ENV: "successor_pilot",
                    "GOVERNED_MEMORY_CAPTURE_MODE": "pilot",
                    "GOVERNED_MEMORY_CAPTURE_OWNER_ALLOWLIST": "",
                },
                "invalid_capture_owner_allowlist",
            ),
            (
                {
                    "POSTGRES_DSN": "postgresql://brains_app:synthetic@127.0.0.1:5433/memory",
                    EXCLUSIVE_MODE_ENV: "successor_pilot",
                    "GOVERNED_MEMORY_CAPTURE_MODE": "off",
                    "GOVERNED_MEMORY_CAPTURE_OWNER_ALLOWLIST": "",
                },
                "conversation_bridge_postgres_target_invalid",
            ),
        )
        for updates, expected in cases:
            with self.subTest(expected=expected):
                environment = dict(os.environ)
                environment.update(updates)
                completed = subprocess.run(
                    [sys.executable, "-c", "import app"],
                    cwd=ROOT,
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(
                    expected,
                    completed.stderr + completed.stdout,
                )


if __name__ == "__main__":
    unittest.main()
