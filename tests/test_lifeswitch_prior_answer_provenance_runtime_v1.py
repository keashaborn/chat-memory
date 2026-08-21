
from __future__ import annotations

import unittest

from seebx.capabilities.conversation.prior_lifeswitch_provenance_contract import (
    InactivePriorLifeSwitchProvenanceProviderV1,
)
from seebx.capabilities.conversation.snapshot import create_current_only_conversation_snapshot_v1
from tests.test_lifeswitch_answer_provenance_receipt_v1 import ACTOR
from tests.test_response_orchestration_v0_2 import THREAD


class PriorLifeSwitchRuntimeRetirementV1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_is_permanently_off_and_performs_no_database_read(self) -> None:
        snapshot = create_current_only_conversation_snapshot_v1(
            authenticated_actor_user_id=ACTOR,
            thread_id=THREAD,
            current_request_id="retired-prior-provenance",
            current_message="Where did that old answer come from?",
        )
        result = await InactivePriorLifeSwitchProvenanceProviderV1().prepare(
            authenticated_actor_user_id=ACTOR,
            conversation_snapshot=snapshot,
        )
        self.assertEqual(result.status, "OFF")
        self.assertFalse(result.database_accessed)

    def test_executable_adapter_contains_no_retired_database_surface(self) -> None:
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        source = (root / "seebx/capabilities/conversation/prior_lifeswitch_provenance_contract.py").read_text(encoding="utf-8")
        self.assertNotIn("read_prior_answer_lifeswitch_provenance_v1", source)
        self.assertNotIn("asyncpg", source)
        self.assertNotIn("PostgresPriorLifeSwitch", source)
        self.assertNotIn("class PriorLifeSwitchProvenanceProviderV1", source)


if __name__ == "__main__":
    unittest.main()
