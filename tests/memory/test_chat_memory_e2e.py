"""Offline synthetic composition of the current successor contracts.

This is not a database, Qdrant, provider, HTTP, service, or production E2E test.
It composes the pure core with recording fakes so cross-layer drift is visible.
"""

from __future__ import annotations

from datetime import timedelta
import unittest
from uuid import UUID

from rag_engine.governed_memory.admission import apply_review
from rag_engine.governed_memory.auth import ActorRole, ActorScope, VerifiedActor
from rag_engine.governed_memory.contracts import ContractViolation, canonical_json_bytes
from rag_engine.governed_memory.extraction import (
    build_provider_request,
    validate_provider_result,
)
from rag_engine.governed_memory.lifecycle import apply_lifecycle_command
from rag_engine.governed_memory.projection import build_projection_point
from rag_engine.governed_memory.retrieval import (
    ANSWER_RENDERER_SHA256,
    RetrievalPolicy,
    build_answer_binding,
    mark_answer_binding_dispatched,
    render_memory_context,
    revalidate_candidates,
)
from rag_engine.governed_memory.worker import process_ingest_item
from tests.memory._fixtures import (
    CUTOVER,
    NOW,
    OWNER_A,
    OWNER_B,
    PREDICATE_CATALOG,
    RESPONSE_A,
    RecordingEmbedder,
    RecordingProvider,
    FakeVectorIndex,
    make_bridge_lease,
    make_candidate,
    make_extraction_job,
    make_ingest_payload,
    make_provider_output,
    make_selected_evidence,
    make_worker_actor,
)


MODEL = "synthetic-extraction-model"
SCHEMA = "governed-memory-extraction"
QUERY_SHA256 = "a" * 64
PROMPT_SHA256 = "d" * 64
RETRACTION_OPERATION = UUID("12121212-1212-4212-8212-121212121212")


def owner_actor(scope: ActorScope) -> VerifiedActor:
    return VerifiedActor(
        owner_user_id=OWNER_A,
        actor_id=OWNER_A,
        session_id=OWNER_A,
        role=ActorRole.OWNER,
        scopes=(scope,),
        authentication_manifest_sha256="a" * 64,
        authenticated_at=NOW,
    )


def retrieval_policy() -> RetrievalPolicy:
    return RetrievalPolicy(
        explicit_recall=False,
        allowed_predicates=("preference.personal",),
        max_records=8,
    )


class SyntheticChatMemoryFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_a_to_review_to_chat_b_then_retraction(self) -> None:
        ingest_payload = make_ingest_payload()
        ingest = process_ingest_item(
            ingest_payload,
            lease_envelope=make_bridge_lease(
                ingest_payload, ingest_after=CUTOVER
            ),
            actor=make_worker_actor(),
            expected_owner_user_id=OWNER_A,
            transaction_time=NOW,
        )
        self.assertEqual(ingest["decision"], "send_external")
        self.assertEqual(
            ingest["selected_evidence"]["selected_sha256"],
            make_selected_evidence()["selected_sha256"],
        )

        trusted_job = make_extraction_job()
        request = build_provider_request(
            trusted_job,
            make_selected_evidence(),
            MODEL,
            SCHEMA,
            PREDICATE_CATALOG,
        )
        provider = RecordingProvider(output=make_provider_output())
        provider_output = await provider.complete(request["external_payload"])
        batch = validate_provider_result(request, provider_output)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(batch["proposals"]), 1)
        self.assertNotIn("owner_user_id", batch["proposals"][0])

        proposal = {
            **batch["proposals"][0],
            **batch["proposal_hash_binding"],
            "projectable": True,
            "domains": [],
            "intents": [],
            "surface": "normal",
            "requires_explicit": False,
            "valid_from": None,
            "valid_to": None,
            "review_state": "pending_review",
            "expires_at": NOW + timedelta(hours=24),
        }
        self.assertEqual(proposal["owner_user_id"], trusted_job["owner_user_id"])
        review = apply_review(
            proposal,
            {
                "actor": owner_actor(ActorScope.REVIEW_PROPOSALS),
                "proposal_id": proposal["proposal_id"],
                "operation_id": proposal["operation_id"],
                "decision": "admit",
                "expected_proposal_sha256": proposal["proposal_sha256"],
                "expected_source_sha256": proposal["source_sha256"],
                "expected_selected_sha256": proposal["selected_sha256"],
                "expected_selection_binding_sha256": proposal[
                    "selection_binding_sha256"
                ],
                "expected_predicate_catalog_sha256": proposal[
                    "predicate_catalog_sha256"
                ],
                "reason_codes": ["explicit_owner_review"],
            },
            predicate_catalog=PREDICATE_CATALOG,
            transaction_time=NOW,
        )
        claim = review["claim"]
        self.assertEqual(claim["lifecycle_state"], "active")

        embedder = RecordingEmbedder()
        projection_text = claim["retrieval_text"]
        raw_vector = await embedder.embed(projection_text)
        outbox = {**review["projection"], "state": "claimed"}
        point = build_projection_point(claim, outbox, raw_vector)
        vector_index = FakeVectorIndex()
        await vector_index.upsert(
            point_id=UUID(point["point_id"]),
            vector=list(point["vector"]),
            payload=point["payload"],
        )
        self.assertEqual(len(vector_index.points), 1)

        payload = point["payload"]
        candidate = {
            "claim_id": payload["claim_id"],
            "owner_user_id": payload["owner_user_id"],
            "projection_contract_sha256": payload[
                "projection_contract_sha256"
            ],
            "projection_manifest_sha256": payload[
                "projection_manifest_sha256"
            ],
            "projection_operation_id": payload["projection_operation_id"],
            "projection_sequence": payload["projection_sequence"],
            "retrieval_text_sha256": payload["retrieval_text_sha256"],
            "revision_id": payload["revision_id"],
            "revision_number": payload["revision_number"],
            "revision_sha256": payload["revision_sha256"],
            "score": 0.91,
            "selection_binding_sha256": payload[
                "selection_binding_sha256"
            ],
        }
        vector_index.search_results = [candidate]
        query_vector = await embedder.embed("synthetic cobalt preference")
        search_results = await vector_index.search(
            owner_user_id=OWNER_A,
            vector=query_vector,
            limit=8,
        )
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(
            embedder.calls,
            [projection_text, "synthetic cobalt preference"],
        )
        self.assertEqual(vector_index.calls[0][0], "upsert")
        self.assertEqual(vector_index.calls[1][0], "search")

        answer_policy = retrieval_policy()
        selected = revalidate_candidates(
            OWNER_A,
            search_results,
            [claim],
            policy=answer_policy,
            predicate_catalog=PREDICATE_CATALOG,
            authorization_at=NOW,
        )
        rendered = render_memory_context(
            OWNER_A,
            selected,
            max_records=8,
            max_bytes=32_768,
        )
        prepared = build_answer_binding(
            owner_user_id=OWNER_A,
            response_id=RESPONSE_A,
            rendered_context=rendered,
            selected_claims=selected,
            query_sha256=QUERY_SHA256,
            policy=answer_policy,
            renderer_sha256=ANSWER_RENDERER_SHA256,
            prompt_sha256=PROMPT_SHA256,
        )
        self.assertEqual(prepared["model_exposed_count"], 0)
        escaped = canonical_json_bytes(rendered["content"])
        prefix = b'{"input":"synthetic question","memory":'
        outbound = prefix + escaped + b"}"
        binding = mark_answer_binding_dispatched(
            prepared,
            rendered,
            outbound_request_bytes=outbound,
            escaped_segment_start_utf8=len(prefix),
            escaped_segment_end_utf8=len(prefix) + len(escaped),
        )
        self.assertEqual(binding["outcome"], "exposed")
        self.assertEqual(binding["model_exposed_count"], 1)
        self.assertEqual(
            binding["binding_sha256"], binding["injection_manifest_sha256"]
        )
        self.assertFalse(binding["proves_semantic_use"])

        retraction = apply_lifecycle_command(
            claim,
            {
                "actor": owner_actor(ActorScope.MUTATE_CLAIMS),
                "operation_id": str(RETRACTION_OPERATION),
                "action": "retract",
                "expected_state_sha256": claim["state_sha256"],
                "expected_revision_sha256": claim["revision_sha256"],
            },
            predicate_catalog=PREDICATE_CATALOG,
            transaction_time=NOW,
        )
        await vector_index.delete(point_id=UUID(point["point_id"]))
        retracted = {
            **claim,
            "lifecycle_state": retraction["claim_lifecycle_state"],
            "projection_sequence": retraction["projection_sequence"],
            "updated_at": NOW,
            "state_sha256": retraction["claim_state_sha256"],
        }
        self.assertEqual(
            revalidate_candidates(
                OWNER_A,
                [candidate],
                [retracted],
                policy=retrieval_policy(),
                predicate_catalog=PREDICATE_CATALOG,
                authorization_at=NOW,
            ),
            (),
        )
        self.assertEqual(vector_index.points, {})

    async def test_candidate_authority_failure_degrades_to_content_free_no_memory(self) -> None:
        audit_receipt: dict[str, object]
        try:
            revalidate_candidates(
                OWNER_A,
                [make_candidate(owner_user_id=str(OWNER_B))],
                [],
                policy=retrieval_policy(),
                predicate_catalog=PREDICATE_CATALOG,
                authorization_at=NOW,
            )
        except ContractViolation as exc:
            selected: tuple[dict[str, object], ...] = ()
            audit_receipt = {
                "outcome": "ordinary_answer_without_memory",
                "reason_code": exc.code,
                "memory_selected": False,
            }
        else:
            self.fail("cross-owner candidate did not fail closed")

        rendered = render_memory_context(
            OWNER_A, selected, max_records=8, max_bytes=32_768
        )
        binding = build_answer_binding(
            owner_user_id=OWNER_A,
            response_id=RESPONSE_A,
            rendered_context=rendered,
            selected_claims=selected,
            query_sha256=QUERY_SHA256,
            policy=retrieval_policy(),
            renderer_sha256=ANSWER_RENDERER_SHA256,
            prompt_sha256=PROMPT_SHA256,
        )
        self.assertEqual(binding["outcome"], "no_memory_selected")
        self.assertEqual(binding["dispatch_state"], "not_applicable")
        self.assertEqual(binding["model_exposed_count"], 0)
        self.assertEqual(
            audit_receipt["reason_code"],
            "cross_owner_vector_candidate",
        )
        self.assertNotIn("content", audit_receipt)


if __name__ == "__main__":
    unittest.main()
