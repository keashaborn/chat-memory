from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from memory_v1_projection_v5_contract_test import sha256  # noqa: E402
from memory_v1_v5_claim_projection_review_manifest import (  # noqa: E402
    ManifestError,
    load_bundle,
)


OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"


def bundle(contract_version: str) -> dict[str, object]:
    value: dict[str, object] = {
        "contract_version": contract_version,
        "owner_user_id": OWNER,
        "database_writes": 0,
        "external_model_calls": 0,
        "qdrant_writes": 0,
        "plan_id": str(uuid.uuid4()),
        "packet": {
            "projections": [{
                "projection_ref": "p01",
                "lane": "claim",
                "target": {"action": "create"},
                "review": {
                    "state": "manual_review_required",
                    "authorization_required": True,
                },
            }],
        },
    }
    value["bundle_sha256"] = sha256(value)
    return value


class ReviewManifestBundleContractTest(unittest.TestCase):
    def test_accepts_only_known_stage_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for contract_version in (
                "memory_v1_claim_projection_stage_bundle_v5_1",
                "memory_v1_relationship_claim_stage_bundle_v5_1",
            ):
                path = Path(directory) / f"{contract_version}.json"
                path.write_text(json.dumps(bundle(contract_version)), encoding="utf-8")
                self.assertEqual(
                    load_bundle(path, OWNER)["contract_version"], contract_version
                )

    def test_rejects_unknown_stage_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bundle.json"
            path.write_text(
                json.dumps(bundle("memory_v1_unknown_stage_bundle")), encoding="utf-8"
            )
            with self.assertRaisesRegex(ManifestError, "bundle boundary or hash mismatch"):
                load_bundle(path, OWNER)


if __name__ == "__main__":
    unittest.main()
