from __future__ import annotations

import unittest
from uuid import UUID

from rag_engine.memory_v1_answer_provenance_v1 import (
    build_governed_memory_answer_provenance_v1,
)
from rag_engine.memory_v1_selection_envelope import (
    FinalAnswerMemoryBindingV1,
    MemoryAnswerBindingItemV1,
    MemoryLane,
    MemoryRecordRefV1,
)


OWNER = UUID("11111111-1111-4111-8111-111111111111")
OTHER_OWNER = UUID("99999999-9999-4999-8999-999999999999")
HASH = "a" * 64


def _record(owner: UUID, record_number: int, rank: int) -> MemoryRecordRefV1:
    return MemoryRecordRefV1(
        owner_user_id=owner,
        lane=MemoryLane.CLAIM,
        record_id=UUID(f"22222222-2222-4222-8222-{record_number:012d}"),
        revision_id=UUID(f"33333333-3333-4333-8333-{record_number:012d}"),
        source_content_sha256=HASH,
        rank=rank,
    )


def _binding(
    *,
    items: tuple[MemoryAnswerBindingItemV1, ...],
    owner: UUID = OWNER,
    selected_count: int | None = None,
) -> FinalAnswerMemoryBindingV1:
    injected_count = sum(item.injected for item in items)
    exposed_count = sum(item.answer_model_exposed for item in items)
    return FinalAnswerMemoryBindingV1.model_construct(
        owner_user_id=owner,
        items=items,
        selected_count=len(items) if selected_count is None else selected_count,
        injected_count=injected_count,
        exposed_count=exposed_count,
        selected_control_count=0,
        applied_control_count=0,
        outcome=("exposed" if exposed_count else "selected_not_injected"),
        binding_manifest_sha256=HASH,
    )


class MemoryAnswerProvenanceV1Tests(unittest.TestCase):
    def test_absent_binding_is_an_explicit_empty_contract(self) -> None:
        value = build_governed_memory_answer_provenance_v1(None)
        self.assertEqual(value.binding_outcome, "no_memory_binding")
        self.assertEqual(value.references, ())
        self.assertFalse(value.semantic_use_verified)

    def test_only_model_exposed_records_are_referenced(self) -> None:
        selected_only = MemoryAnswerBindingItemV1(
            record=_record(OWNER, 1, 1),
            injected=False,
            answer_model_exposed=False,
            actual_prompt_tokens=0,
            rendered_fragment_sha256=None,
        )
        exposed = MemoryAnswerBindingItemV1(
            record=_record(OWNER, 2, 2),
            injected=True,
            answer_model_exposed=True,
            actual_prompt_tokens=12,
            rendered_fragment_sha256="b" * 64,
        )
        value = build_governed_memory_answer_provenance_v1(
            _binding(items=(selected_only, exposed))
        )
        self.assertEqual(value.selected_count, 2)
        self.assertEqual(value.injected_count, 1)
        self.assertEqual(value.model_exposed_count, 1)
        self.assertEqual(len(value.references), 1)
        self.assertEqual(value.references[0].record_id, exposed.record.record_id)
        self.assertTrue(value.references[0].model_exposed)
        self.assertEqual(
            value.provenance_basis,
            "final_answer_binding_model_exposure_not_semantic_use",
        )
        dumped = value.model_dump(mode="json")
        self.assertNotIn("owner_user_id", str(dumped))
        self.assertNotIn("source_content_sha256", str(dumped))

    def test_inconsistent_or_cross_owner_binding_fails_closed(self) -> None:
        exposed = MemoryAnswerBindingItemV1(
            record=_record(OTHER_OWNER, 3, 1),
            injected=True,
            answer_model_exposed=True,
            actual_prompt_tokens=10,
            rendered_fragment_sha256="c" * 64,
        )
        with self.assertRaisesRegex(ValueError, "cross-owner"):
            build_governed_memory_answer_provenance_v1(
                _binding(items=(exposed,))
            )

        ordinary = MemoryAnswerBindingItemV1(
            record=_record(OWNER, 4, 1),
            injected=False,
            answer_model_exposed=False,
            actual_prompt_tokens=0,
            rendered_fragment_sha256=None,
        )
        with self.assertRaisesRegex(ValueError, "selected count"):
            build_governed_memory_answer_provenance_v1(
                _binding(items=(ordinary,), selected_count=2)
            )


if __name__ == "__main__":
    unittest.main()
