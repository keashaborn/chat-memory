#!/usr/bin/env python3
from __future__ import annotations

import unittest

from scripts.memory_v1_v5_2_pet_profile_claim_apply_manifest import (
    DEFERRED_OBSERVATION,
    configure,
    plan_targets,
)
from scripts.memory_v1_v5_2_pet_profile_claim_stage import TARGETS
from scripts import memory_v1_v5_2_compiler_v8_claim_apply_manifest as base


class PetProfileClaimApplyManifestTest(unittest.TestCase):
    def test_exactly_one_temporal_conflict_is_not_materialized(self) -> None:
        targets = plan_targets()
        self.assertEqual(len(TARGETS), 10)
        self.assertEqual(len(targets), 10)
        self.assertNotIn(
            DEFERRED_OBSERVATION,
            {target["observation_id"] for target in targets.values()},
        )
        self.assertEqual(
            sorted(target["predicate"] for target in targets.values()),
            sorted(
                ["identity.name"] * 3
                + ["pet.breed"] * 2
                + ["pet.species"] * 3
                + ["relationship.has_pet"] * 2
            ),
        )

    def test_configure_uses_supported_not_absolute_assessment(self) -> None:
        original_targets = base.TARGETS
        original_assessment = base.ASSESSMENT
        try:
            configure()
            self.assertEqual(len(base.TARGETS), 10)
            self.assertEqual(base.ASSESSMENT["action"], "promote_supported")
            self.assertEqual(base.ASSESSMENT["support_score"], "1.000")
            self.assertEqual(base.ASSESSMENT["opposition_score"], "0.000")
            self.assertIn(
                "pet_profile_semantics_reviewed",
                base.ASSESSMENT["reason_codes"],
            )
        finally:
            base.TARGETS = original_targets
            base.ASSESSMENT = original_assessment


if __name__ == "__main__":
    unittest.main()
