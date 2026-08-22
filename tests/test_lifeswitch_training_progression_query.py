from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "seebx" / "capabilities" / "training" / "routes.py"
SESSIONS = ROOT / "seebx" / "adapters" / "lifeswitch_training_sessions_postgres.py"


class TrainingProgressionQueryContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        source = ROUTER.read_text(encoding="utf-8")
        match = re.search(
            r"async def list_strength_progression\(.*?(?=\n@router\.)",
            source,
            flags=re.DOTALL,
        )
        if match is None:
            raise AssertionError("list_strength_progression route not found")
        cls.route = match.group(0).lower()
        cls.query = SESSIONS.read_text(encoding="utf-8").lower()

    def test_query_is_owner_scoped_and_uses_current_completed_sessions(self) -> None:
        self.assertIn("require_actor_matches_owner(req, owner_user_id)", self.route)
        self.assertIn("_resolve_training_view_target(", self.route)
        self.assertIn("req, viewer, target_user_id", self.route)
        self.assertIn("from {self._schema}.training_session_current_v s", self.query)
        self.assertIn("where s.owner_user_id=$1::uuid", self.query)
        self.assertIn("s.finished_at is not null", self.query)
        self.assertIn("s.is_active=true", self.query)
        self.assertIn("l.is_active=true", self.query)

    def test_query_returns_strength_exercise_exposures_not_daily_totals(self) -> None:
        self.assertIn("l.exercise_id", self.query)
        self.assertIn("count(l.training_set_log_id)::int as set_count", self.query)
        self.assertIn("coalesce(sum(l.reps), 0)::int as total_reps", self.query)
        self.assertIn("coalesce(max(l.weight), 0)::float as max_load", self.query)
        self.assertIn("coalesce(sum(l.volume), 0)::float as total_volume", self.query)
        self.assertIn(
            "join {self._schema}.training_set_effective_role_v1 role_resolution",
            self.query,
        )
        self.assertIn("role_resolution.effective_role='strength'", self.query)
        self.assertIn("role_resolution_sources", self.query)
        self.assertIn("s.day between $2::date and $3::date", self.query)

    def test_query_preserves_load_unit_comparability(self) -> None:
        self.assertIn("count(distinct nullif(trim(l.load_unit), ''))", self.query)
        self.assertIn("else 'mixed'", self.query)


if __name__ == "__main__":
    unittest.main()
