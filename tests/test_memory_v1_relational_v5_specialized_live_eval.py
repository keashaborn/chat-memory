from __future__ import annotations

import hashlib
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.memory_v1_relational_v5_specialized_live_eval import (
    _load_selection,
    _pass_schema_hashes,
    _repository_commit,
    run,
)


class SpecializedV5LiveRunnerTest(unittest.TestCase):
    def selection_fixture(self) -> tuple[list[dict], dict]:
        cases = [{"case_id": f"v5-{ordinal:02d}"} for ordinal in range(1, 26)]
        selected = [
            "v5-01",
            "v5-02",
            "v5-03",
            "v5-04",
            "v5-05",
            "v5-08",
            "v5-10",
            "v5-13",
            "v5-14",
            "v5-15",
            "v5-18",
            "v5-19",
            "v5-20",
            "v5-22",
            "v5-23",
            "v5-25",
        ]
        payload = {
            "authorization_scope": "failed_cases_only_store_false_zero_write",
            "baseline_evaluator_commit": "c" * 40,
            "baseline_report_sha256": "d" * 64,
            "case_contract_sha256": "b" * 64,
            "case_ids": selected,
            "selection_version": "memory_v1_relational_specialized_rerun_v1",
            "source_manifest_sha256": "a" * 64,
        }
        return cases, payload

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

    def test_subset_selection_is_hash_bound_and_manifest_ordered(self) -> None:
        cases, payload = self.selection_fixture()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "selection.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            selected, digest, case_ids = _load_selection(
                path,
                cases=cases,
                source_manifest_sha256="a" * 64,
                case_contract_sha256="b" * 64,
            )
        self.assertEqual(selected, payload)
        self.assertEqual(len(digest or ""), 64)
        self.assertEqual(case_ids, payload["case_ids"])

    def test_subset_selection_rejects_case_contract_drift(self) -> None:
        cases, payload = self.selection_fixture()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "selection.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "case contract hash mismatch"):
                _load_selection(
                    path,
                    cases=cases,
                    source_manifest_sha256="a" * 64,
                    case_contract_sha256="e" * 64,
                )

    def test_subset_selection_rejects_reordered_cases(self) -> None:
        cases, payload = self.selection_fixture()
        payload["case_ids"] = list(reversed(payload["case_ids"]))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "selection.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "retain manifest order"):
                _load_selection(
                    path,
                    cases=cases,
                    source_manifest_sha256="a" * 64,
                    case_contract_sha256="b" * 64,
                )

    def test_checked_in_failed16_selection_matches_revised_contract(self) -> None:
        cases_path = Path("evals/memory_v1_relational_extraction_v5_cases.jsonl")
        cases = [
            json.loads(line)
            for line in cases_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        selection, digest, case_ids = _load_selection(
            Path("evals/memory_v1_relational_v5_failed16_rerun_20260715.json"),
            cases=cases,
            source_manifest_sha256=(
                "8d31688923f3a0bb82c019b98dc6a78a867129a44157e80efc65432b60d2b649"
            ),
            case_contract_sha256=hashlib.sha256(cases_path.read_bytes()).hexdigest(),
        )
        self.assertIsNotNone(selection)
        self.assertEqual(len(digest or ""), 64)
        self.assertEqual(len(case_ids), 16)

    def test_v5_08_contract_is_temporal_project_current_state(self) -> None:
        cases = [
            json.loads(line)
            for line in Path("evals/memory_v1_relational_extraction_v5_cases.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        expected = next(item["expected"] for item in cases if item["case_id"] == "v5-08")
        self.assertEqual(expected["outcome"], "conditional_project")
        self.assertEqual(expected["required_predicate_families"], ["project.current_state"])
        self.assertIn("open_state_validity", expected["required_temporal_features"])
        self.assertEqual(expected["required_deferrals"], ["project_scope_unresolved"])

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
