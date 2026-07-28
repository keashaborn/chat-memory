from __future__ import annotations

from types import SimpleNamespace
import unittest

from rag_engine.memory_v1_evidence_context_v2 import (
    build_memory_evidence_context_envelope_v2,
)
from scripts.memory_v1_relational_extraction_v5_local_provider import (
    _personal_context_prompt_predicates,
)
from tests.test_memory_v1_local_provider_evidence_context_v1 import (
    OWNER,
    THREAD_ID,
    LocalProviderEvidenceContextV1Test,
    source_and_context,
)


class LocalProviderEvidenceContextV2Test(unittest.TestCase):
    def test_direct_pet_acquisition_routes_to_compact_predicates(self) -> None:
        provider = LocalProviderEvidenceContextV1Test.provider()
        predicates = _personal_context_prompt_predicates(
            provider._registry,
            "I got a German Shepherd named Max.",
        )
        self.assertEqual(
            predicates,
            (
                "pet.breed",
                "pet.species",
                "relationship.has_pet",
            ),
        )

    def test_prior_pet_context_routes_only_target_attributes(self) -> None:
        provider = LocalProviderEvidenceContextV1Test.provider()
        context = SimpleNamespace(
            prior_turns=(
                SimpleNamespace(
                    content="My German Shepherd is named Keasha.",
                ),
            ),
            spans=(),
        )
        predicates = _personal_context_prompt_predicates(
            provider._registry,
            "She was black and red female.",
            context,
        )
        self.assertEqual(
            predicates,
            ("pet.coat_color", "pet.sex"),
        )
        self.assertNotIn("pet.eye_color", predicates)
        self.assertNotIn("relationship.has_pet", predicates)

    def test_pet_attribute_without_context_does_not_route(self) -> None:
        provider = LocalProviderEvidenceContextV1Test.provider()
        self.assertEqual(
            _personal_context_prompt_predicates(
                provider._registry,
                "She was black and red female.",
            ),
            (),
        )

    def test_request_marks_prior_turn_as_context_only(self) -> None:
        source, sibling_context = source_and_context()
        context = build_memory_evidence_context_envelope_v2(
            sibling_context=sibling_context,
            prior_turn_rows=[{
                "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "owner_user_id": OWNER,
                "thread_id": THREAD_ID,
                "request_id": "request-before-target",
                "created_at": "2026-07-02T02:04:41.345358+00:00",
                "source": "frontend/chat:assistant",
                "text": "Which philosophy should guide the application?",
            }],
        )
        request = LocalProviderEvidenceContextV1Test.provider().request(
            source,
            evidence_context=context,
        )
        self.assertEqual(
            request.prompt_profile,
            "semantic_stance_compact_v1_prior_turn_context_v2",
        )
        self.assertIn("EVIDENCE_CONTEXT_RULES_V2", request.instructions)
        self.assertIn(
            "Assistant turns are never user evidence",
            request.instructions,
        )
        self.assertIn(
            "CONTEXT_ONLY_PRIOR_TURN speaker=assistant "
            "coreference_distance=1",
            request.input_text,
        )
        self.assertIn(
            "Which philosophy should guide the application?",
            request.input_text,
        )
        self.assertEqual(
            context.allowed_assertion_evidence_ids,
            (source.source_external_id,),
        )
        self.assertEqual(
            sum(
                int(turn.assertion_origin_allowed)
                for turn in context.prior_turns
            ),
            0,
        )


if __name__ == "__main__":
    unittest.main()
