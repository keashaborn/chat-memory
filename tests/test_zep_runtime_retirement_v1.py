from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ZepRuntimeRetirementContractTests(unittest.TestCase):
    def test_app_has_no_retired_governed_memory_runtime_binding(self) -> None:
        source = (ROOT / "app.py").read_text()
        for retired_symbol in (
            "create_governed_memory_erasure_proxy_router_v1",
            "capture_decision_for_owner",
            "enqueue_captured_chat_log_message",
            "GOVERNED_MEMORY_ERASURE_PROXY_CONFIGURED",
            "CONVERSATION_BRIDGE_IDENTITY",
            "rag_engine.memory_actor_auth_v1",
        ):
            self.assertNotIn(retired_symbol, source)

    def test_health_declares_zep_without_retired_qdrant_surface(self) -> None:
        source = (ROOT / "app.py").read_text()
        self.assertIn('"provider": "zep"', source)
        self.assertIn('"postgres_access": "disabled"', source)
        for retired_symbol in (
            "qdrant_client",
            "make_qdrant_client",
            "get_qdrant",
            "QDRANT_URL",
            "qdrant_url",
            "qdrant_access",
            "DEFAULT_COLLECTION",
            "EMBED_MODEL",
            "general_rag_declared",
        ):
            self.assertNotIn(retired_symbol, source)

    def test_chat_and_memory_deletion_contracts_remain_separate(self) -> None:
        source = (ROOT / "app.py").read_text()
        self.assertIn('@app.post("/chat-history/clear")', source)
        self.assertIn('@app.delete("/memory/chat-and-zep/clear")', source)
        self.assertIn('"memory_retained": False', source)

    def test_live_response_roots_do_not_import_retired_memory_packages(self) -> None:
        for relative in (
            "seebx/capabilities/conversation/router.py",
            "rag_engine/response_composition_root_v0_2.py",
            "rag_engine/response_composition_root_v0_4.py",
        ):
            source = (ROOT / relative).read_text()
            self.assertNotIn("rag_engine.governed_memory", source)
            self.assertNotIn("successor_memory_chat_adapter_v1", source)

    def test_live_router_uses_one_conversation_persistence_service(self) -> None:
        source = (ROOT / "seebx/capabilities/conversation/router.py").read_text()
        self.assertIn(
            "seebx.capabilities.conversation.persistence",
            source,
        )
        self.assertEqual(source.count("persist_conversation_response("), 1)
        self.assertNotIn("response_persistence_v1", source)
        self.assertNotIn("response_persistence_v3", source)

        for relative in (
            "rag_engine/response_persistence_v1.py",
            "rag_engine/response_persistence_v3.py",
        ):
            compatibility_source = (ROOT / relative).read_text()
            self.assertIn("persist_conversation_response", compatibility_source)
            self.assertNotIn("INSERT INTO public.chat_log", compatibility_source)


if __name__ == "__main__":
    unittest.main()
