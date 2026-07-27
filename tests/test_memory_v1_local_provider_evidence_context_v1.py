from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from rag_engine.memory_v1_evidence_context_v1 import (
    build_memory_evidence_context_envelope_v1,
)
from scripts.memory_v1_predicate_runtime_profile_v2 import (
    load_runtime_profile_v2,
)
from scripts.memory_v1_relational_extraction_v5_local_provider import (
    EVIDENCE_CONTEXT_COREFERENCE_VERSION,
    LocalLlamaCppProvider,
    _apply_context_coreference_bindings,
    _context_coreference_bindings,
    _explicit_concept_candidates,
    _example_entity,
    _example_observation,
    _literal,
    _packet,
    _target_definite_descriptions,
    _unresolved_context_coreferences,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
    ProviderPacket,
    TrustedExtractionSource,
)


ROOT = Path(__file__).resolve().parents[1]
OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
SOURCE_ID = "ed91f3b9-a4b8-4e63-aac7-53e370412483"
THREAD_ID = "2f6e6a6c-f43e-45e2-8137-8b1b01e67976"
REQUEST_ID = "bfca3e63-e670-4601-a06d-6345c18554f4"
TARGET_ID = "049205b4-9a6c-5e1a-bb8f-2ab9f05f8964"
SOURCE_TEXT = (
    "Much of the app is turning into life switch, so I’m thinking about "
    "making the verbal sage fractal monistic data that just be the kind of "
    "the engine that runs it through this platform. My idea is, I want to "
    "philosophy to be conveyed subtly cause I think the philosophy will "
    "help people in life. But no one‘s ever gonna ask about fractal monism "
    "so I need to figure out a way that the concepts and philosophy can be "
    "slipped into diet, exercise training, and just general questions of "
    "someone asks."
)


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def source_and_context():
    source_row = {
        "id": SOURCE_ID,
        "owner_user_id": OWNER,
        "thread_id": THREAD_ID,
        "request_id": REQUEST_ID,
        "created_at": "2026-07-02T02:05:41.345358+00:00",
        "text": SOURCE_TEXT,
    }
    definitions = (
        (
            "3bd07b8c-c047-531f-a3a7-05c8d901d5b6",
            0,
            183,
            "technical_project",
            "atomic",
        ),
        (
            "0c5c5763-ee69-532a-a0a6-e9be1e3433c1",
            184,
            238,
            "contextual_project",
            "compound_child",
        ),
        (
            TARGET_ID,
            239,
            293,
            "user_viewpoint",
            "compound_child",
        ),
    )
    rows = []
    for evidence_id, start, end, lane, origin in definitions:
        content = SOURCE_TEXT[start:end]
        rows.append(
            {
                "evidence_id": evidence_id,
                "owner_user_id": OWNER,
                "source_system": "public.chat_log",
                "content": content,
                "content_sha256": sha(content),
                "metadata": {
                    "source_id": SOURCE_ID,
                    "thread_id": THREAD_ID,
                    "request_id": REQUEST_ID,
                    "source_content_sha256": sha(SOURCE_TEXT),
                    "source_char_start": start,
                    "source_char_end": end,
                    "primary_lane": lane,
                    "epistemic_role": "user_belief_or_opinion",
                    "span_origin": origin,
                },
            }
        )
    target = next(row for row in rows if row["evidence_id"] == TARGET_ID)
    context = build_memory_evidence_context_envelope_v1(
        expected_owner_user_id=OWNER,
        target_evidence_id=TARGET_ID,
        expected_target_content_sha256=target["content_sha256"],
        source_row=source_row,
        evidence_rows=rows,
    )
    source = TrustedExtractionSource.create(
        job_id="00000000-0000-4000-8000-000000000001",
        source_system="public.chat_log",
        source_external_id=TARGET_ID,
        source_sha256=target["content_sha256"],
        source_recorded_at="2026-07-02T02:05:41.345358+00:00",
        content=target["content"],
    )
    return source, context


