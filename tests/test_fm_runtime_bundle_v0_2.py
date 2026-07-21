from __future__ import annotations

import ast
from collections import Counter
import json
from pathlib import Path
import unittest

from rag_engine.fm_runtime_bundle_v0_2 import (
    CANONICAL_MANIFEST_SHA256,
    COMPILED_BUNDLE_SHA256,
    EXPECTED_SOURCE_ARTIFACT_SHA256,
    FMRuntimeBundleError,
    RuntimeConceptV02,
    RuntimeHistoricalFormulationV02,
    RuntimeInferenceApplicationV02,
    RuntimeRelationshipV02,
    RuntimeTensionV02,
    canonical_sha256,
    load_runtime_bundle_v0_2,
    parse_runtime_bundle_v0_2,
)


ROOT = Path(__file__).resolve().parents[1]
BUNDLE_PATH = ROOT / "rag_engine" / "data" / "fm_v0_2_runtime_bundle.json"
RUNTIME_MODULE = ROOT / "rag_engine" / "fm_runtime_bundle_v0_2.py"
COMPILER = ROOT / "scripts" / "compile_fm_runtime_bundle_v0_2.py"


class FMRuntimeBundleV02Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = load_runtime_bundle_v0_2(BUNDLE_PATH)
        cls.index = cls.bundle.record_index()

    def test_pinned_manifest_artifacts_and_payload_digest(self) -> None:
        self.assertEqual(
            self.bundle.canonical_manifest_sha256,
            CANONICAL_MANIFEST_SHA256,
        )
        self.assertEqual(self.bundle.bundle_sha256, COMPILED_BUNDLE_SHA256)
        self.assertEqual(
            self.bundle.source_artifact_sha256,
            EXPECTED_SOURCE_ARTIFACT_SHA256,
        )
        payload = self.bundle.model_dump(
            mode="json", exclude={"bundle_sha256"}, exclude_none=True
        )
        self.assertEqual(canonical_sha256(payload), self.bundle.bundle_sha256)
        self.assertEqual(self.bundle.validation_status, "PASS_CANONICAL_AUTHOR_APPROVED")
        self.assertEqual(self.bundle.deterministic_checks_passed, 5859)

    def test_exact_record_counts_and_stable_id_ranges(self) -> None:
        kinds = Counter(record.kind for record in self.bundle.records)
        self.assertEqual(
            kinds,
            {
                "concept": 46,
                "relationship": 48,
                "historical_formulation": 22,
                "inference_rule": 27,
                "inference_application": 27,
                "tension": 28,
            },
        )
        self.assertEqual(
            {record.id for record in self.bundle.records if record.kind == "concept"},
            {f"FM-C-{number:03d}" for number in range(1, 47)},
        )
        self.assertEqual(
            {
                record.id
                for record in self.bundle.records
                if record.kind == "relationship"
            },
            {f"FM-R-{number:03d}" for number in range(1, 49)},
        )
        self.assertEqual(
            {
                record.id
                for record in self.bundle.records
                if record.kind == "historical_formulation"
            },
            {f"FM-HF-{number:03d}" for number in range(1, 23)},
        )
        self.assertEqual(
            {record.id for record in self.bundle.records if record.kind == "tension"},
            {f"FM-T-{number:03d}" for number in range(1, 29)},
        )

    def test_epistemic_labels_are_separate_and_review_is_complete(self) -> None:
        consciousness = self.index["FM-C-009"]
        self.assertIsInstance(consciousness, RuntimeConceptV02)
        self.assertEqual(consciousness.assertion.epistemic_status, "provisional")
        self.assertEqual(consciousness.assertion.framework_status, "speculative_extension")
        self.assertEqual(consciousness.assertion.external_status, "empirical_hypothesis")
        for record in self.bundle.records:
            if hasattr(record, "assertion"):
                self.assertFalse(record.assertion.author_review_required)
            elif hasattr(record, "author_review_required"):
                self.assertFalse(record.author_review_required)

    def test_historical_formulations_are_status_only(self) -> None:
        historical = [
            record
            for record in self.bundle.records
            if isinstance(record, RuntimeHistoricalFormulationV02)
        ]
        self.assertEqual(len(historical), 22)
        for record in historical:
            self.assertEqual(record.inference_use, "status_only")
            self.assertIn(record.status, {"deprecated", "disputed", "excluded", "historical_only"})
            self.assertIn("cannot license current inference", record.render_text)
            self.assertTrue(record.superseded_by)

    def test_noncurrent_application_premises_are_explicitly_negative(self) -> None:
        expected = {
            "FM-IA-006": "FM-HF-014",
            "FM-IA-008": "FM-HF-010",
            "FM-IA-009": "FM-HF-005",
            "FM-IA-015": "FM-HF-017",
            "FM-IA-016": "FM-HF-013",
            "FM-IA-023": "FM-HF-018",
            "FM-IA-027": "FM-HF-012",
        }
        actual: dict[str, str] = {}
        for record in self.bundle.records:
            if not isinstance(record, RuntimeInferenceApplicationV02):
                continue
            historical = [
                premise
                for premise in record.source_premises
                if premise.record_id.startswith("FM-HF-")
            ]
            for premise in historical:
                self.assertEqual(premise.premise_use, "noncurrent_status_only")
                actual[record.id] = premise.record_id
        self.assertEqual(actual, expected)

    def test_relationship_endpoint_can_resolve_to_historical_boundary(self) -> None:
        relationship = self.index["FM-R-043"]
        self.assertIsInstance(relationship, RuntimeRelationshipV02)
        self.assertEqual(relationship.object_id, "FM-HF-018")
        self.assertIn(relationship.object_id, self.index)

    def test_tensions_preserve_native_status_and_boundary(self) -> None:
        tension = self.index["FM-T-026"]
        self.assertIsInstance(tension, RuntimeTensionV02)
        self.assertEqual(tension.status, "practical unit retained; literal claim excluded")
        self.assertEqual(tension.boundary, tension.status)
        self.assertEqual(tension.inference_use, "constraint_only")

    def test_application_gate_is_independent_control(self) -> None:
        gate = self.bundle.application_gate
        self.assertEqual(gate.id, "FM-AG-001")
        self.assertEqual(gate.fm_influence, "off_or_deferred")
        self.assertIn("separately governed response policy", gate.layer_boundary)

    def test_tampered_bundle_fails_closed_even_with_recomputed_payload_hash(self) -> None:
        value = json.loads(BUNDLE_PATH.read_text(encoding="utf-8"))
        value["records"][0]["render_text"] += " tampered"
        payload = {key: item for key, item in value.items() if key != "bundle_sha256"}
        value["bundle_sha256"] = canonical_sha256(payload)
        with self.assertRaisesRegex(FMRuntimeBundleError, "reviewed v0.2 digest"):
            parse_runtime_bundle_v0_2(json.dumps(value))

    def test_duplicate_json_keys_fail_closed(self) -> None:
        with self.assertRaisesRegex(FMRuntimeBundleError, "duplicate JSON key"):
            parse_runtime_bundle_v0_2('{"bundle_contract_version":"x","bundle_contract_version":"y"}')

    def test_runtime_has_no_yaml_rag_database_or_provider_dependency(self) -> None:
        source = RUNTIME_MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
        self.assertTrue(
            imports.isdisjoint(
                {"asyncpg", "openai", "qdrant_client", "requests", "yaml"}
            ),
            imports,
        )
        self.assertIn("import yaml", COMPILER.read_text(encoding="utf-8"))
        self.assertNotIn("import yaml", source)


if __name__ == "__main__":
    unittest.main()
