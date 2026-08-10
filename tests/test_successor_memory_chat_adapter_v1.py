from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest
from uuid import UUID

from rag_engine.governed_memory.contracts import ContractViolation
from rag_engine.governed_memory.response_contracts import (
    SuccessorMemoryAssemblyV1,
    SuccessorPromptReferenceContextBlockV1,
    SuccessorPromptReferenceFragmentV1,
    SuccessorResponseRequestV1,
)
from rag_engine.response_composition_root_v0_2 import GovernedMemoryAssemblyV1
from rag_engine.response_conversation_snapshot_v1 import (
    create_current_only_conversation_snapshot_v1,
)
from rag_engine.response_policy_v0_2 import ResponsePolicySignalsV0_2
from rag_engine.successor_memory_chat_adapter_v1 import (
    SuccessorMemoryChatAdapterV1,
)


ROOT = Path(__file__).resolve().parents[1]
ACTOR = UUID("11111111-1111-4111-8111-111111111111")
THREAD = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
REQUEST_ID = "successor-chat-adapter-request"
QUERY = "Which synthetic interface theme do I prefer?"
CONTENT = "Synthetic owner prefers the cobalt interface theme."


def _canonical_model_bytes(value: object) -> bytes:
    return json.dumps(
        value.model_dump(mode="json"),  # type: ignore[attr-defined]
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _local_assembly() -> SuccessorMemoryAssemblyV1:
    raw = CONTENT.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    return SuccessorMemoryAssemblyV1(
        successor_memory_context_block=SuccessorPromptReferenceContextBlockV1(
            source_contract_version="governed-memory-answer-context-v1",
            source_manifest_sha256=digest,
            request_id_sha256=hashlib.sha256(
                REQUEST_ID.encode("utf-8")
            ).hexdigest(),
            query_sha256=hashlib.sha256(QUERY.encode("utf-8")).hexdigest(),
            content=CONTENT,
            content_sha256=digest,
            content_bytes=len(raw),
            estimated_tokens=(len(raw) + 3) // 4,
            fragments=(
                SuccessorPromptReferenceFragmentV1(
                    ordinal=0,
                    byte_offset=0,
                    byte_length=len(raw),
                    content_sha256=digest,
                    estimated_tokens=(len(raw) + 3) // 4,
                ),
            ),
        )
    )


class RecordingCoreProvider:
    def __init__(self) -> None:
        self.requests: list[SuccessorResponseRequestV1] = []
        self.selected = False

    @property
    def has_selected_claims(self) -> bool:
        return self.selected

    def prepare(
        self,
        *,
        request: SuccessorResponseRequestV1,
    ) -> SuccessorMemoryAssemblyV1:
        self.requests.append(request)
        self.selected = True
        return _local_assembly()

    def discard_selected_state(self) -> None:
        self.selected = False

    async def persist_dispatched_answer_binding(self, **_kwargs: object) -> object:
        raise AssertionError("persistence is outside this translation test")


class SuccessorMemoryChatAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_validated_host_models_translate_to_exact_successor_wire(self) -> None:
        core = RecordingCoreProvider()
        adapter = SuccessorMemoryChatAdapterV1(core)  # type: ignore[arg-type]
        snapshot = create_current_only_conversation_snapshot_v1(
            authenticated_actor_user_id=ACTOR,
            thread_id=THREAD,
            current_request_id=REQUEST_ID,
            current_message=QUERY,
        )
        signals = ResponsePolicySignalsV0_2()

        assembly = await adapter.prepare(
            authenticated_actor_user_id=ACTOR,
            conversation_snapshot=snapshot,
            trusted_policy_signals=signals,
        )

        self.assertIsInstance(assembly, GovernedMemoryAssemblyV1)
        block = assembly.successor_memory_context_block
        self.assertIsNotNone(block)
        assert block is not None
        self.assertEqual(block.content, CONTENT)
        self.assertEqual(block.kind.value, "memory")
        self.assertEqual(len(core.requests), 1)
        request = core.requests[0]
        self.assertEqual(request.authenticated_actor_user_id, ACTOR)
        self.assertEqual(request.thread_id, THREAD)
        self.assertEqual(request.request_id, REQUEST_ID)
        self.assertEqual(request.current_message, QUERY)
        self.assertEqual(
            request.conversation_snapshot_sha256,
            snapshot.snapshot_sha256,
        )
        self.assertEqual(
            request.trusted_policy_signals_sha256,
            hashlib.sha256(_canonical_model_bytes(signals)).hexdigest(),
        )
        self.assertTrue(adapter.has_selected_claims)
        adapter.discard_selected_state()
        self.assertFalse(adapter.has_selected_claims)

    async def test_invalid_host_policy_never_reaches_core(self) -> None:
        core = RecordingCoreProvider()
        adapter = SuccessorMemoryChatAdapterV1(core)  # type: ignore[arg-type]
        snapshot = create_current_only_conversation_snapshot_v1(
            authenticated_actor_user_id=ACTOR,
            thread_id=THREAD,
            current_request_id=REQUEST_ID,
            current_message=QUERY,
        )
        with self.assertRaisesRegex(
            ContractViolation,
            "invalid_response_memory_policy_signals",
        ):
            await adapter.prepare(
                authenticated_actor_user_id=ACTOR,
                conversation_snapshot=snapshot,
                trusted_policy_signals=object(),  # type: ignore[arg-type]
            )
        self.assertEqual(core.requests, [])


class SuccessorRouterImportTests(unittest.TestCase):
    def test_router_import_does_not_import_legacy_memory_provider(self) -> None:
        script = (
            "import sys\n"
            "import rag_engine.resse_response_router\n"
            "assert 'rag_engine.governed_memory_provider_v1' not in sys.modules\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)


if __name__ == "__main__":
    unittest.main()
