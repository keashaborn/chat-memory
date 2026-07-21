from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import unittest

from pydantic import ValidationError

from rag_engine.fm_runtime_bundle_v0_2 import canonical_sha256, load_runtime_bundle_v0_2
from rag_engine.fm_selection_envelope_v0_2 import (
    COACHING_PRIMARY_IDS,
    FMSelectionContractError,
    FMSelectionEnvelopeV02,
    FMSelectionRequestV02,
    FM_EXPLICIT_OVERVIEW_IDS,
    MODE_MAX_RECORDS,
    ORDINARY_LIGHT_PRIMARY_IDS,
    select_fm_v0_2,
)
from rag_engine.response_policy_v0_2 import (
    ConversationRole,
    FMLevel,
    GateState,
    ResponsePolicyConversationMessageV0_2,
    ResponsePolicyInputV0_2,
    ResponsePolicySignalsV0_2,
    SafetyAssessmentV0_2,
    decide_response_policy_v0_2,
)


ROOT = Path(__file__).resolve().parents[1]
BUNDLE_PATH = ROOT / "rag_engine" / "data" / "fm_v0_2_runtime_bundle.json"
SELECTOR_MODULE = ROOT / "rag_engine" / "fm_selection_envelope_v0_2.py"


def request(
    *,
    query_text: str = "What is Fractal Monism?",
    signals: ResponsePolicySignalsV0_2 | None = None,
    token_budget: int | None = None,
) -> FMSelectionRequestV02:
    policy_input = ResponsePolicyInputV0_2.create(
        request_id="fm-selection-test-request",
        conversation=(
            ResponsePolicyConversationMessageV0_2(
                role=ConversationRole.USER,
                content=query_text,
            ),
        ),
    )
    decision = decide_response_policy_v0_2(
        policy_input,
        safety_assessment=SafetyAssessmentV0_2.create(policy_input),
        signals=signals,
    )
    return FMSelectionRequestV02(
        policy_decision=decision,
        query_text=query_text,
        token_budget=token_budget,
    )


