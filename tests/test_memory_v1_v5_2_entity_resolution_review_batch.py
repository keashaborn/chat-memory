from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid

from scripts.memory_v1_v5_2_entity_resolution_review_batch import (
    AUTHORIZATION_CONTRACT,
    CONFIRMATION,
    MANIFEST_CONTRACT,
    ReviewOnlyError,
    load_authorization,
    load_manifest,
    load_spec,
    normalized_item,
    sha256_bytes,
)


OWNER = "11111111-1111-4111-8111-111111111111"
ENTITY = "22222222-2222-4222-8222-222222222222"
EVIDENCE_LINK = "33333333-3333-4333-8333-333333333333"
EVIDENCE_CREATE = "44444444-4444-4444-8444-444444444444"
HEAD = "a" * 40


def write_json(path: Path, value: dict, mode: int) -> bytes:
    raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    path.write_bytes(raw)
    os.chmod(path, mode)
    return raw


def spec_value() -> dict:
    return {
        "contract_version": "memory_v1_v5_2_entity_review_spec_v1",
        "target_server": "seebx",
        "owner_user_id": OWNER,
        "items": [
            {
                "evidence_id": EVIDENCE_LINK,
                "entity_ref": "e00",
                "entity_type": "animal",
                "name_text": "Existing Pet",
                "expected_action": "link_existing",
                "expected_selected_entity_id": ENTITY,
                "expected_proposed_entity": None,
                "expected_review_reason_codes": [
                    "relationship_role_unverified"
                ],
                "review_reason": "Review exact existing pet without applying.",
            },
            {
                "evidence_id": EVIDENCE_CREATE,
                "entity_ref": "e00",
                "entity_type": "animal",
                "name_text": "New Pet",
                "expected_action": "create_new",
                "expected_selected_entity_id": None,
                "expected_proposed_entity": {
                    "entity_type": "animal",
                    "identity_state": "named",
                    "canonical_name": "New Pet",
                    "display_label": "New Pet",
                    "creation_reason": "new_named_entity_no_exact_owner_match",
                },
                "expected_review_reason_codes": [
                    "new_named_entity_requires_review"
                ],
                "review_reason": "Review exact new pet without applying.",
            },
        ],
    }


def planner_item(expected: dict, index: int) -> dict:
    return {
        "entity_ref": expected["entity_ref"],
        "entity_type": expected["entity_type"],
        "name_text": expected["name_text"],
        "action": expected["expected_action"],
        "decision_state": "manual_review_required",
        "selected_entity_id": expected["expected_selected_entity_id"],
        "proposed_entity": expected["expected_proposed_entity"],
        "review_reason_codes": expected["expected_review_reason_codes"],
        "existing_review_count": 0,
        "existing_apply_count": 0,
        "resolution_id": str(
            uuid.UUID(f"50000000-0000-4000-8000-{index:012d}")
        ),
        "mention_id": str(
            uuid.UUID(f"60000000-0000-4000-8000-{index:012d}")
        ),
        "mention_sha256": f"{index + 1:x}" * 64,
        "candidate_set_sha256": f"{index + 3:x}" * 64,
        "decision_sha256": f"{index + 5:x}" * 64,
    }


class EntityReviewBatchTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repository = Path(directory.name)
        self.review = self.repository / "review"
        self.review.mkdir(mode=0o700)
        self.spec_path = self.repository / "spec.json"
        self.spec_raw = write_json(self.spec_path, spec_value(), 0o644)

    def expected_items(self) -> list[dict]:
        return spec_value()["items"]

    def normalized_items(self) -> list[dict]:
        return [
            normalized_item(planner_item(item, index), item)
            for index, item in enumerate(self.expected_items(), start=1)
        ]

    def manifest_path(self) -> Path:
        value = {
            "contract_version": MANIFEST_CONTRACT,
            "target_server": "seebx",
            "owner_user_id": OWNER,
            "required_head_commit": HEAD,
            "spec_path": str(self.spec_path),
            "spec_sha256": sha256_bytes(self.spec_raw),
            "expected_item_count": 2,
            "expected_new_rows": 4,
            "items": self.normalized_items(),
        }
        path = self.review / "manifest.json"
        write_json(path, value, 0o600)
        return path

    def test_spec_supports_link_existing_and_create_new(self):
        spec, path, digest = load_spec(str(self.spec_path), self.repository)
        self.assertEqual(path, self.spec_path)
        self.assertEqual(digest, sha256_bytes(self.spec_raw))
        self.assertEqual(spec["owner_user_id"], OWNER)
        self.assertEqual(
            [item["expected_action"] for item in spec["items"]],
            ["link_existing", "create_new"],
        )

    def test_spec_rejects_link_without_selected_entity(self):
        value = spec_value()
        value["items"][0]["expected_selected_entity_id"] = None
        write_json(self.spec_path, value, 0o644)
        with self.assertRaisesRegex(
            ReviewOnlyError, "expected_selected_entity_id"
        ):
            load_spec(str(self.spec_path), self.repository)

    def test_spec_rejects_create_with_selected_entity(self):
        value = spec_value()
        value["items"][1]["expected_selected_entity_id"] = ENTITY
        write_json(self.spec_path, value, 0o644)
        with self.assertRaisesRegex(
            ReviewOnlyError, "proposed entity"
        ):
            load_spec(str(self.spec_path), self.repository)

    def test_normalized_item_rejects_resolution_drift(self):
        expected = self.expected_items()[0]
        raw = planner_item(expected, 1)
        raw["selected_entity_id"] = str(uuid.uuid4())
        with self.assertRaisesRegex(ReviewOnlyError, "candidate drifted"):
            normalized_item(raw, expected)

    def test_manifest_is_bound_to_spec_and_dynamic_counts(self):
        manifest, digest = load_manifest(
            str(self.manifest_path()), self.review, self.repository, HEAD
        )
        self.assertEqual(manifest["owner_user_id"], OWNER)
        self.assertEqual(manifest["expected_item_count"], 2)
        self.assertEqual(manifest["expected_new_rows"], 4)
        self.assertEqual(len(digest), 64)

    def test_manifest_rejects_spec_tamper(self):
        path = self.manifest_path()
        value = spec_value()
        value["items"][0]["name_text"] = "Changed"
        write_json(self.spec_path, value, 0o644)
        with self.assertRaisesRegex(
            ReviewOnlyError, "stale or invalid"
        ):
            load_manifest(str(path), self.review, self.repository, HEAD)

    def test_authorization_uses_plan_owner_and_dynamic_budget(self):
        now = dt.datetime.now(dt.timezone.utc)
        plan = {
            "owner_user_id": OWNER,
            "item_count": 2,
            "expected_new_rows": 4,
        }
        plan_sha = "b" * 64
        value = {
            "contract_version": AUTHORIZATION_CONTRACT,
            "authorization_id": str(uuid.uuid4()),
            "authorized": True,
            "authorized_by": "Eric Lund",
            "authorized_at": now.isoformat(),
            "expires_at": (now + dt.timedelta(minutes=20)).isoformat(),
            "expected_head_commit": HEAD,
            "target_server": "seebx",
            "scope": "review_owner_v5_2_entity_resolutions_without_apply",
            "owner_user_id": OWNER,
            "plan_sha256": plan_sha,
            "expected_item_count": 2,
            "expected_new_rows": 4,
            "confirmation": CONFIRMATION,
        }
        path = self.review / "authorization.json"
        write_json(path, value, 0o600)
        authorization, digest = load_authorization(
            str(path), self.review, plan, plan_sha, HEAD
        )
        self.assertEqual(authorization["owner_user_id"], OWNER)
        self.assertEqual(len(digest), 64)


if __name__ == "__main__":
    unittest.main()
