from __future__ import annotations

import copy
import hashlib
import unittest

from rag_engine.memory_v1_evidence_context_v1 import (
    EvidenceContextContractError,
    build_memory_evidence_context_envelope_v1,
)
from rag_engine.memory_v1_evidence_context_v2 import (
    CONTRACT_VERSION,
    build_memory_evidence_context_envelope_v2,
    sanitized_evidence_context_report_v2,
)


OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
OTHER = "557ea042-cb82-48f8-9429-472e96c957ef"
THREAD = "2f6e6a6c-f43e-45e2-8137-8b1b01e67976"
SOURCE = "ed91f3b9-a4b8-4e63-aac7-53e370412483"
TARGET = "049205b4-9a6c-5e1a-bb8f-2ab9f05f8964"
REQUEST = "bfca3e63-e670-4601-a06d-6345c18554f4"
TEXT = "That was when I was in my early twenties."


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sibling_context():
    source = {
        "id": SOURCE,
        "owner_user_id": OWNER,
        "thread_id": THREAD,
        "request_id": REQUEST,
        "created_at": "2026-07-28T12:00:00+00:00",
        "text": TEXT,
    }
    evidence = [{
        "evidence_id": TARGET,
        "owner_user_id": OWNER,
        "source_system": "public.chat_log",
        "content": TEXT,
        "content_sha256": sha(TEXT),
        "metadata": {
            "source_id": SOURCE,
            "thread_id": THREAD,
            "request_id": REQUEST,
            "source_content_sha256": sha(TEXT),
            "source_char_start": 0,
            "source_char_end": len(TEXT),
            "primary_lane": "personal_history",
            "epistemic_role": "user_report",
            "span_origin": "atomic",
        },
    }]
    return build_memory_evidence_context_envelope_v1(
        expected_owner_user_id=OWNER,
        target_evidence_id=TARGET,
        expected_target_content_sha256=sha(TEXT),
        source_row=source,
        evidence_rows=evidence,
    )


def turns():
    return [
        {
            "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "owner_user_id": OWNER,
            "thread_id": THREAD,
            "request_id": "request-1",
            "created_at": "2026-07-28T11:58:00+00:00",
            "source": "frontend/chat:user",
            "text": "I began studying martial arts after high school.",
        },
        {
            "id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "owner_user_id": OWNER,
            "thread_id": THREAD,
            "request_id": "request-2",
            "created_at": "2026-07-28T11:59:00+00:00",
            "source": "frontend/chat:assistant",
            "text": "When did you begin studying martial arts?",
        },
    ]


class EvidenceContextV2Test(unittest.TestCase):
    def test_prior_turns_are_bounded_and_non_authoritative(self) -> None:
        envelope = build_memory_evidence_context_envelope_v2(
            sibling_context=sibling_context(),
            prior_turn_rows=turns(),
        )
        self.assertEqual(envelope.contract_version, CONTRACT_VERSION)
        self.assertEqual(
            [turn.speaker_role for turn in envelope.prior_turns],
            ["user", "assistant"],
        )
        self.assertEqual(
            [turn.context_distance for turn in envelope.prior_turns],
            [2, 1],
        )
        self.assertEqual(envelope.allowed_assertion_evidence_ids, (TARGET,))
        self.assertTrue(all(
            not turn.assertion_origin_allowed
            and not turn.instruction_capability
            for turn in envelope.prior_turns
        ))
        envelope.validate_hash()

    def test_reordered_rows_have_same_hash(self) -> None:
        forward = build_memory_evidence_context_envelope_v2(
            sibling_context=sibling_context(),
            prior_turn_rows=turns(),
        )
        reverse = build_memory_evidence_context_envelope_v2(
            sibling_context=sibling_context(),
            prior_turn_rows=list(reversed(turns())),
        )
        self.assertEqual(forward.envelope_sha256, reverse.envelope_sha256)

    def test_cross_owner_and_thread_fail_closed(self) -> None:
        for field, value, message in (
            ("owner_user_id", OTHER, "owner mismatch"),
            (
                "thread_id",
                "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                "thread mismatch",
            ),
        ):
            rows = copy.deepcopy(turns())
            rows[0][field] = value
            with self.assertRaisesRegex(
                EvidenceContextContractError,
                message,
            ):
                build_memory_evidence_context_envelope_v2(
                    sibling_context=sibling_context(),
                    prior_turn_rows=rows,
                )

    def test_future_context_fails_closed(self) -> None:
        rows = copy.deepcopy(turns())
        rows[0]["created_at"] = "2026-07-28T12:01:00+00:00"
        with self.assertRaisesRegex(
            EvidenceContextContractError,
            "not prior",
        ):
            build_memory_evidence_context_envelope_v2(
                sibling_context=sibling_context(),
                prior_turn_rows=rows,
            )

    def test_budget_uses_nearest_tail_without_authority(self) -> None:
        rows = turns()
        rows[1]["text"] = "prefix " + ("x" * 20) + " nearest question"
        envelope = build_memory_evidence_context_envelope_v2(
            sibling_context=sibling_context(),
            prior_turn_rows=rows,
            max_prior_turns=1,
            max_context_chars=16,
            max_turn_chars=16,
        )
        self.assertEqual(len(envelope.prior_turns), 1)
        self.assertEqual(
            envelope.prior_turns[0].content,
            "nearest question",
        )
        self.assertGreater(
            envelope.prior_turns[0].source_char_start,
            0,
        )

    def test_sanitized_report_contains_no_source_text(self) -> None:
        envelope = build_memory_evidence_context_envelope_v2(
            sibling_context=sibling_context(),
            prior_turn_rows=turns(),
        )
        report = sanitized_evidence_context_report_v2(envelope)
        rendered = str(report)
        self.assertNotIn("martial arts", rendered)
        self.assertEqual(report["prior_turn_count"], 2)
        self.assertEqual(report["prior_turn_assertion_origin_count"], 0)
        self.assertFalse(report["raw_context_text_persisted"])


if __name__ == "__main__":
    unittest.main()
