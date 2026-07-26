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
    LocalLlamaCppProvider,
)
from scripts.memory_v1_relational_extraction_v5_provider import (
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


if __name__ == "__main__":
    unittest.main()
