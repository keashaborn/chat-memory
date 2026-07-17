from __future__ import annotations

import copy
import unittest
import uuid

from scripts.memory_v1_relational_v5_specialized_materialize import (
    MODE,
    build_materialized_report,
)
from scripts.memory_v1_v5_source_evidence import load_report_sources
from scripts.memory_v1_v5_stage_preflight import _load_case


OWNER = "1240822d-ac9a-4096-95aa-e2b24d36ef50"
REPLAY_SHA = "a" * 64
COMMIT = "b" * 40
SOURCE_ID = "617a5854-79a1-468b-86d4-b01c7871a2b3"
SOURCE_SHA = "c" * 64


def extraction_packet() -> dict:
    return {
        "contract_version": "memory_v1_relational_extraction_v5",
        "source_envelope": {
            "job_id": "11111111-1111-4111-8111-111111111111",
            "source_system": "public.chat_log",
            "source_external_id": SOURCE_ID,
            "source_sha256": SOURCE_SHA,
            "source_recorded_at": "2026-07-14T17:06:00+00:00",
        },
        "predicate_registry_version": "memory_predicate_registry_v5",
        "entity_mentions": [],
        "observations": [],
        "comparison_hints": [],
        "deferrals": [],
        "packet_findings": [],
    }


def replay_report() -> dict:
    packet = extraction_packet()
    return {
        "mode": "zero_write_relational_v5_specialized_saved_replay",
        "pipeline_version": "memory_v1_relational_specialized_v5_1",
        "evaluator_commit": COMMIT,
        "manifest_sha256": "d" * 64,
        "case_contract_sha256": "e" * 64,
        "owner_user_id": OWNER,
        "external_model_calls": 0,
        "store": False,
        "replay": {
            "case_id": "v5-13",
            "ordinal": 13,
            "source_external_id": SOURCE_ID,
            "source_sha256": SOURCE_SHA,
            "model_packet": {},
            "packet": packet,
            "deterministic_rejections": [],
            "integrity_reasons": [],
            "evaluation": {"passed": True, "findings": [], "actual": {}},
        },
        "zero_write_proof": {
            "passed": True,
            "database_unchanged": True,
            "qdrant_unchanged": True,
        },
    }


class SpecializedMaterializeTest(unittest.TestCase):
    def test_materialized_report_is_accepted_by_evidence_and_stage_loaders(self) -> None:
        report = build_materialized_report(
            replay_report(),
            replay_report_sha256=REPLAY_SHA,
            materializer_commit=COMMIT,
        )
        self.assertEqual(report["mode"], MODE)
        evidence = load_report_sources(
            report,
            owner=uuid.UUID(OWNER),
            case_ids=["v5-13"],
        )
        self.assertEqual(evidence[0]["source_external_id"], SOURCE_ID)
        selected = _load_case(report, "v5-13", uuid.UUID(OWNER))
        self.assertEqual(selected["packet"]["source_envelope"]["source_sha256"], SOURCE_SHA)

    def test_materialization_rejects_unresolved_replay(self) -> None:
        report = replay_report()
        report["replay"]["evaluation"]["passed"] = False
        with self.assertRaisesRegex(RuntimeError, "did not pass"):
            build_materialized_report(
                report,
                replay_report_sha256=REPLAY_SHA,
                materializer_commit=COMMIT,
            )

    def test_stage_loader_rejects_broken_materialization_provenance(self) -> None:
        report = build_materialized_report(
            replay_report(),
            replay_report_sha256=REPLAY_SHA,
            materializer_commit=COMMIT,
        )
        broken = copy.deepcopy(report)
        broken["sources"][0]["materialization_provenance"][
            "source_replay_report_sha256"
        ] = "f" * 64
        with self.assertRaisesRegex(RuntimeError, "provenance"):
            _load_case(broken, "v5-13", uuid.UUID(OWNER))
        with self.assertRaisesRegex(RuntimeError, "provenance"):
            load_report_sources(
                broken,
                owner=uuid.UUID(OWNER),
                case_ids=["v5-13"],
            )


if __name__ == "__main__":
    unittest.main()
