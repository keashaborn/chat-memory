from __future__ import annotations

from dataclasses import asdict
import json
import unittest

from rag_engine.governed_memory.contracts import ContractViolation, canonical_sha256
from rag_engine.governed_memory.runtime.qdrant_adapter import (
    QDRANT_ALIAS,
    QDRANT_PHYSICAL_COLLECTION,
)
from rag_engine.governed_memory.runtime.rebuild_application import (
    prepare_receipt_from_json,
)
from rag_engine.governed_memory.runtime.rebuild_controller import (
    RebuildPrepareReceipt,
)


TARGET = "governed_memory_9a54cf123493_000002"


def receipt() -> RebuildPrepareReceipt:
    material = {
        "alias": QDRANT_ALIAS,
        "manifest_sha256": "a" * 64,
        "point_count": 1,
        "source_collection": QDRANT_PHYSICAL_COLLECTION,
        "target_collection": TARGET,
        "target_verification_receipt_sha256": "b" * 64,
    }
    return RebuildPrepareReceipt(
        **material,
        receipt_sha256=canonical_sha256(
            "governed_memory.successor_rebuild_prepare_receipt", material
        ),
    )


class RebuildApplicationTests(unittest.TestCase):
    def test_prepare_receipt_json_round_trip_is_closed(self) -> None:
        expected = receipt()
        raw = json.dumps(asdict(expected), sort_keys=True).encode("utf-8")
        self.assertEqual(prepare_receipt_from_json(raw), expected)

    def test_prepare_receipt_json_rejects_duplicate_or_extra_fields(self) -> None:
        expected = receipt()
        value = asdict(expected)
        value["unexpected"] = True
        with self.assertRaisesRegex(
            ContractViolation, "invalid_rebuild_prepare_receipt_json"
        ):
            prepare_receipt_from_json(json.dumps(value).encode("utf-8"))
        with self.assertRaisesRegex(
            ContractViolation, "invalid_rebuild_prepare_receipt_json"
        ):
            prepare_receipt_from_json(b'{"alias":"a","alias":"b"}')


if __name__ == "__main__":
    unittest.main()
