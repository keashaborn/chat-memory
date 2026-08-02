from __future__ import annotations

import unittest

from rag_engine.lifeswitch_answer_provenance_store_v1 import (
    persist_lifeswitch_provenance_receipt_on_connection_v1,
)
from rag_engine.lifeswitch_answer_binding_v1 import FinalAnswerLifeSwitchBindingV1
from rag_engine.lifeswitch_answer_provenance_receipt_v1 import FinalAnswerLifeSwitchProvenanceReceiptV1
from tests.test_lifeswitch_answer_provenance_receipt_v1 import ACTOR, ANSWER, NOW, new_plan


class FakeConn:
    def __init__(self) -> None:
        self.calls = []
        self.manifest = None

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        if "insert into" in sql.lower():
            self.manifest = args[-1]

    async def fetchval(self, sql, *args):
        return self.manifest


class LifeSwitchAnswerProvenanceStoreV1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_store_sets_owner_role_and_verifies_insert(self) -> None:
        plan = await new_plan("Did you access my LifeSwitch nutrition day for Monday?")
        binding = FinalAnswerLifeSwitchBindingV1.create(
            assembly=plan.assembled_prompt,
            authenticated_actor_user_id=ACTOR,
            answer_id=ANSWER,
            created_at=NOW,
        )
        assert binding is not None
        receipt = FinalAnswerLifeSwitchProvenanceReceiptV1.create(
            prepared_context=plan.lifeswitch_context,
            binding=binding,
            assistant_text_sha256="d" * 64,
            attestation_sha256="e" * 64,
        )
        conn = FakeConn()
        await persist_lifeswitch_provenance_receipt_on_connection_v1(conn, receipt)
        sql = "\n".join(item[0] for item in conn.calls).lower()
        self.assertIn("set local role lifeswitch_chat_binding_writer_v1", sql)
        self.assertIn("final_answer_lifeswitch_provenance_receipt_v1", sql)
        self.assertIn("reset role", sql)


if __name__ == "__main__":
    unittest.main()
