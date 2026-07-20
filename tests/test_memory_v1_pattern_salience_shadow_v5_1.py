from __future__ import annotations

import copy
from datetime import date
import unittest
import uuid

from scripts.memory_v1_pattern_salience_shadow_v5_1 import (
    ShadowGenerationError,
    build_pattern_evaluation,
    generate_report,
    sha256_text,
)


OWNER = uuid.UUID("11111111-1111-4111-8111-111111111111")
SELF = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OBJECT = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def observation(
    number: int,
    *,
    predicate: str = "event.reported",
    object_kind: str = "entity",
    object_entity_id: str | None = OBJECT,
    subject_entity_id: str | None = SELF,
    subject_entity_type: str | None = "self",
    episode: int | None = None,
    bucket: int | None = None,
    effective_date: str = "2026-07-01",
    polarity: str = "affirmed",
) -> dict:
    episode = number if episode is None else episode
    bucket = number if bucket is None else bucket
    return {
        "observation_id": f"00000000-0000-4000-8000-{number:012d}",
        "evidence_id": f"10000000-0000-4000-8000-{number:012d}",
        "predicate": predicate,
        "predicate_registry_version": "memory_predicate_registry_v5_1",
        "subject_entity_id": subject_entity_id,
        "subject_entity_type": subject_entity_type,
        "object_kind": object_kind,
        "object_entity_id": object_entity_id,
        "object_literal_sha256": "9" * 64 if object_kind == "literal" else None,
        "polarity": polarity,
        "modality": "asserted",
        "projection_class": "direct_claim",
        "surface_policy": "mention_when_directly_relevant",
        "sensitivity": "medium",
        "extraction_confidence": 1,
        "observation_sha256": f"{number:x}".rjust(64, "0"),
        "evidence_content_sha256": f"{number + 100:x}".rjust(64, "0"),
        "evidence_directness": 1,
        "evidence_source_reliability": 1,
        "evidence_source_system": "public.chat_log",
        "independence_key_sha256": f"{number + 200:x}".rjust(64, "0"),
        "episode_key_sha256": f"{episode + 300:x}".rjust(64, "0"),
        "temporal_bucket_sha256": f"{bucket + 400:x}".rjust(64, "0"),
        "effective_date": effective_date,
        "temporal_basis": "instant",
        "temporal_source_form": "implicit_source_time",
    }


def target(observation_ids: list[str], *, status: str = "supported") -> dict:
    return {
        "target_kind": "claim",
        "target_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        "target_revision_number": 2,
        "semantic_key_sha256": "d" * 64,
        "status": status,
        "sensitivity": "medium",
        "target_class": "relationship.friend_of",
        "object_kind": "entity",
        "valid_from": None,
        "valid_to": None,
        "created_at": "2026-07-01T00:00:00.000000Z",
        "authority_state": None,
        "observation_links": [
            {"observation_id": value, "stance": "supports", "relevance": 1}
            for value in observation_ids
        ],
    }


def snapshot(observations: list[dict], targets: list[dict]) -> dict:
    return {
        "contract_version": "memory_v1_pattern_salience_shadow_input_v5_1",
        "owner_user_id_sha256": sha256_text(str(OWNER)),
        "observation_total": len(observations),
        "observation_returned": len(observations),
        "observations_truncated": False,
        "target_total": len(targets),
        "target_returned": len(targets),
        "targets_truncated": False,
        "observations": observations,
        "targets": targets,
    }


