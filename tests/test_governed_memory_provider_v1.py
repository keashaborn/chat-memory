from __future__ import annotations

import unittest
from unittest.mock import patch
from uuid import UUID

from rag_engine.governed_memory_provider_v1 import (
    LiveGovernedMemoryAssemblyProviderV1,
)
from rag_engine.response_conversation_snapshot_v1 import (
    create_current_only_conversation_snapshot_v1,
)


ACTOR = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
THREAD = UUID("d776c8ef-7f3d-45b2-8820-4be87b7ca19d")


class GovernedMemoryProviderV1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_irrelevant_request_returns_empty_without_external_access(self) -> None:
        snapshot = create_current_only_conversation_snapshot_v1(
            authenticated_actor_user_id=ACTOR,
            thread_id=THREAD,
            current_request_id="ordinary-request",
            current_message="Explain why the sky looks blue.",
        )
        provider = LiveGovernedMemoryAssemblyProviderV1(object())

        with patch(
            "rag_engine.governed_memory_provider_v1.classify_memory_intent",
            return_value={
                "routes": {"governed_claims": False},
                "memory_intent": "none",
                "claim_context": {},
            },
        ), patch(
            "rag_engine.governed_memory_provider_v1.embed_text",
            side_effect=AssertionError("embedding must not run"),
        ), patch(
            "rag_engine.governed_memory_provider_v1.make_qdrant_client",
            side_effect=AssertionError("Qdrant must not be opened"),
        ):
            result = await provider.prepare(
                authenticated_actor_user_id=ACTOR,
                conversation_snapshot=snapshot,
            )

        self.assertIsNone(result.memory_input)
        self.assertIsNone(result.memory_application)


if __name__ == "__main__":
    unittest.main()
