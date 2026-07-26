from __future__ import annotations

import copy
import hashlib
import unittest

from rag_engine.memory_v1_evidence_context_v1 import (
    CONTEXT_POLICY,
    CONTRACT_VERSION,
    EvidenceContextContractError,
    build_memory_evidence_context_envelope_v1,
    sanitized_evidence_context_report_v1,
)


OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
OTHER = "557ea042-cb82-48f8-9429-472e96c957ef"
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
    return hashlib.sha256(value.encode()).hexdigest()


def fixture() -> tuple[dict, list[dict]]:
    source = {
        "id": SOURCE_ID,
        "owner_user_id": OWNER,
        "thread_id": THREAD_ID,
        "request_id": REQUEST_ID,
        "created_at": "2026-07-02T02:05:41.345358+00:00",
        "text": SOURCE_TEXT,
    }
    spans = [
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
    ]
    evidence = []
    for evidence_id, start, end, lane, origin in spans:
        content = SOURCE_TEXT[start:end]
        evidence.append(
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
    return source, evidence


def build(source: dict, evidence: list[dict]):
    target = next(row for row in evidence if row["evidence_id"] == TARGET_ID)
    return build_memory_evidence_context_envelope_v1(
        expected_owner_user_id=OWNER,
        target_evidence_id=TARGET_ID,
        expected_target_content_sha256=target["content_sha256"],
        source_row=source,
        evidence_rows=evidence,
    )


class EvidenceContextV1Test(unittest.TestCase):
    def test_exact_sibling_context_is_ordered_and_target_bounded(self) -> None:
        source, evidence = fixture()
        envelope = build(source, list(reversed(evidence)))
        self.assertEqual(envelope.contract_version, CONTRACT_VERSION)
        self.assertEqual(envelope.context_policy, CONTEXT_POLICY)
        self.assertEqual(
            [span.context_role for span in envelope.spans],
            ["before", "before", "target"],
        )
        self.assertEqual(
            envelope.allowed_assertion_evidence_ids,
            (TARGET_ID,),
        )
        self.assertEqual(len(envelope.context_only_evidence_ids), 2)
        self.assertEqual(
            [
                span.assertion_origin_allowed
                for span in envelope.spans
            ],
            [False, False, True],
        )
        self.assertFalse(envelope.source.raw_source_text_retained)
        envelope.validate_hash()

    def test_reordered_rows_have_same_envelope_hash(self) -> None:
        source, evidence = fixture()
        forward = build(source, evidence)
        reverse = build(source, list(reversed(evidence)))
        self.assertEqual(
            forward.envelope_sha256,
            reverse.envelope_sha256,
        )

    def test_cross_owner_evidence_fails_closed(self) -> None:
        source, evidence = fixture()
        evidence[0]["owner_user_id"] = OTHER
        with self.assertRaisesRegex(
            EvidenceContextContractError,
            "evidence owner mismatch",
        ):
            build(source, evidence)

    def test_cross_owner_source_fails_closed(self) -> None:
        source, evidence = fixture()
        source["owner_user_id"] = OTHER
        with self.assertRaisesRegex(
            EvidenceContextContractError,
            "source owner mismatch",
        ):
            build(source, evidence)

    def test_source_hash_drift_fails_closed(self) -> None:
        source, evidence = fixture()
        source["text"] += " changed"
        with self.assertRaisesRegex(
            EvidenceContextContractError,
            "evidence full-source hash mismatch",
        ):
            build(source, evidence)

    def test_offset_drift_fails_closed(self) -> None:
        source, evidence = fixture()
        evidence[1]["metadata"]["source_char_start"] = 183
        with self.assertRaisesRegex(
            EvidenceContextContractError,
            "does not match its source offsets",
        ):
            build(source, evidence)

    def test_overlap_fails_closed_even_if_content_matches(self) -> None:
        source, evidence = fixture()
        duplicate = copy.deepcopy(evidence[0])
        duplicate["evidence_id"] = (
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        )
        evidence.append(duplicate)
        with self.assertRaisesRegex(
            EvidenceContextContractError,
            "evidence source spans overlap",
        ):
            build(source, evidence)

    def test_target_hash_mismatch_fails_closed(self) -> None:
        source, evidence = fixture()
        with self.assertRaisesRegex(
            EvidenceContextContractError,
            "target evidence content hash mismatch",
        ):
            build_memory_evidence_context_envelope_v1(
                expected_owner_user_id=OWNER,
                target_evidence_id=TARGET_ID,
                expected_target_content_sha256="0" * 64,
                source_row=source,
                evidence_rows=evidence,
            )

    def test_sanitized_report_contains_no_content(self) -> None:
        source, evidence = fixture()
        report = sanitized_evidence_context_report_v1(
            build(source, evidence)
        )
        rendered = str(report)
        self.assertNotIn("Much of the app", rendered)
        self.assertNotIn("philosophy will help", rendered)
        self.assertEqual(report["span_count"], 3)
        self.assertEqual(report["assertion_origin_count"], 1)
        self.assertEqual(report["context_only_span_count"], 2)
        self.assertFalse(report["retrieval_activation"])
        self.assertFalse(report["prompt_influence"])


if __name__ == "__main__":
    unittest.main()