class PatternSalienceShadowV51Tests(unittest.TestCase):
    def test_three_independent_entity_events_form_review_proposal(self) -> None:
        rows = [
            observation(1, effective_date="2026-07-01"),
            observation(2, effective_date="2026-07-10"),
            observation(3, effective_date="2026-07-20"),
        ]
        evaluations, proposals, routes = build_pattern_evaluation(rows)
        self.assertEqual(routes, {})
        self.assertEqual(len(evaluations), 1)
        self.assertEqual(evaluations[0]["disposition"], "pattern_review")
        self.assertEqual(len(proposals), 1)
        self.assertTrue(proposals[0]["identity_inference_forbidden"])
        self.assertTrue(proposals[0]["causal_inference_forbidden"])

    def test_same_episode_does_not_inflate_pattern(self) -> None:
        rows = [observation(i, episode=1, bucket=1) for i in range(1, 4)]
        evaluations, proposals, _ = build_pattern_evaluation(rows)
        self.assertEqual(evaluations[0]["disposition"], "no_pattern")
        self.assertIn(
            "same_episode_repetition_collapsed", evaluations[0]["reason_codes"]
        )
        self.assertEqual(proposals, [])

    def test_literal_object_identity_fails_closed(self) -> None:
        rows = [
            observation(
                i,
                predicate="health.user_reported_observation",
                object_kind="literal",
                object_entity_id=None,
            )
            for i in range(1, 4)
        ]
        evaluations, proposals, routes = build_pattern_evaluation(rows)
        self.assertEqual(evaluations, [])
        self.assertEqual(proposals, [])
        self.assertEqual(routes, {"literal_object_identity_not_bound": 3})

    def test_static_relationship_routes_to_claim_reassessment(self) -> None:
        rows = [observation(i, predicate="relationship.friend_of") for i in range(1, 4)]
        _, proposals, routes = build_pattern_evaluation(rows)
        self.assertEqual(proposals, [])
        self.assertEqual(
            routes, {"static_assertion_routes_to_claim_reassessment": 3}
        )

    def test_snapshot_is_multidimensional_and_independence_aware(self) -> None:
        rows = [observation(1, episode=1), observation(2, episode=1)]
        rows[1]["independence_key_sha256"] = rows[0]["independence_key_sha256"]
        report = generate_report(
            snapshot(rows, [target([row["observation_id"] for row in rows])]),
            OWNER,
            date(2026, 7, 20),
        )
        packet = report["target_snapshot_candidates"][0]["packet"]
        assessment = packet["evidence_assessment"]
        salience = packet["salience_features"]
        self.assertEqual(assessment["independent_support_cluster_count"], 1)
        self.assertEqual(assessment["dimensions"]["independence"], 0.5)
        self.assertEqual(salience["frequency"], 0)
        self.assertNotIn("truth", packet)
        self.assertNotIn("salience", salience)
        self.assertNotIn("overall_score", salience)

    def test_unknown_reliability_uses_neutral_prior_not_zero(self) -> None:
        row = observation(1)
        row["evidence_source_reliability"] = None
        report = generate_report(
            snapshot([row], [target([row["observation_id"]])]),
            OWNER,
            date(2026, 7, 20),
        )
        assessment = report["target_snapshot_candidates"][0]["packet"][
            "evidence_assessment"
        ]
        self.assertEqual(assessment["dimensions"]["source_reliability"], 0.5)
        self.assertEqual(assessment["dimensions"]["support_strength"], 0.5)

    def test_retracted_target_retains_evidence_and_pressure(self) -> None:
        row = observation(1)
        report = generate_report(
            snapshot([row], [target([row["observation_id"]], status="retracted")]),
            OWNER,
            date(2026, 7, 20),
        )
        packet = report["target_snapshot_candidates"][0]["packet"]
        self.assertEqual(packet["evidence_assessment"]["assessment_state"], "retracted")
        self.assertEqual(packet["evidence_assessment"]["supporting_observation_count"], 1)
        self.assertEqual(packet["salience_features"]["importance"], 0)
        self.assertEqual(packet["salience_features"]["contradiction_pressure"], 1)

    def test_target_without_governed_links_is_not_persistable(self) -> None:
        report = generate_report(snapshot([], [target([])]), OWNER, date(2026, 7, 20))
        self.assertEqual(report["target_snapshot_candidates"], [])
        self.assertEqual(
            report["target_evaluations"][0]["reason_codes"],
            ["missing_governed_observation_links"],
        )

    def test_report_is_deterministic(self) -> None:
        row = observation(1)
        value = snapshot([row], [target([row["observation_id"]])])
        first = generate_report(copy.deepcopy(value), OWNER, date(2026, 7, 20))
        second = generate_report(copy.deepcopy(value), OWNER, date(2026, 7, 20))
        self.assertEqual(first, second)

    def test_owner_mismatch_and_truncation_are_rejected(self) -> None:
        value = snapshot([], [])
        value["owner_user_id_sha256"] = "0" * 64
        with self.assertRaisesRegex(ShadowGenerationError, "owner binding"):
            generate_report(value, OWNER, date(2026, 7, 20))
        value = snapshot([], [])
        value["observations_truncated"] = True
        with self.assertRaisesRegex(ShadowGenerationError, "truncated"):
            generate_report(value, OWNER, date(2026, 7, 20))


if __name__ == "__main__":
    unittest.main()
