from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
from uuid import UUID

from rag_engine.governed_memory.contracts import (
    ContractViolation,
    canonical_json_bytes,
)
from rag_engine.governed_memory.response_contracts import (
    SuccessorResponseActorBinding,
)
from rag_engine.governed_memory.response_provider import (
    EXCLUSIVE_MODE_ENV,
    EXCLUSIVE_MODE_SUCCESSOR,
    InactiveSuccessorMemoryProviderV1,
    SuccessorGovernedMemoryAssemblyProviderV1,
    SuccessorResponseConfigurationError,
    choose_response_memory_provider,
    response_mode_from_environment,
)
from rag_engine.governed_memory.response_provenance import (
    SuccessorMemoryNotApplicableReason,
)
from rag_engine.governed_memory.exclusive_cutover import (
    ExclusiveMemoryMode,
    exclusive_memory_mode,
)
from rag_engine.governed_memory.runtime.calibration import CalibrationDecision
from tests.memory._fixtures import (
    CLAIM_A,
    NOW,
    OWNER_A,
    PREDICATE_CATALOG,
    RESPONSE_A,
    THREAD_A,
    RESPONSE_QUERY_A,
    deterministic_vector,
    make_response_actor,
    make_response_request,
    make_candidate,
    make_claim_row,
)


QUERY = RESPONSE_QUERY_A


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


@dataclass
class RecordingRelevanceGate:
    claim_ids: tuple[UUID, ...] | None = None
    fail: bool = False
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def relevant_claim_ids(self, **kwargs: Any) -> tuple[UUID, ...]:
        self.calls.append(dict(kwargs))
        if self.fail:
            raise RuntimeError("synthetic relevance failure")
        if self.claim_ids is not None:
            return self.claim_ids
        return tuple(UUID(str(row["claim_id"])) for row in kwargs["claims"])


class ResourceTrap:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"successor resource accessed: {name}")


def provider(
    *,
    binding: SuccessorResponseActorBinding | None = None,
    repository: RecordingRepository | None = None,
    vector_index: RecordingVectorIndex | None = None,
    embedder: RecordingEmbedder | None = None,
    relevance_gate: RecordingRelevanceGate | None = None,
    decision: CalibrationDecision | None = None,
) -> SuccessorGovernedMemoryAssemblyProviderV1:
    return SuccessorGovernedMemoryAssemblyProviderV1(
        actor=binding or make_response_actor(),
        repository=repository or RecordingRepository(),
        vector_index=vector_index or RecordingVectorIndex(),
        embedder=embedder or RecordingEmbedder(),
        relevance_gate=relevance_gate or RecordingRelevanceGate(),
        predicate_catalog=PREDICATE_CATALOG,
        calibration=decision or calibration(),
        clock=lambda: NOW,
        operation_id_factory=lambda: UUID(
            "34343434-3434-4434-8434-343434343434"
        ),
    )


