from __future__ import annotations

import os
import unittest
import uuid
from unittest.mock import patch

from rag_engine.memory_v1_preference_project_shadow import (
    _activation_allowlisted as specialized_activation,
)
from rag_engine.memory_v1_shadow import (
    governed_activation_allowlisted,
    governed_maximum_sensitivity,
)


ACTOR = uuid.UUID("11111111-1111-4111-8111-111111111111")


class UniversalActivationTest(unittest.TestCase):
    def test_governed_activation_accepts_any_authenticated_actor(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MEMORY_V1_GOVERNED_ACTIVE": "1",
                "MEMORY_V1_GOVERNED_ACTIVE_ALL_AUTHENTICATED": "1",
                "MEMORY_V1_GOVERNED_ACTIVE_USER_IDS": "",
            },
            clear=False,
        ):
            self.assertTrue(governed_activation_allowlisted(ACTOR))

    def test_governed_master_switch_still_fails_closed(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MEMORY_V1_GOVERNED_ACTIVE": "0",
                "MEMORY_V1_GOVERNED_ACTIVE_ALL_AUTHENTICATED": "1",
            },
            clear=False,
        ):
            self.assertFalse(governed_activation_allowlisted(ACTOR))

    def test_explicit_recall_high_sensitivity_is_universal_but_not_implicit(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MEMORY_V1_SHADOW_MAX_SENSITIVITY": "medium",
                "MEMORY_V1_GOVERNED_EXPLICIT_HIGH_ALL_AUTHENTICATED": "1",
                "MEMORY_V1_GOVERNED_EXPLICIT_RECALL_MAX_SENSITIVITY": "high",
            },
            clear=False,
        ):
            self.assertEqual(
                governed_maximum_sensitivity(ACTOR, {"explicit_recall": True}),
                "high",
            )
            self.assertEqual(
                governed_maximum_sensitivity(ACTOR, {"explicit_recall": False}),
                "medium",
            )

    def test_specialized_activation_requires_both_master_switches(self) -> None:
        base = {
            "MEMORY_V1_SHADOW_ALL_AUTHENTICATED": "1",
            "MEMORY_V1_SPECIALIZED_ACTIVE_ALL_AUTHENTICATED": "1",
        }
        with patch.dict(
            os.environ,
            {**base, "MEMORY_V1_SPECIALIZED_ACTIVE": "1"},
            clear=False,
        ):
            self.assertTrue(specialized_activation(ACTOR))
        with patch.dict(
            os.environ,
            {**base, "MEMORY_V1_SPECIALIZED_ACTIVE": "0"},
            clear=False,
        ):
            self.assertFalse(specialized_activation(ACTOR))


if __name__ == "__main__":
    unittest.main()
