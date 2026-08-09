from __future__ import annotations

import hashlib
from pathlib import Path
import unittest
import uuid

from scripts.memory_v1_openai_review_packet_v1 import (
    EXPECTED_PROVIDER_ID,
    EXPECTED_PROVIDER_VERSION,
    OpenAIPacketReviewError,
    openai_model_call_provenance_valid,
    validate_exact_route_preflight,
    validate_openai_row,
)
from scripts.memory_v1_relational_extraction_v5_provider import canonical_sha256


SHA = hashlib.sha256(b"fixture").hexdigest()
JOB_ID = uuid.UUID("00000000-0000-4000-8000-000000000010")
PACKET_ID = uuid.UUID("00000000-0000-4000-8000-000000000011")


def packet() -> dict:
    return {
        "source_envelope": {"job_id": str(JOB_ID)},
        "entity_mentions": [{"mention_ref": "m01"}],
        "observations": [{"observation_ref": "o01"}],
        "comparison_hints": [],
        "deferrals": [],
    }


def row(value: dict) -> dict:
    return {
        "evidence_content_sha256": SHA,
        "provider_model_sha256": SHA,
        "provider_output_sha256": SHA,
        "validator_packet_sha256": canonical_sha256(value),
        "packet_storage_sha256": canonical_sha256(value),
        "evidence_authority_sha256": SHA,
        "provider_id": EXPECTED_PROVIDER_ID,
        "provider_version": EXPECTED_PROVIDER_VERSION,
        "local_model_calls": 0,
        "external_model_calls": 1,
        "storage_integrity_verified": True,
        "manual_review_required": True,
        "entity_mention_count": 1,
        "observation_count": 1,
        "comparison_hint_count": 0,
        "deferral_count": 0,
        "job_status": "review_required",
        "job_route": "relational_extraction",
        "job_lease_present": False,
        "job_error_present": False,
        "evidence_status": "active",
        "exact_stage_batch_count": 0,
        "evidence_stage_batch_count": 0,
        "job_id": JOB_ID,
    }


class OpenAIPacketReviewTests(unittest.TestCase):
    def test_reviewer_uses_existing_forced_rls_packet_lane(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "memory_v1_openai_review_packet_v1.py"
        ).read_text(encoding="utf-8")
        self.assertIn("FROM memory.evidence_extraction_packet_v5 AS packet", source)
        self.assertIn("SELECT set_config('app.user_id',$1,true)", source)
        self.assertIn("plan_owner_v5_2_exact_packet_route_v1", source)
        self.assertNotIn("FROM memory.relational_stage_batch", source)
        self.assertNotIn("read_owner_v5_openai_packet_review_v1", source)

    def test_exact_route_preflight_is_required(self) -> None:
        validate_exact_route_preflight(
            [{"packet_id": PACKET_ID, "route": "manual_review_artifact_ready"}],
            packet_id=PACKET_ID,
        )
        for candidate in (
            [],
            [{"packet_id": uuid.uuid4(), "route": "manual_review_artifact_ready"}],
            [{"packet_id": PACKET_ID, "route": "already_routed"}],
        ):
            with self.assertRaisesRegex(
                OpenAIPacketReviewError, "not eligible for exact review routing"
            ):
                validate_exact_route_preflight(candidate, packet_id=PACKET_ID)

    def test_exact_one_external_call_is_required(self) -> None:
        self.assertTrue(
            openai_model_call_provenance_valid(
                local_model_calls=0, external_model_calls=1
            )
        )
        for local_calls, external_calls in ((1, 0), (0, 0), (0, 2), (1, 1)):
            self.assertFalse(
                openai_model_call_provenance_valid(
                    local_model_calls=local_calls,
                    external_model_calls=external_calls,
                )
            )

    def test_valid_openai_row_passes(self) -> None:
        value = packet()
        validate_openai_row(row(value), value)

    def test_cross_provider_packet_is_rejected(self) -> None:
        value = packet()
        candidate = row(value)
        candidate["provider_id"] = "local_llama_cpp"
        with self.assertRaisesRegex(OpenAIPacketReviewError, "approved OpenAI"):
            validate_openai_row(candidate, value)

    def test_owner_evidence_authority_mismatch_is_rejected(self) -> None:
        value = packet()
        candidate = row(value)
        candidate["evidence_authority_sha256"] = "a" * 64
        with self.assertRaisesRegex(OpenAIPacketReviewError, "authority hashes"):
            validate_openai_row(candidate, value)

    def test_preexisting_stage_is_rejected(self) -> None:
        value = packet()
        candidate = row(value)
        candidate["evidence_stage_batch_count"] = 1
        with self.assertRaisesRegex(OpenAIPacketReviewError, "already has"):
            validate_openai_row(candidate, value)

    def test_packet_hash_and_counts_are_bound(self) -> None:
        value = packet()
        candidate = row(value)
        candidate["observation_count"] = 2
        with self.assertRaisesRegex(OpenAIPacketReviewError, "row counts"):
            validate_openai_row(candidate, value)
        candidate = row(value)
        candidate["validator_packet_sha256"] = "b" * 64
        with self.assertRaisesRegex(OpenAIPacketReviewError, "normalized packet hash"):
            validate_openai_row(candidate, value)


if __name__ == "__main__":
    unittest.main()