class SuccessorResponseModeTests(unittest.TestCase):
    def test_missing_mode_fails_closed_and_successor_text_is_exact(self) -> None:
        with self.assertRaises(SuccessorResponseConfigurationError):
            response_mode_from_environment({})
        self.assertEqual(
            response_mode_from_environment(
                {EXCLUSIVE_MODE_ENV: EXCLUSIVE_MODE_SUCCESSOR}
            ),
            EXCLUSIVE_MODE_SUCCESSOR,
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
        successor = object()
        selected, lifecycle = choose_response_memory_provider(
            mode=EXCLUSIVE_MODE_SUCCESSOR,
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
                successor_factory=fail_successor,
            )
        self.assertEqual(calls, ["successor"])


class SuccessorResponseProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_ineligible_real_provider_refuses_with_zero_successor_io(self) -> None:
        selected = SuccessorGovernedMemoryAssemblyProviderV1(
            actor=make_response_actor(eligible=False),
            repository=ResourceTrap(),
            vector_index=ResourceTrap(),
            embedder=ResourceTrap(),
            relevance_gate=ResourceTrap(),
            predicate_catalog=PREDICATE_CATALOG,
            calibration=calibration(),
            clock=lambda: NOW,
        )
        with self.assertRaisesRegex(
            ContractViolation,
            "ineligible_successor_response_provider",
        ):
            await selected.prepare(
                request=make_response_request(),
            )
        self.assertFalse(selected.has_selected_claims)
        with self.assertRaisesRegex(ContractViolation, "provider_reused"):
            await selected.prepare(
                request=make_response_request(),
            )

    async def test_inactive_exclusion_emits_not_applicable_without_resources(
        self,
    ) -> None:
        selected = InactiveSuccessorMemoryProviderV1(
            SuccessorMemoryNotApplicableReason.ATTACHMENT
        )
        assembly = selected.prepare(
            request=make_response_request(),
        )
        self.assertIsNone(assembly.successor_memory_context_block)
        receipt = await selected.persist_dispatched_answer_binding(
            answer_id=RESPONSE_A,
            prompt_sha256="c" * 64,
            outbound_request_bytes=b'{"messages":[]}',
        )
        self.assertEqual(receipt.binding_outcome, "not_applicable")
        self.assertIs(
            receipt.not_applicable_reason,
            SuccessorMemoryNotApplicableReason.ATTACHMENT,
        )
        self.assertEqual(receipt.references, ())

    async def test_qdrant_candidates_are_postgres_revalidated_before_prompt(self) -> None:
        repository = RecordingRepository()
        vector = RecordingVectorIndex()
        embedder = RecordingEmbedder()
        relevance_gate = RecordingRelevanceGate()
        selected = provider(
            repository=repository,
            vector_index=vector,
            embedder=embedder,
            relevance_gate=relevance_gate,
        )
        assembly = await selected.prepare(
            request=make_response_request(),
        )
        block = assembly.successor_memory_context_block
        self.assertIsNotNone(block)
        assert block is not None
        self.assertEqual(block.block_id, "governed_memory_successor_v1")
        self.assertEqual(embedder.calls, [QUERY])
        self.assertEqual(len(vector.calls), 1)
        self.assertEqual(vector.calls[0]["owner_user_id"], OWNER_A)
        self.assertTrue(vector.calls[0]["explicit_recall"])
        self.assertEqual(
            vector.calls[0]["allowed_predicates"],
            ("preference.personal",),
        )
        self.assertEqual(repository.reads[0][1], (CLAIM_A,))
        self.assertEqual(len(relevance_gate.calls), 1)
        self.assertEqual(relevance_gate.calls[0]["query"], QUERY)
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
        provenance = await selected.persist_dispatched_answer_binding(
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
        self.assertTrue(persisted["binding"]["explicit_recall"])
        self.assertEqual(provenance.binding_outcome, "exposed")
        self.assertEqual(
            provenance.binding_manifest_sha256,
            persisted["binding"]["binding_sha256"],
        )
        self.assertEqual(provenance.references[0].claim_id, CLAIM_A)
        with self.assertRaisesRegex(ContractViolation, "binding_terminal"):
            await selected.persist_dispatched_answer_binding(
                answer_id=RESPONSE_A,
                prompt_sha256="c" * 64,
                outbound_request_bytes=outbound,
            )

    async def test_bounded_preference_recall_detection(self) -> None:
        for query, expected in (
            ("What is my preferred test animal?", True),
            ("Which synthetic interface theme do I prefer?", True),
            ("Do you remember my favorite color?", True),
            ("What is my go-to natural-memory test tea?", True),
            ("Which should I prefer, cobalt or amber?", False),
            ("Compare my preferences with this plan.", False),
        ):
            vector = RecordingVectorIndex()
            selected = provider(vector_index=vector)
            with self.subTest(query=query):
                await selected.prepare(
                    request=make_response_request(current_message=query),
                )
                self.assertEqual(
                    vector.calls[0]["explicit_recall"],
                    expected,
                )
                self.assertEqual(
                    vector.calls[0]["allowed_predicates"],
                    (
                        ("preference.personal",)
                        if expected
                        else tuple(sorted(PREDICATE_CATALOG["predicates"]))
                    ),
                )
                self.assertEqual(vector.calls[0]["limit"], 1 if expected else 8)

    async def test_bounded_personal_recall_detection(self) -> None:
        allowed = tuple(sorted(PREDICATE_CATALOG["predicates"]))
        for query, expected in (
            ("Who is my father?", True),
            ("What do I collect?", True),
            ("Can you recall what I collect?", True),
            ("What instrument do I prefer?", True),
            ("Which alternatives was I considering?", True),
            ("What should I do about my project?", False),
            ("What instrument should I use for my project?", False),
            ("Which could I use for my project?", False),
        ):
            vector = RecordingVectorIndex()
            selected = provider(vector_index=vector)
            with self.subTest(query=query):
                await selected.prepare(
                    request=make_response_request(current_message=query),
                )
                self.assertEqual(vector.calls[0]["explicit_recall"], expected)
                self.assertEqual(vector.calls[0]["allowed_predicates"], allowed)

    async def test_compound_preference_and_considered_recall_uses_full_policy(
        self,
    ) -> None:
        allowed = tuple(sorted(PREDICATE_CATALOG["predicates"]))
        for query in (
            (
                "What is my current preferred memory-validation instrument, "
                "what future instruments am I considering, and what model "
                "directions am I weighing?"
            ),
            (
                "What instrument do I prefer, and which alternatives was I "
                "considering?"
            ),
            (
                "What is my preferred instrument and what other directions "
                "am I weighing?"
            ),
        ):
            vector = RecordingVectorIndex()
            selected = provider(vector_index=vector)
            with self.subTest(query=query):
                await selected.prepare(
                    request=make_response_request(current_message=query),
                )
                self.assertTrue(vector.calls[0]["explicit_recall"])
                self.assertEqual(vector.calls[0]["allowed_predicates"], allowed)
                self.assertEqual(vector.calls[0]["limit"], 8)

    async def test_no_selection_returns_typed_receipt_without_persistence(self) -> None:
        repository = RecordingRepository()
        selected = provider(
            repository=repository,
            vector_index=RecordingVectorIndex(candidates=()),
        )
        assembly = await selected.prepare(
            request=make_response_request(),
        )
        self.assertIsNone(assembly.successor_memory_context_block)
        provenance = await selected.persist_dispatched_answer_binding(
            answer_id=RESPONSE_A,
            prompt_sha256="c" * 64,
            outbound_request_bytes=b'{"messages":[]}',
        )
        self.assertEqual(provenance.binding_outcome, "no_memory_selected")
        self.assertEqual(provenance.references, ())
        self.assertEqual(repository.persisted, [])

    async def test_language_relevance_rejection_exposes_no_memory(self) -> None:
        repository = RecordingRepository()
        relevance_gate = RecordingRelevanceGate(claim_ids=())
        selected = provider(
            repository=repository,
            relevance_gate=relevance_gate,
        )
        assembly = await selected.prepare(request=make_response_request())
        self.assertIsNone(assembly.successor_memory_context_block)
        self.assertFalse(selected.has_selected_claims)
        self.assertEqual(repository.persisted, [])
        self.assertEqual(len(relevance_gate.calls), 1)

    async def test_language_relevance_failure_or_fabrication_fails_closed(
        self,
    ) -> None:
        gates = (
            RecordingRelevanceGate(fail=True),
            RecordingRelevanceGate(
                claim_ids=(UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),)
            ),
        )
        for relevance_gate in gates:
            repository = RecordingRepository()
            selected = provider(
                repository=repository,
                relevance_gate=relevance_gate,
            )
            with self.subTest(gate=relevance_gate):
                assembly = await selected.prepare(
                    request=make_response_request()
                )
                self.assertIsNone(assembly.successor_memory_context_block)
                self.assertFalse(selected.has_selected_claims)
                self.assertEqual(repository.persisted, [])

    async def test_stale_postgres_row_is_never_injected(self) -> None:
        repository = RecordingRepository(
            rows=(make_claim_row(lifecycle_state="retracted"),)
        )
        selected = provider(repository=repository)
        assembly = await selected.prepare(
            request=make_response_request(),
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
                request=make_response_request(),
            )
        self.assertFalse(selected.has_selected_claims)
        self.assertEqual(repository.persisted, [])

    async def test_binding_failure_clears_single_request_selection(self) -> None:
        repository = RecordingRepository(fail_persist=True)
        selected = provider(repository=repository)
        assembly = await selected.prepare(
            request=make_response_request(),
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