class LocalProviderEvidenceContextV1Test(unittest.TestCase):
    @staticmethod
    def provider() -> LocalLlamaCppProvider:
        profile = load_runtime_profile_v2(ROOT, "v5_2")
        registry = json.loads(
            profile.registry_path.read_text(encoding="utf-8")
        )
        return LocalLlamaCppProvider(
            model="qwen3-14b-local-extractor",
            model_file_sha256="5" * 64,
            runtime_revision="llama.cpp-b10066-86a9c79f8",
            registry=registry,
            transport=object(),
        )

    def test_request_keeps_target_offsets_and_marks_siblings_context_only(
        self,
    ) -> None:
        source, context = source_and_context()
        request = self.provider().request(
            source,
            evidence_context=context,
        )
        self.assertEqual(
            request.prompt_profile,
            "semantic_stance_compact_v1_sibling_context_v1",
        )
        self.assertIn("EVIDENCE_CONTEXT_RULES_V1", request.instructions)
        self.assertIn(
            "may not originate an entity, observation, comparison, or source",
            request.instructions,
        )
        self.assertIn(
            "DEFINITE_DESCRIPTION_COREFERENCE_V1",
            request.instructions,
        )
        self.assertIn(
            "COREFERENCE_RESOLUTION_PROCEDURE_V1",
            request.instructions,
        )
        self.assertIn(
            "Synthetic rule example",
            request.instructions,
        )
        self.assertIn(
            "TARGET_DEFINITE_DESCRIPTIONS="
            '[{"determiner":"the","end":28,"head":"philosophy",'
            '"phrase":"the philosophy","start":14}]',
            request.input_text,
        )
        self.assertIn(
            "TARGET_DEFINITE_DESCRIPTION_BINDINGS=",
            request.input_text,
        )
        self.assertIn(
            '"referent":"Fractal Monism"',
            request.input_text,
        )
        self.assertIn(
            "EVIDENCE_CONTEXT_CONTRACT="
            "memory_evidence_context_envelope_v1",
            request.input_text,
        )
        self.assertIn("CONTEXT_ONLY_START", request.input_text)
        self.assertIn(
            context.spans[0].content,
            request.input_text,
        )
        self.assertIn(
            context.spans[1].content,
            request.input_text,
        )
        target_block = (
            "SOURCE_CONTENT_START\n"
            f"{source.content}\n"
            "SOURCE_CONTENT_END"
        )
        self.assertIn(target_block, request.input_text)
        context_block = request.input_text.split(
            "CONTEXT_ONLY_START\n",
            1,
        )[1].split("\nCONTEXT_ONLY_END", 1)[0]
        self.assertNotIn(source.content, context_block)
        context_headers = [
            line
            for line in context_block.splitlines()
            if line.startswith("CONTEXT_ONLY_SPAN ")
        ]
        self.assertEqual(len(context_headers), 2)
        self.assertIn("coreference_distance=1", context_headers[0])
        self.assertIn("source_offsets=184:238", context_headers[0])
        self.assertIn("coreference_distance=2", context_headers[1])
        self.assertIn("source_offsets=0:183", context_headers[1])

    def test_detector_preserves_exact_target_offsets(self) -> None:
        source, _ = source_and_context()
        self.assertEqual(
            _target_definite_descriptions(source.content),
            [
                {
                    "determiner": "the",
                    "phrase": "the philosophy",
                    "head": "philosophy",
                    "start": 14,
                    "end": 28,
                }
            ],
        )
        self.assertEqual(
            _target_definite_descriptions(
                "I think this way will work in the future."
            ),
            [],
        )

    def test_concept_candidates_preserve_ambiguity(self) -> None:
        self.assertEqual(
            _explicit_concept_candidates(
                "We considered Stoicism and Buddhism."
            ),
            ("Buddhism", "Stoicism"),
        )
        self.assertEqual(
            _explicit_concept_candidates(
                "We are building fractal monistic data."
            ),
            ("Fractal Monism",),
        )

    @staticmethod
    def stance_packet(
        source: TrustedExtractionSource,
        *,
        topic_key: str,
        topic_text: str,
        position: str,
    ) -> ProviderPacket:
        return ProviderPacket.model_validate(
            _packet(
                entities=[
                    _example_entity(
                        source.content,
                        entity_ref="e00",
                        entity_type="self",
                        mention_kind="self_reference",
                        name_text=None,
                        relationship_role="user:self",
                        reason_code="explicit_self_reference",
                    )
                ],
                observations=[
                    _example_observation(
                        source.content,
                        observation_ref="o00",
                        subject_entity_ref="e00",
                        predicate="stance.reported",
                        object_value=_literal(
                            "json",
                            {
                                "topic_key": topic_key,
                                "topic_text": topic_text,
                                "position": position,
                                "orientation": "supports",
                                "context": None,
                            },
                        ),
                        projection_class="reported_stance",
                        surface_policy=(
                            "relevant_recall_or_explicit_recall"
                        ),
                        sensitivity="medium",
                        reason_code="explicit_reported_stance",
                        modality="reported_belief",
                    )
                ],
            )
        )

    def test_generic_definite_description_fails_closed(self) -> None:
        source, context = source_and_context()
        packet = self.stance_packet(
            source,
            topic_key="philosophy.life_impact",
            topic_text="philosophy and its impact on life",
            position="the philosophy will help people in life",
        )
        self.assertEqual(
            _unresolved_context_coreferences(source, context, packet),
            ("philosophy",),
        )

    def test_unique_sibling_concept_is_bound_without_assertion_transfer(
        self,
    ) -> None:
        source, context = source_and_context()
        bindings = _context_coreference_bindings(source, context)
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0]["head"], "philosophy")
        self.assertEqual(bindings[0]["referent"], "Fractal Monism")
        self.assertEqual(bindings[0]["referent_key"], "fractal_monism")
        self.assertEqual(bindings[0]["context_distance"], 2)

        generic = self.stance_packet(
            source,
            topic_key="philosophy.fractal_monism_life_impact",
            topic_text="philosophy and its impact on life",
            position="the philosophy will help people in life",
        )
        generic_value = generic.model_dump(mode="json")
        generic_value["entity_mentions"][0]["source_spans"][0]["end"] = 35
        generic_value["observations"][0]["source_spans"][0]["end"] = 35
        generic = ProviderPacket.model_validate(generic_value)
        repaired, repairs = _apply_context_coreference_bindings(
            source,
            context,
            generic,
        )
        self.assertEqual(
            repairs,
            (
                "context_target_source_spans_canonicalized",
                "context_coreference_bound",
            ),
        )
        self.assertEqual(
            _unresolved_context_coreferences(source, context, repaired),
            (),
        )
        value = repaired.model_dump(mode="json")
        self.assertEqual(len(value["entity_mentions"]), 1)
        self.assertEqual(len(value["observations"]), 1)
        observation = value["observations"][0]
        self.assertEqual(
            observation["object"]["value"]["topic_key"],
            "fractal_monism.life_impact",
        )
        self.assertEqual(
            observation["object"]["value"]["topic_text"],
            "Fractal Monism and its impact on life",
        )
        self.assertEqual(
            observation["object"]["value"]["position"],
            "Fractal Monism will help people in life",
        )
        self.assertEqual(
            observation["source_spans"][0]["quote"],
            source.content,
        )
        self.assertEqual(
            observation["source_spans"][0]["end"],
            len(source.content),
        )
        self.assertEqual(
            value["entity_mentions"][0]["source_spans"][0]["end"],
            len(source.content),
        )
        self.assertNotIn(
            context.spans[0].content,
            observation["source_spans"][0]["quote"],
        )

    def test_nearest_explicit_sibling_referent_passes(self) -> None:
        source, context = source_and_context()
        packet = self.stance_packet(
            source,
            topic_key="fractal_monism.life_impact",
            topic_text="Fractal Monism and its impact on life",
            position="Fractal Monism will help people in life",
        )
        self.assertEqual(
            _unresolved_context_coreferences(source, context, packet),
            (),
        )
        self.assertEqual(
            EVIDENCE_CONTEXT_COREFERENCE_VERSION,
            "memory_v1_evidence_context_coreference_v1",
        )

    def test_bound_topic_namespace_normalizes_idempotently(self) -> None:
        source, context = source_and_context()
        packet = self.stance_packet(
            source,
            topic_key="fractal_monism.fractal_monism_life_impact",
            topic_text="Fractal Monism",
            position="Fractal Monism will help people in life",
        )
        repaired, repairs = _apply_context_coreference_bindings(
            source,
            context,
            packet,
        )
        self.assertEqual(repairs, ("context_coreference_bound",))
        self.assertEqual(
            repaired.model_dump(mode="json")["observations"][0][
                "object"
            ]["value"]["topic_key"],
            "fractal_monism.life_impact",
        )

    def test_context_target_mismatch_fails_closed(self) -> None:
        source, context = source_and_context()
        mismatched = TrustedExtractionSource.create(
            job_id=source.job_id,
            source_system=source.source_system,
            source_external_id=(
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            ),
            source_sha256=source.source_sha256,
            source_recorded_at=source.source_recorded_at,
            content=source.content,
        )
        with self.assertRaisesRegex(
            ValueError,
            "context target differs",
        ):
            self.provider().request(
                mismatched,
                evidence_context=context,
            )

    def test_plain_request_remains_unchanged(self) -> None:
        source, _ = source_and_context()
        request = self.provider().request(source)
        self.assertEqual(
            request.prompt_profile,
            "semantic_stance_compact_v1",
        )
        self.assertNotIn("EVIDENCE_CONTEXT", request.input_text)
        self.assertNotIn("EVIDENCE_CONTEXT_RULES_V1", request.instructions)

    def test_third_person_role_context_uses_compact_registry(self) -> None:
        target_content = (
            "I talked to Bob Fry who was the president at the time."
        )
        target_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        sibling_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        sibling_content = (
            "The organization was important to my early career."
        )
        source_text = f"{sibling_content} {target_content}"
        target_start = source_text.index(target_content)
        rows = [
            {
                "evidence_id": sibling_id,
                "owner_user_id": OWNER,
                "source_system": "public.chat_log",
                "content": sibling_content,
                "content_sha256": sha(sibling_content),
                "metadata": {
                    "source_id": SOURCE_ID,
                    "thread_id": THREAD_ID,
                    "request_id": REQUEST_ID,
                    "source_content_sha256": sha(source_text),
                    "source_char_start": 0,
                    "source_char_end": len(sibling_content),
                    "primary_lane": "contextual_personal_history",
                    "epistemic_role": "user_report",
                    "span_origin": "compound_child",
                },
            },
            {
                "evidence_id": target_id,
                "owner_user_id": OWNER,
                "source_system": "public.chat_log",
                "content": target_content,
                "content_sha256": sha(target_content),
                "metadata": {
                    "source_id": SOURCE_ID,
                    "thread_id": THREAD_ID,
                    "request_id": REQUEST_ID,
                    "source_content_sha256": sha(source_text),
                    "source_char_start": target_start,
                    "source_char_end": len(source_text),
                    "primary_lane": "contextual_personal_history",
                    "epistemic_role": "user_report",
                    "span_origin": "compound_child",
                },
            },
        ]
        context = build_memory_evidence_context_envelope_v1(
            expected_owner_user_id=OWNER,
            target_evidence_id=target_id,
            expected_target_content_sha256=sha(target_content),
            source_row={
                "id": SOURCE_ID,
                "owner_user_id": OWNER,
                "thread_id": THREAD_ID,
                "request_id": REQUEST_ID,
                "created_at": "2026-07-02T02:05:41.345358+00:00",
                "text": source_text,
            },
            evidence_rows=rows,
        )
        source = TrustedExtractionSource.create(
            job_id="00000000-0000-4000-8000-000000000001",
            source_system="public.chat_log",
            source_external_id=target_id,
            source_sha256=sha(target_content),
            source_recorded_at="2026-07-02T02:05:41.345358+00:00",
            content=target_content,
        )
        request = self.provider().request(
            source,
            evidence_context=context,
        )
        self.assertEqual(
            request.prompt_profile,
            "personal_context_compact_v1_sibling_context_v1",
        )
        predicate = request.output_schema["$defs"]["ProviderObservation"][
            "properties"
        ]["predicate"]
        self.assertEqual(predicate["enum"], ["occupation.works_as"])
        self.assertLess(len(request.instructions), 16_000)


if __name__ == "__main__":
    unittest.main()
