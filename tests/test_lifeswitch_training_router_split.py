from __future__ import annotations

import ast
from pathlib import Path
import unittest

from seebx.capabilities.training.routes import router


ROOT = Path(__file__).resolve().parents[1]
TRAINING = ROOT / "seebx/capabilities/training"

EXPECTED_ROUTES = (
    ("GET", "/my_exercises", "list_my_exercises"),
    ("POST", "/my_exercises/upsert", "upsert_my_exercise"),
    ("POST", "/my_exercises/{my_exercise_id}/deactivate", "deactivate_my_exercise"),
    ("GET", "/conditioning_library", "list_conditioning_library"),
    ("GET", "/my_conditioning_prescriptions", "list_my_conditioning_prescriptions"),
    ("POST", "/my_conditioning_prescriptions/upsert", "upsert_my_conditioning_prescription"),
    ("POST", "/my_conditioning_prescriptions/{my_conditioning_prescription_id}/deactivate", "deactivate_my_conditioning_prescription"),
    ("POST", "/conditioning_sessions/create", "create_conditioning_session"),
    ("GET", "/conditioning_sessions", "list_conditioning_sessions"),
    ("GET", "/conditioning_sessions/{conditioning_session_log_id}", "get_conditioning_session"),
    ("POST", "/conditioning_sessions/{conditioning_session_log_id}/deactivate", "deactivate_conditioning_session"),
    ("POST", "/conditioning_sessions/{conditioning_session_log_id}/correct", "correct_conditioning_session"),
    ("POST", "/workout_template_shares/create", "create_workout_template_share"),
    ("GET", "/workout_template_shares", "list_workout_template_shares"),
    ("GET", "/workout_template_shares/preview", "preview_workout_template_share"),
    ("POST", "/workout_template_shares/import", "import_workout_template_share"),
    ("POST", "/workout_template_shares/{workout_template_share_id}/revoke", "revoke_workout_template_share"),
    ("GET", "/workout_templates", "list_workout_templates"),
    ("POST", "/workout_templates/upsert", "upsert_workout_template"),
    ("POST", "/workout_templates/{workout_template_id}/classify_historical_sessions", "classify_historical_workout_sessions"),
    ("POST", "/workout_templates/{workout_template_id}/deactivate", "deactivate_workout_template"),
    ("GET", "/workout_templates/{workout_template_id}/exercises", "list_workout_template_exercises"),
    ("POST", "/workout_templates/{workout_template_id}/exercises/upsert", "upsert_workout_template_exercise"),
    ("POST", "/workout_templates/{workout_template_id}/exercises/{workout_template_exercise_id}/delete", "delete_workout_template_exercise"),
    ("GET", "/workout_template_exercises/{workout_template_exercise_id}/segments", "list_workout_template_exercise_segments"),
    ("POST", "/workout_template_exercises/{workout_template_exercise_id}/segments/upsert", "upsert_workout_template_exercise_segment"),
    ("POST", "/workout_template_exercises/{workout_template_exercise_id}/segments/{workout_template_exercise_segment_id}/delete", "delete_workout_template_exercise_segment"),
    ("POST", "/sessions/complete", "complete_training_session"),
    ("POST", "/sessions/create", "create_training_session"),
    ("GET", "/sessions", "list_training_sessions"),
    ("GET", "/sessions/{training_session_id}", "get_training_session"),
    ("GET", "/progression", "list_strength_progression"),
    ("POST", "/sessions/{training_session_id}/deactivate", "deactivate_training_session"),
    ("POST", "/sessions/{training_session_id}/correct", "correct_training_session"),
    ("GET", "/sessions/{training_session_id}/sets", "list_training_session_sets"),
    ("POST", "/sessions/{training_session_id}/sets/add", "add_training_set_log"),
    ("POST", "/sessions/{training_session_id}/sets/{training_set_log_id}/update", "update_training_set_log"),
    ("GET", "/sessions/{training_session_id}/sets/{training_set_log_id}/segments", "list_training_set_log_segments"),
    ("POST", "/sessions/{training_session_id}/sets/{training_set_log_id}/segments/add", "add_training_set_log_segment"),
    ("POST", "/sessions/{training_session_id}/sets/{training_set_log_id}/segments/{training_set_log_segment_id}/delete", "delete_training_set_log_segment"),
    ("POST", "/sessions/{training_session_id}/sets/{training_set_log_id}/delete", "delete_training_set_log"),
)


class TrainingRouterSplitTests(unittest.TestCase):
    def test_composition_preserves_exact_route_order(self) -> None:
        actual = tuple(
            (next(iter(route.methods)), route.path, route.endpoint.__name__)
            for route in router.routes
        )
        self.assertEqual(actual, EXPECTED_ROUTES)

    def test_composition_owns_no_implementation(self) -> None:
        tree = ast.parse((TRAINING / "routes.py").read_text(encoding="utf-8"))
        implementations = [
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        self.assertEqual(implementations, [])

    def test_aggregate_modules_own_no_raw_database_effects(self) -> None:
        forbidden_calls = {"fetch", "fetchrow", "fetchval", "execute", "close"}
        for name in ("exercises.py", "conditioning.py", "sharing.py", "templates.py", "sessions.py"):
            source = (TRAINING / name).read_text(encoding="utf-8")
            tree = ast.parse(source)
            calls = [
                node.func.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in forbidden_calls
            ]
            with self.subTest(module=name):
                self.assertEqual(calls, [])
                self.assertNotIn("asyncpg", source)
                self.assertNotIn("connect_lifeswitch", source)
                self.assertNotIn("@router", source)


if __name__ == "__main__":
    unittest.main()
