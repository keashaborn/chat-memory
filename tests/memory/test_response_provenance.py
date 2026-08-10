from __future__ import annotations

import unittest
import json
from uuid import UUID

from pydantic import ValidationError

from rag_engine.governed_memory.response_provenance import (
    SuccessorMemoryNotApplicableReason,
    SuccessorMemoryAnswerProvenanceV1,
    build_successor_exposed_provenance_v1,
    build_successor_no_memory_selected_provenance_v1,
    build_successor_not_applicable_provenance_v1,
)


ANSWER = UUID("99999999-9999-4999-8999-999999999999")
CLAIM = "ffffffff-ffff-4fff-8fff-ffffffffffff"
REVISION = "12345678-1234-4234-8234-123456789abc"


def exposed():
    return build_successor_exposed_provenance_v1(
        {
            "dispatch_state": "dispatched",
            "outcome": "exposed",
            "response_id": str(ANSWER),
            "prompt_sha256": "a" * 64,
            "outbound_request_sha256": "b" * 64,
            "binding_sha256": "c" * 64,
            "selection_manifest_sha256": "d" * 64,
            "injection_manifest_sha256": "c" * 64,
            "selected_count": 1,
            "injected_count": 1,
            "model_exposed_count": 1,
            "injected_claim_ids": (CLAIM,),
            "injected_revision_ids": (REVISION,),
            "owner_user_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "session_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "memory_block": "synthetic private memory text",
        }
    )


class SuccessorMemoryAnswerProvenanceTests(unittest.TestCase):
    def test_exposed_receipt_is_hash_bound_and_content_free(self) -> None:
        value = exposed()
        self.assertEqual(value.binding_outcome, "exposed")
        self.assertEqual(value.binding_manifest_sha256, "c" * 64)
        self.assertEqual(value.references[0].claim_id, UUID(CLAIM))
        self.assertEqual(value.references[0].revision_id, UUID(REVISION))
        self.assertEqual(value.references[0].rank, 1)
        self.assertFalse(value.semantic_use_verified)
        self.assertEqual(len(value.provenance_sha256), 64)
        dumped = value.model_dump(mode="json")
        self.assertNotIn("content", dumped)
        self.assertNotIn("memory_block", dumped)
        serialized = json.dumps(dumped, sort_keys=True)
        self.assertNotIn("owner_user_id", serialized)
        self.assertNotIn("session_id", serialized)
        self.assertNotIn("synthetic private memory text", serialized)
        self.assertNotIn("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", serialized)
        self.assertNotIn("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", serialized)

        dumped["provenance_sha256"] = "0" * 64
        with self.assertRaises(ValidationError):
            SuccessorMemoryAnswerProvenanceV1.model_validate(dumped)

    def test_no_selection_receipt_is_dispatch_bound_and_empty(self) -> None:
        first = build_successor_no_memory_selected_provenance_v1(
            answer_id=ANSWER,
            prompt_sha256="a" * 64,
            outbound_request_bytes=b'{"messages":[]}',
        )
        second = build_successor_no_memory_selected_provenance_v1(
            answer_id=ANSWER,
            prompt_sha256="a" * 64,
            outbound_request_bytes=b'{"messages":[]}',
        )
        self.assertEqual(first, second)
        self.assertEqual(first.binding_outcome, "no_memory_selected")
        self.assertIsNone(first.binding_manifest_sha256)
        self.assertEqual(first.references, ())
        self.assertEqual(first.selected_count, 0)

    def test_excluded_surface_has_one_truthful_bounded_reason(self) -> None:
        for reason in SuccessorMemoryNotApplicableReason:
            with self.subTest(reason=reason.value):
                value = build_successor_not_applicable_provenance_v1(
                    reason=reason,
                    answer_id=ANSWER,
                    prompt_sha256="a" * 64,
                    outbound_request_bytes=b'{"messages":[]}',
                )
                self.assertEqual(value.binding_outcome, "not_applicable")
                self.assertIs(value.not_applicable_reason, reason)
                self.assertIsNone(value.binding_manifest_sha256)
                self.assertIsNone(value.selection_manifest_sha256)
                self.assertIsNone(value.injection_manifest_sha256)
                self.assertEqual(value.references, ())
                self.assertEqual(value.selected_count, 0)


if __name__ == "__main__":
    unittest.main()
