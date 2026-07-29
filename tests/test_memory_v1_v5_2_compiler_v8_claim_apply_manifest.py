from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = (
    Path(__file__).parents[1]
    / "scripts"
    / "memory_v1_v5_2_compiler_v8_claim_apply_manifest.py"
)
SPEC = importlib.util.spec_from_file_location("compiler_v8_claim_apply_manifest", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CompilerV8ClaimApplyManifestTest(unittest.TestCase):
    def test_exact_four_reviewed_targets_are_hash_locked(self) -> None:
        self.assertEqual(MODULE.TARGETS, {
        "8f7b5442-9b8e-5417-b630-794eaa1429fd": {
            "observation_id": "70d55f38-1e33-418f-8ec6-6bfd2051f4e6",
            "predicate": "relationship.caregiver_for",
            "canonical_text": "The user is a caregiver for Monika.",
        },
        "974f7beb-f6a3-5231-ad52-0a7468d43533": {
            "observation_id": "bbd94cc7-e9d5-429f-8af1-1a029b119db0",
            "predicate": "relationship.spouse_of",
            "canonical_text": "The user is a spouse of Monika.",
        },
        "9e94e09c-dcb2-53e9-a0a5-77dd2971d6d1": {
            "observation_id": "a0ea633d-96df-4ad8-a0c1-b3f4f84e30cc",
            "predicate": "occupation.works_as",
            "canonical_text": "The user formerly worked as BCBA.",
        },
        "fe8110e2-2ff5-5e44-add5-f8380331041d": {
            "observation_id": "c8ce8cd0-e058-4181-ae94-fd6fb1e7c6eb",
            "predicate": "occupation.works_as",
            "canonical_text": "The user formerly worked as clinical psychologist.",
        },
        })
        self.assertEqual({
            hashlib.sha256(item["canonical_text"].encode()).hexdigest()
            for item in MODULE.TARGETS.values()
        }, {
        "e2d3a129f284ec613c0f3de7a52ff41db5c98ddb03bc1f2c5baa3c08377820b9",
        "fbfda600ab019ca8adeeacbf6b7ea4b83a1a0e17c7adef283d4bf9b0229dad6c",
        "a72b43f97962f40802db681f011eba26cd1bfb94a572682aa6fa5193f1323e57",
        "374ee893d9519215a09dd42bcc16857cee88987186be85510ce63e2f25199be0",
        })

    def test_materialization_budget_defers_projection_outbox(self) -> None:
        self.assertEqual(MODULE.EXPECTED_ROWS_PER_ITEM, {
        "claim": 1,
        "claim_revision": 2,
        "claim_observation": 1,
        "projection_apply_event": 1,
        "projection_dispatch_v5": 1,
        "claim_assessment_review_v5": 1,
        "claim_assessment": 1,
        "claim_assessment_apply_v5": 1,
        "relational_operation_request": 2,
        "projection_outbox": 0,
        })
        self.assertEqual(sum(MODULE.EXPECTED_ROWS_PER_ITEM.values()), 11)

    def test_request_ids_are_deterministic_and_operation_specific(self) -> None:
        owner = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
        plan = "8f7b5442-9b8e-5417-b630-794eaa1429fd"
        first = MODULE.request_id(owner, plan, "projection-apply")
        self.assertEqual(first, MODULE.request_id(owner, plan, "projection-apply"))
        self.assertNotEqual(first, MODULE.request_id(owner, plan, "assessment-review"))
        self.assertNotEqual(first, MODULE.request_id(owner, plan, "assessment-apply"))

    def test_assessment_is_supported_not_truth_absolute(self) -> None:
        self.assertEqual(MODULE.ASSESSMENT["action"], "promote_supported")
        self.assertEqual(MODULE.ASSESSMENT["support_score"], "1.000")
        self.assertEqual(MODULE.ASSESSMENT["opposition_score"], "0.000")
        self.assertEqual(MODULE.ASSESSMENT["claim_confidence"], "0.920")

    def test_mixed_review_batch_materializes_only_authorized_targets(self) -> None:
        target_plan = next(iter(MODULE.TARGETS))
        deferred_plan = "00000000-0000-4000-8000-000000000001"
        target_observation = MODULE.TARGETS[target_plan]["observation_id"]
        review_manifest = {
            "items": [
                {
                    "plan_id": target_plan,
                    "observation_id": target_observation,
                    "decision": "authorized",
                },
                {
                    "plan_id": deferred_plan,
                    "observation_id": "00000000-0000-4000-8000-000000000002",
                    "decision": "deferred",
                },
            ]
        }
        review_result = {
            "outcomes": [
                {
                    "plan_id": target_plan,
                    "observation_id": target_observation,
                    "decision": "authorized",
                    "outcome": "applied",
                    "rows_written": 1,
                },
                {
                    "plan_id": deferred_plan,
                    "observation_id": "00000000-0000-4000-8000-000000000002",
                    "decision": "deferred",
                    "outcome": "applied",
                    "rows_written": 1,
                },
            ]
        }
        original = MODULE.TARGETS
        MODULE.TARGETS = {target_plan: original[target_plan]}
        try:
            reviewed, staged = MODULE.authorized_review_items(
                review_manifest, review_result
            )
        finally:
            MODULE.TARGETS = original
        self.assertEqual(set(reviewed), {target_plan})
        self.assertEqual(set(staged), {target_plan})


if __name__ == "__main__":
    unittest.main()
