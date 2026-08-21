from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "seebx" / "capabilities" / "training" / "routes.py"


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

    def test_query_is_owner_scoped_and_uses_current_completed_sessions(self) -> None:
        self.assertIn("require_actor_matches_owner(req, owner_user_id)", self.route)
        self.assertIn("_resolve_training_view_target(req, viewer, target_user_id)", self.route)
        self.assertIn("from {schema}.training_session_current_v s", self.route)
        self.assertIn("where s.owner_user_id=$1::uuid", self.route)
        self.assertIn("s.finished_at is not null", self.route)
        self.assertIn("s.is_active=true", self.route)
        self.assertIn("l.is_active=true", self.route)

    def test_query_returns_strength_exercise_exposures_not_daily_totals(self) -> None:
        self.assertIn("l.exercise_id", self.route)
        self.assertIn("count(l.training_set_log_id)::int as set_count", self.route)
        self.assertIn("coalesce(sum(l.reps), 0)::int as total_reps", self.route)
        self.assertIn("coalesce(max(l.weight), 0)::float as max_load", self.route)
        self.assertIn("coalesce(sum(l.volume), 0)::float as total_volume", self.route)
        self.assertIn(
            "join {schema}.training_set_effective_role_v1 role_resolution",
            self.route,
        )
        self.assertIn("role_resolution.effective_role='strength'", self.route)
        self.assertIn("role_resolution_sources", self.route)
        self.assertIn("s.day between $2::date and $3::date", self.route)

    def test_query_preserves_load_unit_comparability(self) -> None:
        self.assertIn("count(distinct nullif(trim(l.load_unit), ''))", self.route)
        self.assertIn("else 'mixed'", self.route)


if __name__ == "__main__":
    unittest.main()
