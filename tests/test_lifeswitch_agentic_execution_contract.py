from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ExecutionContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.idempotency = (ROOT / "lifeswitch_agentic" / "command_idempotency.py").read_text(
            encoding="utf-8"
        ).lower()
        cls.outbox = (ROOT / "lifeswitch_agentic" / "outbox_repository.py").read_text(
            encoding="utf-8"
        ).lower()
        cls.plan = (ROOT / "lifeswitch_agentic" / "plan_repository.py").read_text(
            encoding="utf-8"
        ).lower()

    def test_write_receipt_is_transaction_bound_and_conflict_safe(self) -> None:
        self.assertIn("conn.is_in_transaction()", self.idempotency)
        self.assertIn("on conflict (owner_user_id, command_name, idempotency_key)", self.idempotency)
        self.assertIn("idempotency_conflict", self.idempotency)

    def test_outbox_claim_is_nonblocking_and_lease_bound(self) -> None:
        self.assertIn("for update skip locked", self.outbox)
        self.assertIn("claim_expires_at", self.outbox)
        self.assertIn("claim_token", self.outbox)

    def test_plan_transitions_write_outbox_in_domain_transactions(self) -> None:
        for event_type in (
            "plan_revision.proposed",
            "plan_revision.conflicted",
            "plan_version.activated",
        ):
            with self.subTest(event_type=event_type):
                self.assertIn(event_type, self.plan)


if __name__ == "__main__":
    unittest.main()
