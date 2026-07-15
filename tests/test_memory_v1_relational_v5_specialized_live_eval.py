from __future__ import annotations

import inspect
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.memory_v1_relational_v5_specialized_live_eval import (
    _pass_schema_hashes,
    _repository_commit,
    run,
)


class SpecializedV5LiveRunnerTest(unittest.TestCase):
    def test_pass_schema_hashes_are_complete_and_stable_shape(self) -> None:
        hashes = _pass_schema_hashes()
        self.assertEqual(
            set(hashes),
            {"entity_graph", "temporal_content", "project_knowledge"},
        )
        for value in hashes.values():
            self.assertEqual(len(value), 64)
            self.assertTrue(all(character in "0123456789abcdef" for character in value))

    def test_live_runner_has_state_proof_but_no_persistence_statement(self) -> None:
        path = Path(inspect.getfile(run))
        source = path.read_text(encoding="utf-8").lower()
        self.assertIn("state_snapshot", source)
        self.assertIn("zero_write_proof", source)
        self.assertIn("expected_manifest_sha256", source)
        self.assertIn("store", source)
        for forbidden in (
            "insert into",
            "update ",
            "delete from",
            ".upsert(",
            ".add(",
        ):
            self.assertNotIn(forbidden, source)

    @patch("scripts.memory_v1_relational_v5_specialized_live_eval.subprocess.run")
    def test_repository_commit_is_full_sha_on_clean_committed_tree(self, run_mock) -> None:
        run_mock.side_effect = [
            SimpleNamespace(stdout=""),
            SimpleNamespace(stdout="a" * 40 + "\n"),
        ]
        commit = _repository_commit()
        self.assertEqual(len(commit), 40)
        self.assertTrue(all(character in "0123456789abcdef" for character in commit))


if __name__ == "__main__":
    unittest.main()
