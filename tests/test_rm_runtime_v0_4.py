from __future__ import annotations

import hashlib
import unittest

from seebx.capabilities.conversation.prompt import (
    ContextKind,
    PromptAssemblyRequestV1,
    assemble_prompt,
)
from rag_engine.response_policy_prompt_v0_2 import render_response_policy_prompt_v0_2
from seebx.capabilities.conversation.policy import (
    ConversationRole,
    FMLevel,
    GateState,
    ResponseMode,
    ResponsePolicyConversationMessageV0_2,
    ResponsePolicyInputV0_2,
    ResponsePolicySignalsV0_2,
    SafetyAssessmentV0_2,
    decide_response_policy_v0_2,
)
from rag_engine.response_source_awareness_v1 import MemorySourceStatusV1
from rag_engine.rm_selection_envelope_v0_4 import (
    ACTIVE_PHILOSOPHY_ID,
    CANONICAL_MANIFEST_SHA256,
    DEFAULT_PROMPT_PATH,
    RMSelectionRequestV04,
    RUNTIME_PROMPT_SHA256,
    load_runtime_prompt_v0_4,
    select_rm_v0_4,
)
from tests.test_prompt_assembler_v1 import zep_memory_block


def chain(
    message: str,
    *,
    signals: ResponsePolicySignalsV0_2 | None = None,
    safety_gate: GateState = GateState.PASS,
):
    policy_input = ResponsePolicyInputV0_2.create(
        request_id="rm-v0-4-focused-test",
        conversation=(
            ResponsePolicyConversationMessageV0_2(
                role=ConversationRole.USER,
                content=message,
            ),
        ),
    )
    safety = SafetyAssessmentV0_2.create(
        policy_input,
        high_stakes_gate=safety_gate,
        safety_action_required=safety_gate is not GateState.PASS,
        reason_codes=("focused_test",) if safety_gate is not GateState.PASS else (),
    )
    trusted = signals or ResponsePolicySignalsV0_2()
    decision = decide_response_policy_v0_2(
        policy_input,
        safety_assessment=safety,
        signals=trusted,
    )
    selection = select_rm_v0_4(
        RMSelectionRequestV04(
            policy_decision=decision,
            query_text=message,
        )
    )
    return policy_input, safety, trusted, decision, selection


class RMRuntimeV04Test(unittest.TestCase):
    def test_canonical_prompt_is_exact_and_selected_for_explicit_rm(self) -> None:
        raw = DEFAULT_PROMPT_PATH.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), RUNTIME_PROMPT_SHA256)
        prompt = load_runtime_prompt_v0_4()
        self.assertEqual(prompt.encode("utf-8"), raw)

        policy_input, safety, signals, decision, selection = chain(
            "Explain Relational Monism in plain language."
        )
        self.assertEqual(decision.response_mode, ResponseMode.FM_EXPLICIT)
        self.assertEqual(selection.status, "SELECTED")
        self.assertEqual(selection.active_philosophy_id, ACTIVE_PHILOSOPHY_ID)
        self.assertEqual(selection.canonical_manifest_sha256, CANONICAL_MANIFEST_SHA256)
        self.assertEqual(selection.compact_content(), prompt)

        assembled = assemble_prompt(
            PromptAssemblyRequestV1(
                policy_input=policy_input,
                safety_assessment=safety,
                policy_signals=signals,
                policy_decision=decision,
                policy_prompt=render_response_policy_prompt_v0_2(decision),
                fm_selection=selection,
            )
        )
        self.assertEqual(len(assembled.context_blocks), 1)
        block = assembled.context_blocks[0]
        self.assertEqual(block.kind, ContextKind.RELATIONAL_MONISM)
        self.assertEqual(block.block_id, ACTIVE_PHILOSOPHY_ID)
        self.assertNotIn("Fractal Monism v0.2", block.content)

    def test_high_stakes_and_technical_arbitration_keep_rm_out(self) -> None:
        for message, signals, gate, expected_mode in (
            (
                "Explain Relational Monism while I have crushing chest pain.",
                ResponsePolicySignalsV0_2(fm_explicit=True),
                GateState.TRIGGERED,
                ResponseMode.HIGH_STAKES,
            ),
            (
                "Implement a Python Relational Monism router.",
                ResponsePolicySignalsV0_2(technical=True, fm_explicit=True),
                GateState.PASS,
                ResponseMode.TECHNICAL,
            ),
        ):
            with self.subTest(mode=expected_mode):
                policy_input, safety, trusted, decision, selection = chain(
                    message,
                    signals=signals,
                    safety_gate=gate,
                )
                self.assertEqual(decision.response_mode, expected_mode)
                self.assertEqual(decision.fm_effective_level, FMLevel.OFF)
                self.assertEqual(selection.status, "OFF")
                assembled = assemble_prompt(
                    PromptAssemblyRequestV1(
                        policy_input=policy_input,
                        safety_assessment=safety,
                        policy_signals=trusted,
                        policy_decision=decision,
                        policy_prompt=render_response_policy_prompt_v0_2(decision),
                        fm_selection=selection,
                    )
                )
                self.assertFalse(
                    any(
                        block.kind is ContextKind.RELATIONAL_MONISM
                        for block in assembled.context_blocks
                    )
                )

    def test_rm_and_zep_memory_remain_separate_context_blocks(self) -> None:
        message = "Use Relational Monism to explain the relevant project constraint."
        policy_input, safety, signals, decision, selection = chain(message)
        memory = zep_memory_block(
            message,
            request_id="rm-v0-4-focused-test",
        )
        assembled = assemble_prompt(
            PromptAssemblyRequestV1(
                policy_input=policy_input,
                safety_assessment=safety,
                policy_signals=signals,
                policy_decision=decision,
                policy_prompt=render_response_policy_prompt_v0_2(decision),
                successor_memory_context_block=memory,
                memory_source_status=MemorySourceStatusV1.SELECTED,
                fm_selection=selection,
            )
        )
        self.assertEqual(
            tuple(block.block_id for block in assembled.context_blocks),
            ("zep_memory_v1", ACTIVE_PHILOSOPHY_ID),
        )
        self.assertEqual(assembled.context_blocks[0].content, memory.content)


if __name__ == "__main__":
    unittest.main()
