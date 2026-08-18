from __future__ import annotations

import pathlib
import unittest


class ResseResponsePriorLifeSwitchProvenanceV1Tests(unittest.TestCase):
    def test_router_uses_single_v4_text_and_voice_response_path(self) -> None:
        source = (
            pathlib.Path(__file__).parents[1]
            / "seebx"
            / "capabilities/conversation/router.py"
        ).read_text(encoding="utf-8")
        self.assertIn("IntegratedLifeSwitchResponseCompositionRootV0_4", source)
        self.assertIn("InactivePriorLifeSwitchProvenanceProviderV1", source)
        self.assertNotIn("LazyPostgresPriorLifeSwitchRestrictedReadSessionV1", source)
        self.assertIn("persist_finalized_response_v3", source)
        self.assertIn("build_response_inspection_v4", source)
        self.assertNotIn("client_prior_lifeswitch", source)


if __name__ == "__main__":
    unittest.main()
