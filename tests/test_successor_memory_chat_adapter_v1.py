from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from uuid import UUID

from rag_engine.governed_memory.contracts import ContractViolation
from rag_engine.governed_memory.response_postgres import (
    SuccessorResponsePostgresError,
)
from rag_engine.governed_memory.response_provider import (
    InactiveSuccessorMemoryProviderV1,
    SuccessorResponseConfigurationError,
)
from rag_engine.governed_memory.response_contracts import (
    SuccessorMemoryAssemblyV1,
    SuccessorPromptReferenceContextBlockV1,
    SuccessorPromptReferenceFragmentV1,
    SuccessorResponseRequestV1,
)
from rag_engine.governed_memory.response_provenance import (
    SuccessorMemoryAnswerProvenanceV1,
    SuccessorMemoryNotApplicableReason,
)
from rag_engine.governed_memory.runtime.openai_adapters import (
    OpenAIOutcomeUnknownFailure,
    OpenAITerminalFailure,
)
from seebx.capabilities.conversation.composition import GovernedMemoryAssemblyV1
from seebx.capabilities.conversation.snapshot import (
    create_current_only_conversation_snapshot_v1,
)
from rag_engine.response_policy_v0_2 import ResponsePolicySignalsV0_2
from rag_engine.response_source_awareness_v1 import MemorySourceStatusV1
from rag_engine.successor_memory_chat_adapter_v1 import (
    SuccessorMemoryChatAdapterV1,
)


ROOT = Path(__file__).resolve().parents[1]
ACTOR = UUID("11111111-1111-4111-8111-111111111111")
THREAD = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
REQUEST_ID = "successor-chat-adapter-request"
QUERY = "Which synthetic interface theme do I prefer?"
CONTENT = "Synthetic owner prefers the cobalt interface theme."
ANSWER = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


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


class FailingCoreProvider:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.discarded = False
        self.persist_called = False

    @property
    def has_selected_claims(self) -> bool:
        return False

    def prepare(
        self,
        *,
        request: SuccessorResponseRequestV1,
    ) -> SuccessorMemoryAssemblyV1:
        del request
        raise self.error

    def discard_selected_state(self) -> None:
        self.discarded = True

    async def persist_dispatched_answer_binding(self, **_kwargs: object) -> object:
        self.persist_called = True
        raise AssertionError("degraded persistence reached unavailable provider")


class EmptyCoreProvider:
    @property
    def has_selected_claims(self) -> bool:
        return False

    def prepare(
        self,
        *,
        request: SuccessorResponseRequestV1,
    ) -> SuccessorMemoryAssemblyV1:
        del request
        return SuccessorMemoryAssemblyV1()

    def discard_selected_state(self) -> None:
        return None

    async def persist_dispatched_answer_binding(self, **_kwargs: object) -> object:
        raise AssertionError("persistence is outside this source-status test")


class SuccessorMemoryChatAdapterTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def host_input() -> tuple[object, ResponsePolicySignalsV0_2]:
        return (
            create_current_only_conversation_snapshot_v1(
                authenticated_actor_user_id=ACTOR,
                thread_id=THREAD,
                current_request_id=REQUEST_ID,
                current_message=QUERY,
            ),
            ResponsePolicySignalsV0_2(),
        )

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
        self.assertIs(assembly.source_status, MemorySourceStatusV1.SELECTED)
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

    async def test_active_provider_without_selection_reports_checked_empty(self) -> None:
        adapter = SuccessorMemoryChatAdapterV1(EmptyCoreProvider())  # type: ignore[arg-type]
        snapshot, signals = self.host_input()

        assembly = await adapter.prepare(
            authenticated_actor_user_id=ACTOR,
            conversation_snapshot=snapshot,  # type: ignore[arg-type]
            trusted_policy_signals=signals,
        )

        self.assertIsNone(assembly.successor_memory_context_block)
        self.assertIs(
            assembly.source_status,
            MemorySourceStatusV1.CHECKED_EMPTY,
        )

    async def test_inactive_provider_reports_not_applicable(self) -> None:
        adapter = SuccessorMemoryChatAdapterV1(
            InactiveSuccessorMemoryProviderV1(
                SuccessorMemoryNotApplicableReason.ATTACHMENT
            )
        )
        snapshot, signals = self.host_input()

        assembly = await adapter.prepare(
            authenticated_actor_user_id=ACTOR,
            conversation_snapshot=snapshot,  # type: ignore[arg-type]
            trusted_policy_signals=signals,
        )

        self.assertIsNone(assembly.successor_memory_context_block)
        self.assertIs(
            assembly.source_status,
            MemorySourceStatusV1.NOT_APPLICABLE,
        )

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

    async def test_runtime_dependency_failures_degrade_without_memory(self) -> None:
        failures = (
            SuccessorResponsePostgresError(
                "response_memory_postgres_unavailable"
            ),
            SuccessorResponseConfigurationError(
                "successor_response_postgres_unavailable"
            ),
            ContractViolation("qdrant_response_transport_unavailable"),
            OpenAIOutcomeUnknownFailure("provider_timeout_after_dispatch"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                core = FailingCoreProvider(failure)
                adapter = SuccessorMemoryChatAdapterV1(core)  # type: ignore[arg-type]
                snapshot, signals = self.host_input()
                assembly = await adapter.prepare(
                    authenticated_actor_user_id=ACTOR,
                    conversation_snapshot=snapshot,  # type: ignore[arg-type]
                    trusted_policy_signals=signals,
                )
                self.assertIsNone(assembly.successor_memory_context_block)
                self.assertIs(
                    assembly.source_status,
                    MemorySourceStatusV1.UNAVAILABLE,
                )
                self.assertFalse(adapter.has_selected_claims)
                self.assertTrue(core.discarded)
                provenance = await adapter.persist_dispatched_answer_binding(
                    answer_id=ANSWER,
                    prompt_sha256="c" * 64,
                    outbound_request_bytes=b'{"messages":[]}',
                )
                self.assertIsInstance(
                    provenance,
                    SuccessorMemoryAnswerProvenanceV1,
                )
                self.assertEqual(provenance.binding_outcome, "not_applicable")
                self.assertIs(
                    provenance.not_applicable_reason,
                    SuccessorMemoryNotApplicableReason.RUNTIME_UNAVAILABLE,
                )
                self.assertEqual(provenance.references, ())
                self.assertFalse(core.persist_called)

    async def test_security_and_contract_failures_remain_fail_closed(self) -> None:
        failures = (
            SuccessorResponsePostgresError(
                "response_memory_owner_context_mismatch"
            ),
            SuccessorResponseConfigurationError(
                "successor_response_runtime_configuration_invalid"
            ),
            ContractViolation("qdrant_response_transport_invalid"),
            OpenAITerminalFailure("provider_auth_rejected"),
        )
        for failure in failures:
            with self.subTest(failure=str(failure)):
                core = FailingCoreProvider(failure)
                adapter = SuccessorMemoryChatAdapterV1(core)  # type: ignore[arg-type]
                snapshot, signals = self.host_input()
                with self.assertRaises(type(failure)) as raised:
                    await adapter.prepare(
                        authenticated_actor_user_id=ACTOR,
                        conversation_snapshot=snapshot,  # type: ignore[arg-type]
                        trusted_policy_signals=signals,
                    )
                self.assertIs(raised.exception, failure)
                self.assertFalse(core.discarded)
                self.assertFalse(core.persist_called)


class SuccessorRouterImportTests(unittest.TestCase):
    def test_router_import_does_not_import_legacy_memory_provider(self) -> None:
        script = (
            "import sys\n"
            "import seebx.capabilities.conversation.router\n"
            "assert 'rag_engine.governed_memory_provider_v1' not in sys.modules\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env={
                **os.environ,
                "GOVERNED_MEMORY_EXCLUSIVE_MODE": "successor_pilot",
            },
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)


if __name__ == "__main__":
    unittest.main()
