from __future__ import annotations

import datetime as dt
import unittest
import uuid

from pydantic import ValidationError

from rag_engine.fm_selection_envelope_v0_2 import (
    FMSelectionRequestV02,
    select_fm_v0_2,
)
from rag_engine.lifeswitch_data_plan_v1 import create_lifeswitch_data_plan_v1
from rag_engine.lifeswitch_domain_context_v1 import (
    LifeSwitchContextSectionV1,
    TrustedLifeSwitchContextRequestV1,
    create_lifeswitch_context_envelope_v1,
    render_lifeswitch_context_v1,
)
from rag_engine.lifeswitch_prompt_integration_v1 import (
    ContextKindV2,
    LifeSwitchPromptAugmentationRequestV1,
    assemble_prompt_with_lifeswitch_v1,
)
from rag_engine.prompt_assembler_v1 import PromptAssemblyRequestV1, assemble_prompt
from tests.test_prompt_assembler_v1 import (
    assembly_request,
    governed_memory,
    policy_chain,
    prior_web_provenance,
)


ACTOR = uuid.UUID("11111111-1111-4111-8111-111111111111")
THREAD = uuid.UUID("22222222-2222-4222-8222-222222222222")
SNAPSHOT = "a" * 64
TODAY = dt.date(2026, 7, 29)


def lifeswitch_context(message: str, request_id: str = "request-123"):
    plan = create_lifeswitch_data_plan_v1(message, today=TODAY)
    request = TrustedLifeSwitchContextRequestV1.create(
        request_id=request_id,
        authenticated_actor_user_id=ACTOR,
        owner_user_id=ACTOR,
        thread_id=THREAD,
        conversation_snapshot_sha256=SNAPSHOT,
        owner_timezone="America/Chicago",
        query=message,
        data_plan=plan,
    )
    section = LifeSwitchContextSectionV1.create(
        projection="nutrition_day",
        status="AVAILABLE",
        window=plan.window,
        record_count=1,
        source_relations=("lifeswitch_nutrition.nutrition_day",),
        payload={
            "daily": [
                {
                    "date": "2026-07-27",
                    "calories": 2210.0,
                    "protein_g": 142.0,
                }
            ]
        },
    )
    envelope = create_lifeswitch_context_envelope_v1(
        request=request,
        plan_source="agentic_active",
        as_of_local_date=TODAY,
        sections=(section,),
        generated_at=dt.datetime(2026, 7, 29, 12, tzinfo=dt.timezone.utc),
    )
    return envelope, render_lifeswitch_context_v1(envelope)


def augment(base, message: str = "Was I low on protein Monday?"):
    envelope, rendered = lifeswitch_context(message)
    request = LifeSwitchPromptAugmentationRequestV1.create(
        trusted_thread_id=THREAD,
        conversation_snapshot_sha256=SNAPSHOT,
        base_assembly=base,
        lifeswitch_envelope=envelope,
        lifeswitch_rendered=rendered,
    )
    return assemble_prompt_with_lifeswitch_v1(request)


class LifeSwitchPromptIntegrationV1Tests(unittest.TestCase):
    def test_lifeswitch_is_inserted_as_lower_authority_reference_data(self) -> None:
        message = "Was I low on protein Monday?"
        base = assemble_prompt(assembly_request(message))

        result = augment(base, message)

        self.assertEqual(
            tuple(block.block_id for block in result.context_blocks),
            ("lifeswitch_domain_context_v1",),
        )
        self.assertIs(result.context_blocks[0].kind, ContextKindV2.LIFESWITCH)
        self.assertEqual(result.system_prompt, base.system_prompt)
        self.assertEqual(result.conversation, base.conversation)
        self.assertLessEqual(result.manifest.lifeswitch_estimated_tokens, 1000)

    def test_memory_precedes_lifeswitch(self) -> None:
        message = "Was I low on protein Monday?"
        memory_input, memory_application = governed_memory(message)
        request = assembly_request(message).model_copy(
            update={
                "memory_input": memory_input,
                "memory_application": memory_application,
            }
        )
        base = assemble_prompt(request)

        result = augment(base, message)

        self.assertEqual(
            tuple(block.block_id for block in result.context_blocks),
            ("governed_memory_v1", "lifeswitch_domain_context_v1"),
        )

    def test_lifeswitch_precedes_fm(self) -> None:
        message = "Explain Fractal Monism. Was I low on protein Monday?"
        policy_input, safety, signals, decision, prompt = policy_chain(message)
        fm = select_fm_v0_2(
            FMSelectionRequestV02(
                policy_decision=decision,
                query_text=message,
            )
        )
        base = assemble_prompt(
            PromptAssemblyRequestV1(
                policy_input=policy_input,
                safety_assessment=safety,
                policy_signals=signals,
                policy_decision=decision,
                policy_prompt=prompt,
                fm_selection=fm,
            )
        )

        result = augment(base, message)

        self.assertEqual(
            tuple(block.block_id for block in result.context_blocks),
            ("lifeswitch_domain_context_v1", "relational_monism_v0_4"),
        )

    def test_lifeswitch_precedes_web_provenance(self) -> None:
        message = "What sources did you use for my protein answer Monday?"
        base = assemble_prompt(
            assembly_request(message).model_copy(
                update={"prior_web_provenance": prior_web_provenance(message)}
            )
        )

        result = augment(base, message)

        self.assertEqual(
            tuple(block.block_id for block in result.context_blocks),
            ("lifeswitch_domain_context_v1", "prior_web_provenance_v1"),
        )

    def test_cross_thread_lifeswitch_context_fails_closed(self) -> None:
        message = "Was I low on protein Monday?"
        base = assemble_prompt(assembly_request(message))
        envelope, rendered = lifeswitch_context(message)

        with self.assertRaises(ValidationError):
            LifeSwitchPromptAugmentationRequestV1.create(
                trusted_thread_id=uuid.uuid4(),
                conversation_snapshot_sha256=SNAPSHOT,
                base_assembly=base,
                lifeswitch_envelope=envelope,
                lifeswitch_rendered=rendered,
            )

    def test_cross_snapshot_lifeswitch_context_fails_closed(self) -> None:
        message = "Was I low on protein Monday?"
        base = assemble_prompt(assembly_request(message))
        envelope, rendered = lifeswitch_context(message)

        with self.assertRaises(ValidationError):
            LifeSwitchPromptAugmentationRequestV1.create(
                trusted_thread_id=THREAD,
                conversation_snapshot_sha256="b" * 64,
                base_assembly=base,
                lifeswitch_envelope=envelope,
                lifeswitch_rendered=rendered,
            )

    def test_no_lifeswitch_context_preserves_v1_prompt(self) -> None:
        base = assemble_prompt(assembly_request())
        request = LifeSwitchPromptAugmentationRequestV1.create(
            trusted_thread_id=THREAD,
            conversation_snapshot_sha256=SNAPSHOT,
            base_assembly=base,
            lifeswitch_envelope=None,
            lifeswitch_rendered=None,
        )

        result = assemble_prompt_with_lifeswitch_v1(request)

        self.assertEqual(result.context_blocks, ())
        self.assertEqual(result.system_prompt, base.system_prompt)
        self.assertEqual(result.conversation, base.conversation)
        self.assertIsNone(result.manifest.lifeswitch_envelope_sha256)


if __name__ == "__main__":
    unittest.main()
