
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]
MIGRATION = ROOT / "governed-migrations/lifeswitch_answer_provenance_receipt_v1"


class LifeSwitchAnswerProvenanceReceiptSqlV1Tests(unittest.TestCase):
    def test_migration_preserves_only_the_owner_scoped_receipt(self) -> None:
        sql = (MIGRATION / "forward.pgsql").read_text(encoding="utf-8").lower()
        self.assertIn("final_answer_lifeswitch_provenance_receipt_v1", sql)
        self.assertIn("force row level security", sql)
        self.assertIn("authenticated_actor_user_id=owner_user_id", sql)
        self.assertIn("grant select,insert", sql)
        self.assertNotIn("read_prior_answer_lifeswitch_provenance_v1", sql)
        self.assertNotIn("memory.assistant_transcript_attestation_v1", sql)

    def test_package_hashes_are_exact_and_view_is_retired(self) -> None:
        package = json.loads((MIGRATION / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(package["migration_id"], "lifeswitch_answer_provenance_receipt_v1")
        self.assertTrue(package["retired_surface"]["read_prior_answer_view"])
        for key in ("forward", "recovery"):
            record = package[key]
            self.assertEqual(hashlib.sha256((MIGRATION / record["path"]).read_bytes()).hexdigest(), record["sha256"])


if __name__ == "__main__":
    unittest.main()
