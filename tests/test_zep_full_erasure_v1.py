from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ZepFullErasureContractTests(unittest.TestCase):
    def test_route_uses_owner_barrier_chat_clear_then_zep_delete(self) -> None:
        source = (ROOT / "app.py").read_text()
        route = source.index('@app.delete("/memory/chat-and-zep/clear")')
        block = source[route : route + 3_500]
        actor = block.index("owner_user_id = parse_uuid(actor or \"\")")
        barrier = block.index("owner_erasure_barrier(owner_user_id)")
        clear = block.index("clear_chat_history_v1(")
        delete = block.index("delete_owner_memory(owner_user_id)")
        self.assertLess(actor, barrier)
        self.assertLess(barrier, clear)
        self.assertLess(clear, delete)
        self.assertIn('scope="all"', block)
        self.assertIn('"memory_retained": False', block)
        self.assertIn('"zep_called": True', block)
        self.assertIn('"zep_deleted": True', block)

    def test_route_cannot_reach_protected_lifeswitch_postgres(self) -> None:
        source = (ROOT / "app.py").read_text()
        route = source.index('@app.delete("/memory/chat-and-zep/clear")')
        block = source[route : route + 3_500]
        self.assertNotIn("LIFESWITCH_POSTGRES_DSN", block)
        self.assertNotIn("lifeswitch_nutrition", block)
        self.assertNotIn("lifeswitch_training", block)
        self.assertNotIn("lifeswitch_measurement_entries", block)
        self.assertNotIn("lifeswitch_snapshot", block)


if __name__ == "__main__":
    unittest.main()
