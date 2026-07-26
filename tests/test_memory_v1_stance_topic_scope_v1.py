from __future__ import annotations

import unittest
from datetime import datetime, timezone
from uuid import UUID

from rag_engine.memory_v1_selection_envelope import (
    BUDGET_POLICY_VERSION,
    TOKEN_ESTIMATOR_VERSION,
    MemoryLane,
    MemoryLaneLimitV1,
    MemorySelectionBudgetPolicyV1,
    MemorySelectionRequestV1,
    QueryEmbeddingArtifactV1,
    QueryEmbeddingSource,
    SelectionDirective,
    Sensitivity,
    SourceContractVersionV1,
)
from rag_engine.memory_v1_stance_topic_scope_v1 import (
    claim_row_matches_stance_topic_scope_v1,
    resolve_stance_topic_scope_v1,
    stance_topic_tokens,
)


OWNER = UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
OTHER = UUID("557ea042-cb82-48f8-9429-472e96c957ef")
TRACE = UUID("10000000-0000-4000-8000-000000000001")
THREAD = UUID("d776c8ef-7f3d-45b2-8820-4be87b7ca19d")
SOURCE = SourceContractVersionV1(
    name="claim_projection",
    version="memory_projection_v5",
)


def request(query: str) -> MemorySelectionRequestV1:
    vector = (0.125, -0.25, 0.5)
    lane = MemoryLaneLimitV1(
        lane=MemoryLane.CLAIM,
        max_records=4,
        max_tokens=600,
    )
    return MemorySelectionRequestV1.create(
        intent_adapter_version="memory_intent_adapter_test_v1",
        source_contract_versions=(SOURCE,),
        selection_trace_id=TRACE,
        authenticated_actor_user_id=OWNER,
        owner_user_id=OWNER,
        request_id="stance-topic-scope-test",
        thread_id=THREAD,
        query_text=query,
        query_vector=vector,
        query_embedding=QueryEmbeddingArtifactV1.from_vector(
            source=QueryEmbeddingSource.PRIVATE_LOCAL,
            model_version="test",
            vector=vector,
        ),
        memory_intent="personal_recall",
        domains=("stance_recall",),
        requested_lanes=(MemoryLane.CLAIM,),
        selection_directive=SelectionDirective.EVALUATE,
        explicit_recall=True,
        project_key=None,
        component_key=None,
        max_sensitivity=Sensitivity.MEDIUM,
        selected_at=datetime(2026, 7, 26, tzinfo=timezone.utc),
        budget_policy=MemorySelectionBudgetPolicyV1(
            policy_version=BUDGET_POLICY_VERSION,
            token_estimator_version=TOKEN_ESTIMATOR_VERSION,
            max_records=4,
            max_tokens=600,
            max_controls=4,
            lane_limits=(lane,),
        ),
    )


def row(
    *,
    owner: UUID = OWNER,
    topic_key: str,
    topic_text: str,
    position: str,
) -> dict[str, object]:
    return {
        "owner_user_id": owner,
        "predicate": "stance.reported",
        "canonical_text": f'The user reports this position: "{position}"',
        "object_literal": {
            "kind": "literal",
            "datatype": "json",
            "value": {
                "topic_key": topic_key,
                "topic_text": topic_text,
                "position": position,
                "orientation": "supports",
            },
        },
    }


class StanceTopicScopeV1Test(unittest.TestCase):
    def claim_context(self) -> dict[str, object]:
        return {
            "domain": "stance_recall",
            "allowed_predicates": ["stance.reported"],
        }

    def test_specific_topic_extracts_only_meaningful_terms(self) -> None:
        self.assertEqual(
            stance_topic_tokens(
                "What have I said about how Fractal Monism can help people?"
            ),
            ("fractal", "help", "monism", "people"),
        )

    def test_specific_topic_accepts_matching_stance_and_rejects_unrelated_stance(
        self,
    ) -> None:
        scope = resolve_stance_topic_scope_v1(
            request=request(
                "What have I said about how Fractal Monism can help people?"
            ),
            claim_context=self.claim_context(),
        )
        assert scope is not None
        self.assertFalse(scope.broad_recall)
        self.assertEqual(scope.minimum_overlap, 2)
        self.assertTrue(
            claim_row_matches_stance_topic_scope_v1(
                row(
                    topic_key="fractal_monism.application",
                    topic_text="Fractal Monism",
                    position="Fractal Monism will help people in life",
                ),
                scope,
            )
        )
        self.assertFalse(
            claim_row_matches_stance_topic_scope_v1(
                row(
                    topic_key="epistemology.future_acceptance",
                    topic_text="future acceptance",
                    position="Worrying about what happens next month does not matter",
                ),
                scope,
            )
        )

    def test_future_topic_accepts_future_stance(self) -> None:
        scope = resolve_stance_topic_scope_v1(
            request=request(
                "What have I said about worrying about the future?"
            ),
            claim_context=self.claim_context(),
        )
        assert scope is not None
        self.assertTrue(
            claim_row_matches_stance_topic_scope_v1(
                row(
                    topic_key="epistemology.future_acceptance",
                    topic_text="future acceptance",
                    position="Worrying about the future does not help",
                ),
                scope,
            )
        )

    def test_broad_opinion_recall_keeps_all_owner_stances(self) -> None:
        scope = resolve_stance_topic_scope_v1(
            request=request("What are some opinions I have shared?"),
            claim_context=self.claim_context(),
        )
        assert scope is not None
        self.assertTrue(scope.broad_recall)
        self.assertTrue(
            claim_row_matches_stance_topic_scope_v1(
                row(
                    topic_key="epistemology.future_acceptance",
                    topic_text="future acceptance",
                    position="Worrying about the future does not help",
                ),
                scope,
            )
        )

    def test_cross_owner_row_fails_closed(self) -> None:
        scope = resolve_stance_topic_scope_v1(
            request=request("What have I said about evidence?"),
            claim_context=self.claim_context(),
        )
        assert scope is not None
        with self.assertRaisesRegex(RuntimeError, "owner boundary"):
            claim_row_matches_stance_topic_scope_v1(
                row(
                    owner=OTHER,
                    topic_key="evidence.truth",
                    topic_text="evidence",
                    position="Evidence supports or weakens claims",
                ),
                scope,
            )


if __name__ == "__main__":
    unittest.main()
