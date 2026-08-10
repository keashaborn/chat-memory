from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
from uuid import UUID

from rag_engine.governed_memory.contracts import (
    ContractViolation,
    canonical_json_bytes,
)
from rag_engine.governed_memory.response_provider import (
    EXCLUSIVE_MODE_ENV,
    EXCLUSIVE_MODE_LEGACY,
    EXCLUSIVE_MODE_SUCCESSOR,
    SuccessorGovernedMemoryAssemblyProviderV1,
    SuccessorResponseActorBinding,
    SuccessorResponseConfigurationError,
    choose_response_memory_provider,
    response_mode_from_environment,
)
from rag_engine.governed_memory.exclusive_cutover import (
    ExclusiveMemoryMode,
    exclusive_memory_mode,
)
from rag_engine.governed_memory.runtime.calibration import CalibrationDecision
from rag_engine.response_conversation_snapshot_v1 import (
    create_current_only_conversation_snapshot_v1,
)
from rag_engine.response_policy_v0_2 import ResponsePolicySignalsV0_2
from tests.memory._fixtures import (
    CLAIM_A,
    NOW,
    OWNER_A,
    PREDICATE_CATALOG,
    RESPONSE_A,
    THREAD_A,
    deterministic_vector,
    make_candidate,
    make_claim_row,
)


SESSION = UUID("12121212-1212-4212-8212-121212121212")
REQUEST = "successor-response-request"
QUERY = "Which synthetic interface theme do I prefer?"


def actor(*, eligible: bool = True) -> SuccessorResponseActorBinding:
    return SuccessorResponseActorBinding(
        owner_user_id=OWNER_A,
        session_id=SESSION,
        authentication_manifest_sha256="a" * 64,
        request_id=REQUEST,
        thread_id=THREAD_A,
        eligible=eligible,
    )


def snapshot():
    return create_current_only_conversation_snapshot_v1(
        authenticated_actor_user_id=OWNER_A,
        thread_id=THREAD_A,
        current_request_id=REQUEST,
        current_message=QUERY,
    )


def calibration(*, enabled: bool = True) -> CalibrationDecision:
    return CalibrationDecision(
        retrieval_enabled=enabled,
        threshold_micros=800_000 if enabled else None,
        artifact_sha256="b" * 64 if enabled else None,
        reason_code="calibration_approved" if enabled else "calibration_absent",
    )


@dataclass
class RecordingRepository:
    rows: tuple[Mapping[str, object], ...] = field(
        default_factory=lambda: (make_claim_row(),)
    )
    fail_persist: bool = False
    reads: list[tuple[SuccessorResponseActorBinding, tuple[UUID, ...]]] = field(
        default_factory=list
    )
    persisted: list[dict[str, Any]] = field(default_factory=list)

    async def read_claims(
        self,
        *,
        actor: SuccessorResponseActorBinding,
        claim_ids: Sequence[UUID],
    ) -> tuple[Mapping[str, object], ...]:
        self.reads.append((actor, tuple(claim_ids)))
        return self.rows

    async def persist_answer_binding(self, **kwargs: Any) -> None:
        if self.fail_persist:
            raise RuntimeError("synthetic persistence failure")
        self.persisted.append(dict(kwargs))


@dataclass
class RecordingVectorIndex:
    candidates: tuple[dict[str, object], ...] = field(
        default_factory=lambda: (make_candidate(),)
    )
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def search_owner_candidates(self, **kwargs: Any):
        self.calls.append(dict(kwargs))
        return self.candidates


@dataclass
class RecordingEmbedder:
    calls: list[str] = field(default_factory=list)

    async def embed(self, text: str):
        self.calls.append(text)
        return deterministic_vector()


class ResourceTrap:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"successor resource accessed: {name}")


def provider(
    *,
    binding: SuccessorResponseActorBinding | None = None,
    repository: RecordingRepository | None = None,
    vector_index: RecordingVectorIndex | None = None,
    embedder: RecordingEmbedder | None = None,
    decision: CalibrationDecision | None = None,
) -> SuccessorGovernedMemoryAssemblyProviderV1:
    return SuccessorGovernedMemoryAssemblyProviderV1(
        actor=binding or actor(),
        repository=repository or RecordingRepository(),
        vector_index=vector_index or RecordingVectorIndex(),
        embedder=embedder or RecordingEmbedder(),
        predicate_catalog=PREDICATE_CATALOG,
        calibration=decision or calibration(),
        clock=lambda: NOW,
        operation_id_factory=lambda: UUID(
            "34343434-3434-4434-8434-343434343434"
        ),
    )