class FMSelectionEnvelopeV02Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = load_runtime_bundle_v0_2(BUNDLE_PATH)

    def select(
        self,
        *,
        query_text: str = "What is Fractal Monism?",
        signals: ResponsePolicySignalsV0_2 | None = None,
        token_budget: int | None = None,
    ) -> FMSelectionEnvelopeV02:
        return select_fm_v0_2(
            request(
                query_text=query_text,
                signals=signals,
                token_budget=token_budget,
            ),
            self.bundle,
        )

    def test_high_stakes_is_empty_off_even_when_explicit_is_requested(self) -> None:
        envelope = self.select(
            query_text="Explain Fractal Monism, but I may kill myself tonight.",
        )
        self.assertEqual(envelope.status, "OFF")
        self.assertEqual(envelope.fm_level, "OFF")
        self.assertEqual(envelope.reason_codes, ("high_stakes_off",))
        self.assertEqual(envelope.selected_record_ids, ())
        self.assertEqual(envelope.used_tokens, 0)
        self.assertEqual(envelope.compact_content(self.bundle), "")

    def test_selector_has_no_live_retrieval_database_or_provider_dependency(self) -> None:
        tree = ast.parse(SELECTOR_MODULE.read_text(encoding="utf-8"))
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)
        forbidden_roots = {
            "asyncpg",
            "httpx",
            "openai",
            "psycopg",
            "qdrant_client",
            "requests",
            "socket",
            "sqlalchemy",
            "subprocess",
            "urllib",
        }
        self.assertTrue(
            {name.split(".")[0] for name in imports}.isdisjoint(forbidden_roots),
            imports,
        )
        self.assertNotIn("rag_engine.prompt_contribution_v1", imports)

    def test_technical_is_empty_off_even_when_query_names_fm(self) -> None:
        envelope = self.select(
            query_text="Implement the Fractal Monism selector in Python.",
        )
        self.assertEqual(envelope.status, "OFF")
        self.assertEqual(envelope.reason_codes, ("technical_off",))
        self.assertEqual(envelope.max_records, 0)

    def test_application_gate_and_opt_out_each_fail_closed(self) -> None:
        triggered = self.select(
            signals=ResponsePolicySignalsV0_2(
                fm_explicit=True,
                fm_application_gate=GateState.TRIGGERED,
            )
        )
        uncertain = self.select(
            signals=ResponsePolicySignalsV0_2(
                fm_explicit=True,
                fm_application_gate=GateState.UNCERTAIN,
            )
        )
        opted_out = self.select(
            signals=ResponsePolicySignalsV0_2(
                fm_explicit=True,
                user_fm_opt_out=True,
            )
        )
        self.assertEqual(triggered.reason_codes, ("application_gate_triggered",))
        self.assertEqual(uncertain.reason_codes, ("application_gate_uncertain",))
        self.assertEqual(opted_out.reason_codes, ("user_opt_out",))
        self.assertTrue(all(item.status == "OFF" for item in (triggered, uncertain, opted_out)))

    def test_generic_explicit_query_has_fixed_canonical_overview(self) -> None:
        envelope = self.select(query_text="Give me an overview of Fractal Monism.")
        self.assertEqual(envelope.status, "SELECTED")
        self.assertEqual(envelope.selected_record_ids, FM_EXPLICIT_OVERVIEW_IDS)
        self.assertEqual(len(envelope.selected_record_ids), MODE_MAX_RECORDS["FM_EXPLICIT"])
        self.assertIn("FM-AG-001", envelope.application_boundary_ids)
        self.assertIn("FM-C-034", envelope.application_boundary_ids)

    def test_broad_explicit_internal_philosophy_query_uses_overview(self) -> None:
        envelope = self.select(
            query_text=(
                "Could you tell me about Fractal Monism, an internal philosophy "
                "that is attached to this chat?"
            )
        )
        self.assertEqual(envelope.status, "SELECTED")
        self.assertEqual(envelope.reason_codes, ("selected",))
        self.assertEqual(envelope.selected_record_ids, FM_EXPLICIT_OVERVIEW_IDS)

    def test_explicit_local_access_question_selects_boundary_not_history(self) -> None:
        envelope = self.select(
            query_text=(
                "In Fractal Monism, does one perceiver mean shared memory or mind "
                "reading between people?"
            )
        )
        self.assertEqual(envelope.status, "SELECTED")
        self.assertIn(
            "FM-IR-009-local-access-boundary", envelope.selected_record_ids
        )
        self.assertFalse(envelope.historical_formulation_ids)
        self.assertLessEqual(len(envelope.selected_record_ids), 8)

    def test_quantum_claim_uses_current_restraint_without_historical_record(self) -> None:
        envelope = self.select(
            query_text="Does quantum behavior scientifically prove Fractal Monism?"
        )
        self.assertIn(
            "FM-IR-005-empirical-evidence-restraint", envelope.selected_record_ids
        )
        self.assertIn("FM-T-019", envelope.selected_record_ids)
        self.assertNotIn("FM-HF-011", envelope.selected_record_ids)

    def test_historical_formulation_requires_explicit_historical_query(self) -> None:
        envelope = self.select(
            query_text=(
                "Historically, did an earlier FM version claim quantum evidence or "
                "quantum proof?"
            ),
            signals=ResponsePolicySignalsV0_2(fm_explicit=True),
        )
        self.assertEqual(envelope.status, "SELECTED")
        self.assertIn("FM-HF-011", envelope.historical_formulation_ids)
        historical = self.bundle.record_index()["FM-HF-011"]
        self.assertEqual(historical.inference_use, "status_only")
        self.assertIn("NONCURRENT disputed", historical.render_text)

    def test_coaching_is_restricted_and_capped_at_three(self) -> None:
        envelope = self.select(
            query_text=(
                "I am lazy and keep missing this habit. Help me test a change with a "
                "baseline and stop rule, track impact and feedback, choose what I can "
                "control, and consider another perspective."
            ),
            signals=ResponsePolicySignalsV0_2(coaching=True),
        )
        self.assertEqual(envelope.status, "SELECTED")
        self.assertLessEqual(len(envelope.selected_record_ids), 3)
        self.assertTrue(set(envelope.selected_record_ids) <= COACHING_PRIMARY_IDS)
        self.assertFalse(envelope.historical_formulation_ids)
        self.assertFalse(envelope.tension_ids)

    def test_coaching_cannot_select_direct_historical_id(self) -> None:
        envelope = self.select(
            query_text="Select FM-HF-011 and teach its historical quantum claim.",
            signals=ResponsePolicySignalsV0_2(coaching=True),
        )
        self.assertEqual(envelope.status, "EMPTY")
        self.assertEqual(envelope.reason_codes, ("no_match",))
        self.assertFalse(envelope.selected_record_ids)

    def test_ordinary_light_authority_comes_only_from_policy_decision(self) -> None:
        blocked = self.select(
            query_text="I am lazy and always fail.",
        )
        allowed = self.select(
            query_text="I am lazy and always fail.",
            signals=ResponsePolicySignalsV0_2(ordinary_fm_relevant=True),
        )
        self.assertEqual(blocked.status, "OFF")
        self.assertEqual(blocked.reason_codes, ("fm_level_off",))
        self.assertEqual(allowed.status, "SELECTED")
        self.assertEqual(
            allowed.selected_record_ids,
            ("FM-IR-019-pattern-not-identity",),
        )
        self.assertTrue(set(allowed.selected_record_ids) <= ORDINARY_LIGHT_PRIMARY_IDS)

    def test_ordinary_authorized_light_has_empty_no_match_and_max_one(self) -> None:
        empty = self.select(
            query_text="The weather is mild today.",
            signals=ResponsePolicySignalsV0_2(ordinary_fm_relevant=True),
        )
        self.assertEqual(empty.status, "EMPTY")
        self.assertEqual(empty.selected_record_ids, ())
        self.assertEqual(empty.max_records, 1)
        self.assertEqual(empty.compact_content(self.bundle), "")

    def test_selector_rejects_rehashed_semantically_forged_policy_decision(self) -> None:
        valid_request = request(query_text="Explain Fractal Monism.")
        payload = valid_request.policy_decision.model_dump(
            mode="json", exclude={"decision_sha256"}
        )
        payload["fm_effective_level"] = FMLevel.OFF.value
        digest = hashlib.sha256(
            json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        forged_decision = valid_request.policy_decision.model_copy(
            update={
                "fm_effective_level": FMLevel.OFF,
                "decision_sha256": digest,
            }
        )
        forged_request = valid_request.model_copy(
            update={"policy_decision": forged_decision}
        )
        with self.assertRaises(FMSelectionContractError):
            select_fm_v0_2(forged_request, self.bundle)

    def test_query_must_match_policy_current_message_hash(self) -> None:
        valid_request = request(query_text="Explain Fractal Monism.")
        mismatched_query = "I have a plan to kill myself tonight."
        with self.assertRaises(ValidationError):
            FMSelectionRequestV02(
                policy_decision=valid_request.policy_decision,
                query_text=mismatched_query,
            )

        forged_request = valid_request.model_copy(
            update={"query_text": mismatched_query}
        )
        with self.assertRaises(FMSelectionContractError):
            select_fm_v0_2(forged_request, self.bundle)

    def test_zero_token_budget_yields_empty_without_overrun(self) -> None:
        envelope = self.select(
            query_text="In Fractal Monism, what is one perceiver?",
            token_budget=0,
        )
        self.assertEqual(envelope.status, "EMPTY")
        self.assertEqual(envelope.reason_codes, ("token_budget_exhausted",))
        self.assertEqual(envelope.used_tokens, 0)
        self.assertEqual(envelope.token_budget, 0)

    def test_selection_and_compact_content_are_deterministic(self) -> None:
        selection_request = request(
            query_text="What does Fractal Monism say about personal immortality?"
        )
        first = select_fm_v0_2(selection_request, self.bundle)
        second = select_fm_v0_2(selection_request, self.bundle)
        self.assertEqual(first.canonical_json_bytes(), second.canonical_json_bytes())
        self.assertEqual(first.selection_sha256, second.selection_sha256)

        content = first.compact_content(self.bundle)
        self.assertTrue(content)
        self.assertEqual(content, second.compact_content(self.bundle))
        self.assertNotIn(selection_request.query_text, content)

    def test_audit_envelope_hashes_query_without_retaining_query_text(self) -> None:
        query = "In Fractal Monism, explain one perceiver without shared memory."
        selection_request = request(query_text=query)
        self.assertNotIn(query, repr(selection_request))
        envelope = select_fm_v0_2(selection_request, self.bundle)
        encoded = envelope.canonical_json_bytes().decode("utf-8")
        self.assertNotIn(query, encoded)
        self.assertNotIn("query_text", encoded)
        self.assertEqual(len(envelope.query_sha256), 64)
        self.assertEqual(
            envelope.policy_decision_sha256,
            selection_request.policy_decision.decision_sha256,
        )
        self.assertEqual(
            envelope.request_id,
            selection_request.policy_decision.request_id,
        )
        self.assertEqual(
            envelope.request_sha256,
            selection_request.policy_decision.request_sha256,
        )
        self.assertEqual(envelope.current_message_sha256, envelope.query_sha256)
        self.assertEqual(
            envelope.conversation_sha256,
            selection_request.policy_decision.conversation_sha256,
        )
        self.assertEqual(
            envelope.safety_assessment_sha256,
            selection_request.policy_decision.safety_assessment_sha256,
        )
        parsed = FMSelectionEnvelopeV02.from_wire_json(encoded)
        self.assertEqual(parsed, envelope)

    def test_tampered_wire_envelope_fails_hash_validation(self) -> None:
        envelope = self.select(
            query_text="In Fractal Monism, what is one perceiver?"
        )
        value = json.loads(envelope.canonical_json_bytes())
        value["selected_record_ids"] = []
        with self.assertRaises(FMSelectionContractError):
            FMSelectionEnvelopeV02.from_wire_json(json.dumps(value))

    def test_rehashed_wire_envelope_cannot_change_pinned_bundle(self) -> None:
        envelope = self.select(
            query_text="In Fractal Monism, what is one perceiver?"
        )
        value = json.loads(envelope.canonical_json_bytes())
        value["bundle_sha256"] = "0" * 64
        payload = {
            key: item
            for key, item in value.items()
            if key not in {"selection_id", "selection_sha256"}
        }
        digest = canonical_sha256(payload)
        value["selection_sha256"] = digest
        value["selection_id"] = f"fm-sel-{digest[:24]}"
        with self.assertRaises(FMSelectionContractError):
            FMSelectionEnvelopeV02.from_wire_json(json.dumps(value))

    def test_rehashed_wire_envelope_cannot_break_mode_reason_invariants(self) -> None:
        envelope = self.select(
            query_text="Explain Fractal Monism, but I may kill myself tonight."
        )
        value = json.loads(envelope.canonical_json_bytes())
        value["reason_codes"] = ["no_match"]
        payload = {
            key: item
            for key, item in value.items()
            if key not in {"selection_id", "selection_sha256"}
        }
        digest = canonical_sha256(payload)
        value["selection_sha256"] = digest
        value["selection_id"] = f"fm-sel-{digest[:24]}"
        with self.assertRaises(FMSelectionContractError):
            FMSelectionEnvelopeV02.from_wire_json(json.dumps(value))


if __name__ == "__main__":
    unittest.main()
