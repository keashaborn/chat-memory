from __future__ import annotations

from dataclasses import replace
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import unittest
import uuid

from pydantic import BaseModel, ConfigDict, ValidationError

from rag_engine.memory_v1_evidence_context_v1 import (
    build_memory_evidence_context_envelope_v1,
)
from rag_engine.memory_v1_evidence_context_v2 import (
    build_memory_evidence_context_envelope_v2,
)
from rag_engine.memory_v1_openai_structured_transport_v1 import (
    _structured_schema_is_strict,
)
from rag_engine.memory_v1_personal_evidence_prefilter_v1 import (
    TRUSTED_SOURCE_ROLE,
    classify_personal_evidence_v1,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    ProviderEntityMention,
    ProviderObservation,
)


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "rag_engine"
    / "memory_v1_openai_v5_2_semantic_tasks_v1.py"
)
MODULE_NAME = "rag_engine.memory_v1_openai_v5_2_semantic_tasks_v1"
SPEC = importlib.util.spec_from_file_location(MODULE_NAME, MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("semantic-task stage module could not be loaded")
semantic = importlib.util.module_from_spec(SPEC)
sys.modules[MODULE_NAME] = semantic
SPEC.loader.exec_module(semantic)


OWNER_A = "00000000-0000-4000-8000-000000000001"
OWNER_B = "00000000-0000-4000-8000-000000000002"
PROFILE_A = "a" * 64
PROFILE_B = "b" * 64
SHA_C = "c" * 64


class OversizedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    first: str
    second: str


class FakeExpectedBindings:
    def validate_exact(self, **_: object) -> None:
        return None


def gate(source: str):
    return classify_personal_evidence_v1(
        source,
        source_role=TRUSTED_SOURCE_ROLE,
    )


def source_span(source: str, quote: str) -> dict[str, object]:
    start = source.index(quote)
    return {
        "start": start,
        "end": start + len(quote),
        "quote": quote,
    }


def self_mention(source: str) -> dict[str, object]:
    return {
        "entity_ref": "e01",
        "entity_type": "self",
        "mention_kind": "self_reference",
        "name_text": None,
        "relationship_role": None,
        "source_spans": [source_span(source, "I")],
        "extraction_confidence": 1.0,
        "reason_codes": ["explicit_self_reference"],
    }


def entity_mention(source: str) -> ProviderEntityMention:
    return ProviderEntityMention.model_validate(
        {
            "entity_ref": "e02",
            "entity_type": "organization",
            "mention_kind": "named",
            "name_text": "Acme",
            "relationship_role": "employer",
            "source_spans": [source_span(source, "Acme")],
            "extraction_confidence": 1.0,
            "reason_codes": ["explicit_named_entity"],
        },
        strict=True,
    )


def observation(
    source: str,
    *,
    sensitivity: str = "low",
) -> ProviderObservation:
    return ProviderObservation.model_validate(
        {
            "observation_ref": "o01",
            "subject_entity_ref": "e01",
            "predicate": "employment.worked_for",
            "object": {"kind": "entity", "entity_ref": "e02"},
            "polarity": "affirmed",
            "modality": "asserted",
            "projection_class": "direct_claim",
            "surface_policy": "direct_or_relevant",
            "temporal": {
                "semantic": "none",
                "shape": "none",
                "basis": "none",
                "source_form": "none",
                "certainty": "unknown",
                "precision": "unknown",
                "instant": None,
                "calendar_range": None,
                "instant_range": None,
                "relative_offset": None,
                "recurrence": None,
                "anchored_to_source_time": False,
                "reason_codes": [],
            },
            "sensitivity": sensitivity,
            "extraction_confidence": 0.95,
            "source_spans": [source_span(source, "I work at Acme.")],
            "reason_codes": ["explicit_source_support"],
        },
        strict=True,
    )


def extraction_packet(
    source: str,
    *,
    sensitivity: str = "low",
) -> dict[str, object]:
    return {
        "entity_mentions": [
            self_mention(source),
            entity_mention(source).model_dump(mode="json"),
        ],
        "observations": [
            observation(source, sensitivity=sensitivity).model_dump(mode="json")
        ],
        "comparison_hints": [],
        "deferrals": [],
        "packet_findings": [],
    }


def evidence_context(
    owner: str,
    target: str,
    *,
    before: tuple[str, ...],
    after: tuple[str, ...] = (),
):
    segments = [*before, target, *after]
    full_source = " ".join(segments)
    source_id = str(uuid.UUID("10000000-0000-4000-8000-000000000001"))
    thread_id = str(uuid.UUID("10000000-0000-4000-8000-000000000002"))
    request_id = str(uuid.UUID("10000000-0000-4000-8000-000000000003"))
    evidence_ids = [
        str(uuid.UUID(int=0x20000000000040008000000000000001 + index))
        for index in range(len(segments))
    ]
    rows = []
    cursor = 0
    for index, (evidence_id, content) in enumerate(
        zip(evidence_ids, segments, strict=True)
    ):
        start = full_source.index(content, cursor)
        end = start + len(content)
        cursor = end
        rows.append(
            {
                "evidence_id": evidence_id,
                "owner_user_id": owner,
                "source_system": "public.chat_log",
                "content": content,
                "content_sha256": hashlib.sha256(
                    content.encode("utf-8")
                ).hexdigest(),
                "metadata": {
                    "source_id": source_id,
                    "thread_id": thread_id,
                    "request_id": request_id,
                    "source_content_sha256": hashlib.sha256(
                        full_source.encode("utf-8")
                    ).hexdigest(),
                    "source_char_start": start,
                    "source_char_end": end,
                    "primary_lane": "explicit_assertion",
                    "epistemic_role": "asserted",
                    "span_origin": f"fixture_{index}",
                },
            }
        )
    target_index = len(before)
    return build_memory_evidence_context_envelope_v1(
        expected_owner_user_id=owner,
        target_evidence_id=evidence_ids[target_index],
        expected_target_content_sha256=hashlib.sha256(
            target.encode("utf-8")
        ).hexdigest(),
        source_row={
            "id": source_id,
            "owner_user_id": owner,
            "thread_id": thread_id,
            "request_id": request_id,
            "text": full_source,
            "created_at": "2026-08-05T12:00:00Z",
        },
        evidence_rows=rows,
    )


def compile_extraction(
    source: str,
    *,
    owner: str = OWNER_A,
    context=None,
):
    context_sha = None if context is None else context.envelope_sha256
    expected = semantic.build_expected_bindings_v1(
        task="memory_extraction",
        owner_user_id=owner,
        source_text=source,
        context_binding_sha256=context_sha,
        profile_sha256=PROFILE_A,
    )
    return semantic.compile_extraction_payload_v1(
        source_text=source,
        owner_user_id=owner,
        gate_result=gate(source),
        profile_sha256=PROFILE_A,
        expected_bindings=expected,
        evidence_context=context,
    )


def canonical_extraction_result(
    compiled,
    packet: dict[str, object] | None = None,
):
    return semantic.parse_extraction_result_v1(
        compiled,
        packet if packet is not None else extraction_packet(compiled.source_text),
    )


class SemanticTasksV1Test(unittest.TestCase):
    def test_all_output_schemas_pass_current_responses_strict_gate(self) -> None:
        for model in (
            semantic.OpenAIExtractionResultV1,
            semantic.OpenAIEntityValidationResultV1,
            semantic.OpenAIEntailmentResultV1,
        ):
            with self.subTest(model=model.__name__):
                schema = model.model_json_schema()
                self.assertTrue(_structured_schema_is_strict(schema))
                self.assertNotIn('"default"', semantic.canonical_json(schema))

    def test_extraction_schema_reserves_trusted_source_time_for_server(self) -> None:
        schema = semantic.OpenAIExtractionResultV1.model_json_schema()
        observation = schema["$defs"]["OpenAIProviderObservationV1"]
        temporal_ref = observation["properties"]["temporal"]["$ref"]
        temporal_name = temporal_ref.rsplit("/", 1)[-1]
        anchored = schema["$defs"][temporal_name]["properties"][
            "anchored_to_source_time"
        ]
        self.assertEqual(anchored["const"], False)

        source = "I work at Acme."
        packet = extraction_packet(source)
        packet["observations"][0]["temporal"][
            "anchored_to_source_time"
        ] = True
        with self.assertRaises(ValidationError):
            semantic.OpenAIExtractionResultV1.model_validate(
                packet,
                strict=True,
            )

    def test_output_and_canonical_result_envelopes_are_immutable(self) -> None:
        entity = semantic.parse_entity_validation_result_v1(
            {"decision": "supported", "confidence": "high"}
        )
        with self.assertRaises(ValidationError):
            entity.decision = "ambiguous"

        source = "I work at Acme."
        compiled = compile_extraction(source)
        original = extraction_packet(source)
        result = canonical_extraction_result(compiled, original)
        original["entity_mentions"][1]["name_text"] = "Changed"
        local_copy = json.loads(result.canonical_result_json)
        local_copy["entity_mentions"].append({"smuggled": True})
        self.assertNotIn("Changed", result.canonical_result_json)
        self.assertNotIn("smuggled", result.canonical_result_json)
        self.assertFalse(hasattr(result, "entity_mentions"))
        with self.assertRaises(ValidationError):
            result.canonical_result_json = "{}"

    def test_strict_json_and_closed_nested_schema_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            semantic.SemanticTaskContractError,
            "duplicate JSON key",
        ):
            semantic.parse_entity_validation_result_v1(
                '{"decision":"supported","decision":"ambiguous",'
                '"confidence":"high"}'
            )
        with self.assertRaisesRegex(
            semantic.SemanticTaskContractError,
            "non-finite JSON number",
        ):
            semantic.strict_json_loads('{"score":NaN}')
        with self.assertRaisesRegex(
            semantic.SemanticTaskContractError,
            "closed schema",
        ):
            semantic.parse_entailment_result_v1(
                {
                    "decision": "entailed",
                    "confidence": "high",
                    "explanation": "not allowed",
                }
            )
        source = "I work at Acme."
        nested = extraction_packet(source)
        nested["entity_mentions"][1]["source_spans"][0]["unexpected"] = True
        with self.assertRaisesRegex(
            semantic.SemanticTaskContractError,
            "closed schema",
        ):
            semantic.parse_extraction_result_v1(
                compile_extraction(source),
                nested,
            )

    def test_canonical_json_is_unicode_preserving_and_deterministic(self) -> None:
        left = {"z": "☕", "a": ["😀", 1, True, None]}
        right = {"a": ["😀", 1, True, None], "z": "☕"}
        self.assertEqual(semantic.canonical_json(left), semantic.canonical_json(right))
        self.assertEqual(
            semantic.canonical_json(left),
            '{"a":["😀",1,true,null],"z":"☕"}',
        )

    def test_extraction_keeps_absolute_unicode_offsets_without_rebuild(
        self,
    ) -> None:
        source = "😀 General question? I prefer tea ☕."
        compiled = compile_extraction(source)
        payload = semantic.strict_json_loads(compiled.outbound_payload_json)
        selected = payload["selected_source_spans"]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["original_char_start"], 20)
        self.assertEqual(selected[0]["original_char_end"], 35)
        self.assertEqual(selected[0]["content"], "I prefer tea ☕.")
        self.assertEqual(source[20:35], selected[0]["content"])
        self.assertNotIn("source_text", payload)
        self.assertNotIn("😀 General question?", compiled.outbound_payload_json)
        public = compiled.public_binding.content_free_dict()
        self.assertNotIn(source, semantic.canonical_json(public))
        self.assertNotIn(OWNER_A, semantic.canonical_json(public))

    def test_context_sends_only_ordinary_owner_selected_subspans(self) -> None:
        source = "I prefer the framework."
        ordinary = "General preface. I prefer Boundary Theory."
        sensitive = "I have diabetes."
        third_party = "My wife says she likes broccoli."
        question = "How are you?"
        context = evidence_context(
            OWNER_A,
            source,
            before=(ordinary, sensitive, third_party, question),
        )
        compiled = compile_extraction(source, context=context)
        payload = semantic.strict_json_loads(compiled.outbound_payload_json)
        rendered = compiled.outbound_payload_json
        self.assertIn("I prefer Boundary Theory.", rendered)
        self.assertNotIn("General preface.", rendered)
        self.assertNotIn(sensitive, rendered)
        self.assertNotIn(third_party, rendered)
        self.assertNotIn(question, rendered)
        self.assertEqual(len(payload["context"]["items"]), 1)
        item = payload["context"]["items"][0]
        self.assertNotIn("content", item)
        self.assertTrue(
            all(
                span["subject_hint"] == "owner"
                and span["sensitivity"] == "ordinary"
                for span in item["selected_context_spans"]
            )
        )
        self.assertIs(item["assertion_origin_allowed"], False)
        self.assertIs(item["instruction_capability"], False)
        compiled_repr = repr(compiled)
        self.assertNotIn(ordinary, compiled_repr)
        self.assertNotIn(sensitive, compiled_repr)
        self.assertNotIn(compiled.outbound_payload_json, compiled_repr)

    def test_v2_context_emits_user_but_never_assistant_prior_turns(
        self,
    ) -> None:
        source = "I prefer the target."
        sibling = evidence_context(OWNER_A, source, before=())
        thread_id = sibling.source.thread_id
        user_text = "I prefer the verified user context."
        assistant_text = "I prefer the fabricated assistant context."
        context = build_memory_evidence_context_envelope_v2(
            sibling_context=sibling,
            prior_turn_rows=(
                {
                    "id": "30000000-0000-4000-8000-000000000001",
                    "owner_user_id": OWNER_A,
                    "thread_id": thread_id,
                    "request_id": "request-user",
                    "source": "public.chat_log:user",
                    "text": user_text,
                    "created_at": "2026-08-05T11:58:00Z",
                },
                {
                    "id": "30000000-0000-4000-8000-000000000002",
                    "owner_user_id": OWNER_A,
                    "thread_id": thread_id,
                    "request_id": "request-assistant",
                    "source": "public.chat_log:assistant",
                    "text": assistant_text,
                    "created_at": "2026-08-05T11:59:00Z",
                },
            ),
        )
        self.assertEqual(
            [turn.speaker_role for turn in context.prior_turns],
            ["user", "assistant"],
        )
        compiled = compile_extraction(source, context=context)
        payload = semantic.strict_json_loads(compiled.outbound_payload_json)
        rendered = compiled.outbound_payload_json
        self.assertIn(user_text, rendered)
        self.assertNotIn(assistant_text, rendered)
        self.assertEqual(len(payload["context"]["items"]), 1)
        self.assertEqual(
            payload["context"]["items"][0]["speaker_role"],
            "user",
        )

    def test_context_owner_and_source_binding_failures_are_rejected(self) -> None:
        source = "I prefer tea."
        context = evidence_context(
            OWNER_A,
            source,
            before=("I prefer coffee.",),
        )
        expected = semantic.build_expected_bindings_v1(
            task="memory_extraction",
            owner_user_id=OWNER_B,
            source_text=source,
            context_binding_sha256=context.envelope_sha256,
            profile_sha256=PROFILE_A,
        )
        with self.assertRaisesRegex(
            semantic.SemanticTaskContractError,
            "owner mismatch",
        ):
            semantic.compile_extraction_payload_v1(
                source_text=source,
                owner_user_id=OWNER_B,
                gate_result=gate(source),
                profile_sha256=PROFILE_A,
                expected_bindings=expected,
                evidence_context=context,
            )
        other = "I prefer juice."
        with self.assertRaisesRegex(
            semantic.SemanticTaskContractError,
            "source hash mismatch",
        ):
            semantic.compile_extraction_payload_v1(
                source_text=other,
                owner_user_id=OWNER_A,
                gate_result=gate(other),
                profile_sha256=PROFILE_A,
                expected_bindings=semantic.build_expected_bindings_v1(
                    task="memory_extraction",
                    owner_user_id=OWNER_A,
                    source_text=other,
                    context_binding_sha256=context.envelope_sha256,
                    profile_sha256=PROFILE_A,
                ),
                evidence_context=context,
            )

    def test_exact_binding_rejects_all_five_cross_binding_mixups(self) -> None:
        source = "I prefer tea."
        correct = semantic.build_expected_bindings_v1(
            task="memory_extraction",
            owner_user_id=OWNER_A,
            source_text=source,
            context_binding_sha256=None,
            profile_sha256=PROFILE_A,
        )
        mismatches = {
            "task": correct.model_copy(update={"task": "entity_validation"}),
            "owner": semantic.build_expected_bindings_v1(
                task="memory_extraction",
                owner_user_id=OWNER_B,
                source_text=source,
                context_binding_sha256=None,
                profile_sha256=PROFILE_A,
            ),
            "source": correct.model_copy(update={"source_sha256": SHA_C}),
            "context": correct.model_copy(
                update={"context_binding_sha256": SHA_C}
            ),
            "profile": correct.model_copy(update={"profile_sha256": PROFILE_B}),
        }
        for label, expected in mismatches.items():
            with self.subTest(label=label), self.assertRaisesRegex(
                semantic.SemanticTaskContractError,
                label,
            ):
                semantic.compile_extraction_payload_v1(
                    source_text=source,
                    owner_user_id=OWNER_A,
                    gate_result=gate(source),
                    profile_sha256=PROFILE_A,
                    expected_bindings=expected,
                )

    def test_fake_expected_binding_object_is_rejected_for_all_tasks(self) -> None:
        source = "I work at Acme."
        compiled = compile_extraction(source)
        result = canonical_extraction_result(compiled)
        cases = (
            (
                semantic.compile_extraction_payload_v1,
                {
                    "source_text": source,
                    "owner_user_id": OWNER_A,
                    "gate_result": gate(source),
                    "profile_sha256": PROFILE_A,
                },
            ),
            (
                semantic.compile_entity_validation_payload_v1,
                {
                    "source_text": source,
                    "owner_user_id": OWNER_A,
                    "gate_result": gate(source),
                    "extraction_result": result,
                    "entity_ref": "e02",
                    "profile_sha256": PROFILE_A,
                },
            ),
            (
                semantic.compile_entailment_payload_v1,
                {
                    "source_text": source,
                    "owner_user_id": OWNER_A,
                    "gate_result": gate(source),
                    "extraction_result": result,
                    "observation_ref": "o01",
                    "profile_sha256": PROFILE_A,
                },
            ),
        )
        for compiler, kwargs in cases:
            with self.subTest(compiler=compiler.__name__), self.assertRaisesRegex(
                semantic.SemanticTaskContractError,
                "exact contract type",
            ):
                compiler(
                    **kwargs,
                    expected_bindings=FakeExpectedBindings(),
                )

    def test_gate_tampering_and_sensitive_target_are_rejected(self) -> None:
        source = "I prefer tea."
        authoritative = gate(source)
        tampered = replace(
            authoritative,
            selected_spans=(
                replace(
                    authoritative.selected_spans[0],
                    content_sha256="0" * 64,
                ),
            ),
        )
        expected = semantic.build_expected_bindings_v1(
            task="memory_extraction",
            owner_user_id=OWNER_A,
            source_text=source,
            context_binding_sha256=None,
            profile_sha256=PROFILE_A,
        )
        with self.assertRaisesRegex(
            semantic.SemanticTaskContractError,
            "not authoritative",
        ):
            semantic.compile_extraction_payload_v1(
                source_text=source,
                owner_user_id=OWNER_A,
                gate_result=tampered,
                profile_sha256=PROFILE_A,
                expected_bindings=expected,
            )
        sensitive = "I am pregnant."
        with self.assertRaisesRegex(
            semantic.SemanticTaskContractError,
            "not externally eligible",
        ):
            compile_extraction(sensitive)

    def test_extraction_result_rejects_wrong_outside_and_context_spans(self) -> None:
        source = "General preface. I work at Acme."
        compiled = compile_extraction(source)
        wrong_quote = extraction_packet(source)
        wrong_quote["entity_mentions"][1]["source_spans"][0]["quote"] = "Other"
        with self.assertRaisesRegex(
            semantic.SemanticTaskContractError,
            "source quote mismatch",
        ):
            semantic.parse_extraction_result_v1(compiled, wrong_quote)

        wrong_observation_quote = extraction_packet(source)
        wrong_observation_quote["observations"][0]["source_spans"][0][
            "quote"
        ] = "Other"
        with self.assertRaisesRegex(
            semantic.SemanticTaskContractError,
            "source quote mismatch",
        ):
            semantic.parse_extraction_result_v1(
                compiled,
                wrong_observation_quote,
            )

        outside = extraction_packet(source)
        outside["entity_mentions"][1]["source_spans"] = [
            source_span(source, "General")
        ]
        with self.assertRaisesRegex(
            semantic.SemanticTaskContractError,
            "outside externally eligible evidence",
        ):
            semantic.parse_extraction_result_v1(compiled, outside)

        deferred_outside = extraction_packet(source)
        deferred_outside["deferrals"] = [
            {
                "reason_code": "insufficient_evidence",
                "memory_shape": "none",
                "source_spans": [source_span(source, "General")],
                "sensitivity": "low",
            }
        ]
        with self.assertRaisesRegex(
            semantic.SemanticTaskContractError,
            "outside externally eligible evidence",
        ):
            semantic.parse_extraction_result_v1(
                compiled,
                deferred_outside,
            )

        deferred_without_source = extraction_packet(source)
        deferred_without_source["deferrals"] = [
            {
                "reason_code": "insufficient_evidence",
                "memory_shape": "none",
                "source_spans": [],
                "sensitivity": "low",
            }
        ]
        with self.assertRaisesRegex(
            semantic.SemanticTaskContractError,
            "source spans are absent",
        ):
            semantic.parse_extraction_result_v1(
                compiled,
                deferred_without_source,
            )

        target = "I work at Acme."
        context_text = "I prefer X."
        context = evidence_context(
            OWNER_A,
            target,
            before=(context_text,),
        )
        context_compiled = compile_extraction(target, context=context)
        context_only = extraction_packet(target)
        context_only["entity_mentions"][1]["source_spans"] = [
            {
                "start": 0,
                "end": len(context_text),
                "quote": context_text,
            }
        ]
        with self.assertRaisesRegex(
            semantic.SemanticTaskContractError,
            "source (quote|span bounds)",
        ):
            semantic.parse_extraction_result_v1(
                context_compiled,
                context_only,
            )

        valid_result = canonical_extraction_result(compiled)
        forged_result_json = valid_result.canonical_result_json.replace(
            '"Acme"',
            '"Fake"',
            1,
        )
        forged_result = valid_result.model_copy(
            update={"canonical_result_json": forged_result_json}
        )
        with self.assertRaisesRegex(
            semantic.SemanticTaskContractError,
            "envelope hash mismatch",
        ):
            semantic.entity_mention_binding_sha256_v1(
                forged_result,
                "e02",
            )

    def test_entity_and_entailment_select_only_canonical_result_refs(self) -> None:
        source = "I work at Acme."
        extraction = compile_extraction(source)
        result = canonical_extraction_result(extraction)
        entity_sha = semantic.entity_mention_binding_sha256_v1(result, "e02")
        entity_expected = semantic.build_expected_bindings_v1(
            task="entity_validation",
            owner_user_id=OWNER_A,
            source_text=source,
            context_binding_sha256=entity_sha,
            profile_sha256=PROFILE_A,
        )
        entity_task = semantic.compile_entity_validation_payload_v1(
            source_text=source,
            owner_user_id=OWNER_A,
            gate_result=gate(source),
            extraction_result=result,
            entity_ref="e02",
            profile_sha256=PROFILE_A,
            expected_bindings=entity_expected,
        )
        entity_payload = semantic.strict_json_loads(
            entity_task.outbound_payload_json
        )
        self.assertEqual(
            entity_payload["structured_entity_mention"]["name_text"],
            "Acme",
        )

        observation_sha = semantic.observation_binding_sha256_v1(result, "o01")
        entailment_expected = semantic.build_expected_bindings_v1(
            task="observation_entailment",
            owner_user_id=OWNER_A,
            source_text=source,
            context_binding_sha256=observation_sha,
            profile_sha256=PROFILE_A,
        )
        entailment_task = semantic.compile_entailment_payload_v1(
            source_text=source,
            owner_user_id=OWNER_A,
            gate_result=gate(source),
            extraction_result=result,
            observation_ref="o01",
            profile_sha256=PROFILE_A,
            expected_bindings=entailment_expected,
        )
        entailment_payload = semantic.strict_json_loads(
            entailment_task.outbound_payload_json
        )
        self.assertEqual(
            entailment_payload["structured_observation"]["predicate"],
            "employment.worked_for",
        )

        alternate_packet = extraction_packet(source)
        alternate_packet["entity_mentions"][1]["name_text"] = "Other"
        alternate_result = canonical_extraction_result(
            extraction,
            alternate_packet,
        )
        with self.assertRaisesRegex(
            semantic.SemanticTaskContractError,
            "extraction authority|context binding",
        ):
            replace(
                entity_task,
                extraction_result_authority=alternate_result,
            )

    def test_arbitrary_mapping_and_cross_source_result_smuggling_fail(self) -> None:
        source = "I work at Acme."
        extraction = compile_extraction(source)
        result = canonical_extraction_result(extraction)
        entity_sha = semantic.entity_mention_binding_sha256_v1(result, "e02")
        expected = semantic.build_expected_bindings_v1(
            task="entity_validation",
            owner_user_id=OWNER_A,
            source_text=source,
            context_binding_sha256=entity_sha,
            profile_sha256=PROFILE_A,
        )
        with self.assertRaisesRegex(
            semantic.SemanticTaskContractError,
            "envelope type",
        ):
            semantic.compile_entity_validation_payload_v1(
                source_text=source,
                owner_user_id=OWNER_A,
                gate_result=gate(source),
                extraction_result={
                    "name_text": "unrelated private bytes",
                    "source_spans": [source_span(source, "Acme")],
                },
                entity_ref="e02",
                profile_sha256=PROFILE_A,
                expected_bindings=expected,
            )
        other_source = "I work at Other."
        with self.assertRaisesRegex(
            semantic.SemanticTaskContractError,
            "follow-up source binding",
        ):
            semantic.compile_entity_validation_payload_v1(
                source_text=other_source,
                owner_user_id=OWNER_A,
                gate_result=gate(other_source),
                extraction_result=result,
                entity_ref="e02",
                profile_sha256=PROFILE_A,
                expected_bindings=semantic.build_expected_bindings_v1(
                    task="entity_validation",
                    owner_user_id=OWNER_A,
                    source_text=other_source,
                    context_binding_sha256=entity_sha,
                    profile_sha256=PROFILE_A,
                ),
            )

    def test_entailment_external_followup_is_low_sensitivity_only(self) -> None:
        source = "I work at Acme."
        extraction = compile_extraction(source)
        medium_result = canonical_extraction_result(
            extraction,
            extraction_packet(source, sensitivity="medium"),
        )
        item_sha = semantic.observation_binding_sha256_v1(
            medium_result,
            "o01",
        )
        expected = semantic.build_expected_bindings_v1(
            task="observation_entailment",
            owner_user_id=OWNER_A,
            source_text=source,
            context_binding_sha256=item_sha,
            profile_sha256=PROFILE_A,
        )
        with self.assertRaisesRegex(
            semantic.SemanticTaskContractError,
            "low-sensitivity",
        ):
            semantic.compile_entailment_payload_v1(
                source_text=source,
                owner_user_id=OWNER_A,
                gate_result=gate(source),
                extraction_result=medium_result,
                observation_ref="o01",
                profile_sha256=PROFILE_A,
                expected_bindings=expected,
            )

    def test_compiled_task_rejects_noncanonical_and_forged_state(self) -> None:
        source = "I prefer tea."
        compiled = compile_extraction(source)
        with self.assertRaisesRegex(
            semantic.SemanticTaskContractError,
            "not canonical",
        ):
            replace(
                compiled,
                outbound_payload_json=compiled.outbound_payload_json + " ",
            )
        payload = semantic.strict_json_loads(compiled.outbound_payload_json)
        payload["contract_version"] = "forged_contract"
        with self.assertRaisesRegex(
            semantic.SemanticTaskContractError,
            "payload contract",
        ):
            replace(
                compiled,
                outbound_payload_json=semantic.canonical_json(payload),
            )
        payload = semantic.strict_json_loads(compiled.outbound_payload_json)
        payload["canonical_json_contract_version"] = "forged"
        with self.assertRaisesRegex(
            semantic.SemanticTaskContractError,
            "canonical JSON contract",
        ):
            replace(
                compiled,
                outbound_payload_json=semantic.canonical_json(payload),
            )
        forged_public = compiled.public_binding.model_copy(
            update={"source_char_count": compiled.public_binding.source_char_count + 1}
        )
        with self.assertRaisesRegex(
            semantic.SemanticTaskContractError,
            "public binding fields",
        ):
            replace(compiled, public_binding=forged_public)
        with self.assertRaisesRegex(
            semantic.SemanticTaskContractError,
            "output model mismatch",
        ):
            replace(
                compiled,
                output_model=semantic.OpenAIEntityValidationResultV1,
            )
        rendered = repr(compiled)
        self.assertNotIn(source, rendered)
        self.assertNotIn(compiled.outbound_payload_json, rendered)

    def test_all_parse_forms_enforce_total_canonical_byte_bound(self) -> None:
        oversized = "x" * 140_000
        mapping = {
            "decision": "supported",
            "confidence": "high",
            "padding_a": oversized,
            "padding_b": oversized,
        }
        with self.assertRaisesRegex(
            semantic.SemanticTaskContractError,
            "oversized",
        ):
            semantic.parse_entity_validation_result_v1(mapping)
        base_model = OversizedModel(first=oversized, second=oversized)
        with self.assertRaisesRegex(
            semantic.SemanticTaskContractError,
            "oversized",
        ):
            semantic.parse_entity_validation_result_v1(base_model)
        with self.assertRaisesRegex(
            semantic.SemanticTaskContractError,
            "oversized",
        ):
            semantic.strict_json_loads(
                json.dumps({"first": oversized, "second": oversized})
            )

    def test_source_and_payload_bounds_are_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            semantic.SemanticTaskContractError,
            "oversized",
        ):
            semantic.build_expected_bindings_v1(
                task="memory_extraction",
                owner_user_id=OWNER_A,
                source_text="x" * (semantic.MAX_SOURCE_CHARS + 1),
                context_binding_sha256=None,
                profile_sha256=PROFILE_A,
            )
        source = "I prefer tea."
        compiled = compile_extraction(source)
        tampered = compiled.outbound_payload_json.replace(
            PROFILE_A,
            PROFILE_B,
            1,
        )
        with self.assertRaisesRegex(
            semantic.SemanticTaskContractError,
            "profile|payload|public",
        ):
            replace(compiled, outbound_payload_json=tampered)


if __name__ == "__main__":
    unittest.main()