class SuccessorResponseModeTests(unittest.TestCase):
    def test_default_is_legacy_and_mode_text_is_exact(self) -> None:
        self.assertEqual(response_mode_from_environment({}), EXCLUSIVE_MODE_LEGACY)
        self.assertEqual(
            response_mode_from_environment({}),
            exclusive_memory_mode({}).value,
        )
        self.assertEqual(
            response_mode_from_environment(
                {EXCLUSIVE_MODE_ENV: EXCLUSIVE_MODE_SUCCESSOR}
            ),
            EXCLUSIVE_MODE_SUCCESSOR,
        )
        self.assertEqual(
            EXCLUSIVE_MODE_LEGACY,
            ExclusiveMemoryMode.LEGACY.value,
        )
        self.assertEqual(
            EXCLUSIVE_MODE_SUCCESSOR,
            ExclusiveMemoryMode.SUCCESSOR_PILOT.value,
        )
        for invalid in (
            "",
            "successor",
            "SUCCESSOR_PILOT",
            " successor_pilot",
            "successor_pilot\n",
        ):
            with self.subTest(invalid=repr(invalid)):
                with self.assertRaises(SuccessorResponseConfigurationError):
                    response_mode_from_environment({EXCLUSIVE_MODE_ENV: invalid})

    def test_selector_constructs_exactly_one_provider_and_never_falls_back(self) -> None:
        calls: list[str] = []
        legacy = object()
        successor = object()

        selected, lifecycle = choose_response_memory_provider(
            mode=EXCLUSIVE_MODE_LEGACY,
            legacy_factory=lambda: calls.append("legacy") or legacy,
            successor_factory=lambda: calls.append("successor") or successor,
        )
        self.assertIs(selected, legacy)
        self.assertIsNone(lifecycle)
        self.assertEqual(calls, ["legacy"])

        calls.clear()
        selected, lifecycle = choose_response_memory_provider(
            mode=EXCLUSIVE_MODE_SUCCESSOR,
            legacy_factory=lambda: calls.append("legacy") or legacy,
            successor_factory=lambda: calls.append("successor") or successor,
        )
        self.assertIs(selected, successor)
        self.assertIs(lifecycle, successor)
        self.assertEqual(calls, ["successor"])

        calls.clear()

        def fail_successor() -> object:
            calls.append("successor")
            raise SuccessorResponseConfigurationError("synthetic_failure")

        with self.assertRaises(SuccessorResponseConfigurationError):
            choose_response_memory_provider(
                mode=EXCLUSIVE_MODE_SUCCESSOR,
                legacy_factory=lambda: calls.append("legacy") or legacy,
                successor_factory=fail_successor,
            )
        self.assertEqual(calls, ["successor"])


class SuccessorResponseProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_ineligible_request_performs_zero_successor_io(self) -> None:
        selected = SuccessorGovernedMemoryAssemblyProviderV1(
            actor=actor(eligible=False),
            repository=ResourceTrap(),
            vector_index=ResourceTrap(),
            embedder=ResourceTrap(),
            predicate_catalog=PREDICATE_CATALOG,
            calibration=calibration(),
            clock=lambda: NOW,
        )
        assembly = await selected.prepare(
            authenticated_actor_user_id=OWNER_A,
            conversation_snapshot=snapshot(),
            trusted_policy_signals=ResponsePolicySignalsV0_2(),
        )
        self.assertIsNone(assembly.successor_memory_context_block)
        self.assertFalse(selected.has_selected_claims)
        with self.assertRaisesRegex(ContractViolation, "provider_reused"):
            await selected.prepare(
                authenticated_actor_user_id=OWNER_A,
                conversation_snapshot=snapshot(),
                trusted_policy_signals=ResponsePolicySignalsV0_2(),
            )

    async def test_qdrant_candidates_are_postgres_revalidated_before_prompt(self) -> None:
        repository = RecordingRepository()
        vector = RecordingVectorIndex()
        embedder = RecordingEmbedder()
        selected = provider(
            repository=repository,
            vector_index=vector,
            embedder=embedder,
        )
        assembly = await selected.prepare(
            authenticated_actor_user_id=OWNER_A,
            conversation_snapshot=snapshot(),
            trusted_policy_signals=ResponsePolicySignalsV0_2(),
        )
        block = assembly.successor_memory_context_block
        self.assertIsNotNone(block)
        assert block is not None
        self.assertEqual(block.block_id, "governed_memory_successor_v1")
        self.assertEqual(embedder.calls, [QUERY])
        self.assertEqual(len(vector.calls), 1)
        self.assertEqual(vector.calls[0]["owner_user_id"], OWNER_A)
        self.assertEqual(repository.reads[0][1], (CLAIM_A,))
        self.assertTrue(selected.has_selected_claims)

        escaped_memory = canonical_json_bytes(block.content)
        outbound = canonical_json_bytes(
            {
                "messages": [
                    {
                        "content": block.content,
                        "name": "governed_memory_successor_v1",
                        "role": "user",
                    }
                ]
            }
        )
        self.assertEqual(outbound.count(escaped_memory), 1)
        await selected.persist_dispatched_answer_binding(
            answer_id=RESPONSE_A,
            prompt_sha256="c" * 64,
            outbound_request_bytes=outbound,
        )
        self.assertFalse(selected.has_selected_claims)
        self.assertEqual(len(repository.persisted), 1)
        persisted = repository.persisted[0]
        self.assertEqual(persisted["thread_id"], THREAD_A)
        self.assertEqual(persisted["outbound_request"], outbound.decode("utf-8"))
        self.assertEqual(
            persisted["binding"]["selected_claim_ids"],
            (str(CLAIM_A),),
        )
        self.assertEqual(
            persisted["binding"]["injected_claim_ids"],
            (str(CLAIM_A),),
        )
        self.assertEqual(persisted["binding"]["dispatch_state"], "dispatched")
        self.assertEqual(persisted["binding"]["outcome"], "exposed")
        with self.assertRaisesRegex(ContractViolation, "selection_missing"):
            await selected.persist_dispatched_answer_binding(
                answer_id=RESPONSE_A,
                prompt_sha256="c" * 64,
                outbound_request_bytes=outbound,
            )

    async def test_stale_postgres_row_is_never_injected(self) -> None:
        repository = RecordingRepository(
            rows=(make_claim_row(lifecycle_state="retracted"),)
        )
        selected = provider(repository=repository)
        assembly = await selected.prepare(
            authenticated_actor_user_id=OWNER_A,
            conversation_snapshot=snapshot(),
            trusted_policy_signals=ResponsePolicySignalsV0_2(),
        )
        self.assertIsNone(assembly.successor_memory_context_block)
        self.assertFalse(selected.has_selected_claims)
        self.assertEqual(repository.persisted, [])

    async def test_cross_owner_postgres_row_fails_closed(self) -> None:
        repository = RecordingRepository(
            rows=(
                make_claim_row(
                    owner_user_id=UUID(
                        "22222222-2222-4222-8222-222222222222"
                    )
                ),
            )
        )
        selected = provider(repository=repository)
        with self.assertRaisesRegex(
            ContractViolation,
            "cross_owner_authoritative_row",
        ):
            await selected.prepare(
                authenticated_actor_user_id=OWNER_A,
                conversation_snapshot=snapshot(),
                trusted_policy_signals=ResponsePolicySignalsV0_2(),
            )
        self.assertFalse(selected.has_selected_claims)
        self.assertEqual(repository.persisted, [])

    async def test_binding_failure_clears_single_request_selection(self) -> None:
        repository = RecordingRepository(fail_persist=True)
        selected = provider(repository=repository)
        assembly = await selected.prepare(
            authenticated_actor_user_id=OWNER_A,
            conversation_snapshot=snapshot(),
            trusted_policy_signals=ResponsePolicySignalsV0_2(),
        )
        block = assembly.successor_memory_context_block
        assert block is not None
        outbound = canonical_json_bytes({"memory": block.content})
        with self.assertRaises(RuntimeError):
            await selected.persist_dispatched_answer_binding(
                answer_id=RESPONSE_A,
                prompt_sha256="d" * 64,
                outbound_request_bytes=outbound,
            )
        self.assertFalse(selected.has_selected_claims)


if __name__ == "__main__":
    unittest.main()
