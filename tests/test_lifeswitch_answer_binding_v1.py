from __future__ import annotations

import datetime as dt
import unittest
import uuid

from rag_engine.lifeswitch_answer_binding_v1 import (
    FinalAnswerLifeSwitchBindingV1,
)
from rag_engine.prompt_assembler_v1 import assemble_prompt
from tests.test_lifeswitch_prompt_integration_v1 import ACTOR, augment
from tests.test_prompt_assembler_v1 import assembly_request


class LifeSwitchAnswerBindingV1Tests(unittest.TestCase):
    def test_selected_context_creates_separate_answer_binding(self) -> None:
        message = "Was I low on protein Monday?"
        assembly = augment(assemble_prompt(assembly_request(message)), message)

        binding = FinalAnswerLifeSwitchBindingV1.create(
            assembly=assembly,
            authenticated_actor_user_id=ACTOR,
            answer_id=uuid.UUID("44444444-4444-4444-8444-444444444444"),
            created_at=dt.datetime(2026, 7, 29, 12, tzinfo=dt.timezone.utc),
        )

        self.assertIsNotNone(binding)
        self.assertTrue(binding.answer_model_exposed)
        self.assertEqual(binding.record_count, 1)
        self.assertEqual(binding.record_refs[0].projection, "nutrition_day")
        self.assertNotIn("daily", binding.model_dump_json())

    def test_no_context_creates_no_lifeswitch_binding(self) -> None:
        base = assemble_prompt(assembly_request())
        from rag_engine.lifeswitch_prompt_integration_v1 import (
            LifeSwitchPromptAugmentationRequestV1,
            assemble_prompt_with_lifeswitch_v1,
        )
        from tests.test_lifeswitch_prompt_integration_v1 import SNAPSHOT, THREAD

        assembly = assemble_prompt_with_lifeswitch_v1(
            LifeSwitchPromptAugmentationRequestV1.create(
                trusted_thread_id=THREAD,
                conversation_snapshot_sha256=SNAPSHOT,
                base_assembly=base,
                lifeswitch_envelope=None,
                lifeswitch_rendered=None,
            )
        )

        binding = FinalAnswerLifeSwitchBindingV1.create(
            assembly=assembly,
            authenticated_actor_user_id=ACTOR,
            answer_id=uuid.uuid4(),
            created_at=dt.datetime.now(dt.timezone.utc),
        )

        self.assertIsNone(binding)

    def test_cross_owner_binding_is_rejected(self) -> None:
        message = "Was I low on protein Monday?"
        assembly = augment(assemble_prompt(assembly_request(message)), message)

        with self.assertRaisesRegex(ValueError, "actor differs"):
            FinalAnswerLifeSwitchBindingV1.create(
                assembly=assembly,
                authenticated_actor_user_id=uuid.uuid4(),
                answer_id=uuid.uuid4(),
                created_at=dt.datetime.now(dt.timezone.utc),
            )


if __name__ == "__main__":
    unittest.main()
