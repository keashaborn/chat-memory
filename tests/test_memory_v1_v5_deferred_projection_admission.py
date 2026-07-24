from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parent
        / "scripts"
    ),
)

from memory_v1_projection_v5_contract_test import sha256  # noqa: E402
from memory_v1_v5_deferred_projection_admission import (  # noqa: E402
    DeferredProjectionAdmissionError,
    validate_sources,
)


OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
PLAN = "d286d420-2d28-5997-bc3c-9a9f550e61db"
CLAIM = "8fb8b3ab-a627-4555-99c6-fe4dc9b0ca89"


def artifacts() -> tuple[dict[str, object], dict[str, object]]:
    manifest: dict[str, object] = {
        "contract_version": "memory_v1_claim_projection_apply_batch_manifest_v1",
        "owner_user_id": OWNER,
        "required_head_commit": "a" * 40,
        "defer_projection_outbox": True,
        "expected_table_rows": {"projection_outbox": 0},
        "items": [
            {
                "plan_id": PLAN,
                "predicate": "stance.reported",
                "canonical_text_sha256": "b" * 64,
            }
        ],
    }
    manifest["manifest_sha256"] = sha256(manifest)
    apply: dict[str, object] = {
        "contract_version": "memory_v1_claim_projection_apply_batch_result_v1",
        "mode": "apply",
        "owner_user_id": OWNER,
        "manifest_sha256": manifest["manifest_sha256"],
        "projection_outbox_deferred": True,
        "qdrant_writes": 0,
        "outcomes": [
            {
                "plan_id": PLAN,
                "predicate": "stance.reported",
                "claim_id": CLAIM,
                "claim_revision_number": 2,
                "outbox_id": None,
                "outcome": "applied",
            }
        ],
    }
    apply["result_sha256"] = sha256(apply)
    return apply, manifest


class DeferredProjectionAdmissionArtifactTest(unittest.TestCase):
    def write(self, directory: str, name: str, value: object) -> Path:
        path = Path(directory) / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_accepts_exact_deferred_projection_sources(self) -> None:
        apply, manifest = artifacts()
        with tempfile.TemporaryDirectory() as directory:
            loaded_apply, loaded_manifest, claims = validate_sources(
                self.write(directory, "apply.json", apply),
                self.write(directory, "manifest.json", manifest),
            )
        self.assertTrue(loaded_apply["projection_outbox_deferred"])
        self.assertTrue(loaded_manifest["defer_projection_outbox"])
        self.assertEqual(claims[0]["claim_id"], CLAIM)

    def test_rejects_non_deferred_source(self) -> None:
        apply, manifest = artifacts()
        apply["projection_outbox_deferred"] = False
        apply["result_sha256"] = sha256(
            {key: value for key, value in apply.items() if key != "result_sha256"}
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                DeferredProjectionAdmissionError, "deferred projection boundary"
            ):
                validate_sources(
                    self.write(directory, "apply.json", apply),
                    self.write(directory, "manifest.json", manifest),
                )

    def test_rejects_preexisting_outbox_identity(self) -> None:
        apply, manifest = artifacts()
        apply["outcomes"][0]["outbox_id"] = (
            "11111111-1111-4111-8111-111111111111"
        )
        apply["result_sha256"] = sha256(
            {key: value for key, value in apply.items() if key != "result_sha256"}
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                DeferredProjectionAdmissionError, "outcome and manifest differ"
            ):
                validate_sources(
                    self.write(directory, "apply.json", apply),
                    self.write(directory, "manifest.json", manifest),
                )


if __name__ == "__main__":
    unittest.main()
