from __future__ import annotations

import asyncio
import json
from pathlib import Path
import unittest
from unittest import mock
import uuid

from scripts import memory_v1_openai_v5_2_packet_router_v1 as router


OWNER = uuid.UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
PACKET = uuid.UUID("8bf28952-67a5-4a11-8cab-718d451fca4c")


def artifact_pair() -> tuple[dict, dict]:
    counts = {
        state: int(state == "manual_review_required")
        for state in router.RESOLUTION_STATES
    }
    report = {
        "contract_version": router.REVIEW_CONTRACT,
        "mode": "owner_scoped_openai_packet_review_zero_write",
        "owner_user_id": str(OWNER),
        "packet_id": str(PACKET),
        "review_id": str(uuid.UUID("00000000-0000-4000-8000-000000000101")),
        "repository_commit": "a" * 40,
        "review_disposition": "manual_review_required",
        "provider_provenance": {
            "provider_id": "openai_responses",
            "provider_version": "v1",
        },
        "derived_packet_sha256": "b" * 64,
        "resolution_packet_sha256": "c" * 64,
        "resolution_summary": counts,
        "blocking_codes": [],
        "zero_write_proof": {
            "database_writes": 0,
            "qdrant_writes": 0,
            "external_model_calls": 0,
        },
    }
    bundle = {
        "contract_version": router.BUNDLE_CONTRACT,
        "mode": "preflight_only_zero_write",
        "owner_user_id": str(OWNER),
        "case_id": f"openai-packet-{PACKET}",
        "request_id": str(uuid.UUID("00000000-0000-4000-8000-000000000102")),
        "database_writes": 0,
        "qdrant_writes": 0,
        "external_model_calls": 0,
        "authorized_stage": False,
        "extraction_packet_sha256": "b" * 64,
        "resolution_packet_sha256": "c" * 64,
        "resolution_summary": counts,
    }
    return report, bundle


class OpenAIV52PacketRouterTests(unittest.TestCase):
    def test_review_artifacts_are_postgres_only_and_bound(self) -> None:
        report, bundle = artifact_pair()
        with mock.patch.object(router, "repository_commit_valid", return_value=True):
            result = router.validate_artifacts(
                report=report,
                bundle=bundle,
                owner=OWNER,
                packet_id=PACKET,
            )
        self.assertEqual(result["blocking_code_count"], 0)
        self.assertEqual(
            bundle["source_report"]["storage"],
            "memory.v5_2_openai_packet_route_event.review_report",
        )
        rendered = json.dumps(result, sort_keys=True, default=str)
        self.assertNotIn("/home/ubuntu/memory-v1-reviews", rendered)

    def test_mismatched_owner_and_unknown_counts_fail_closed(self) -> None:
        report, bundle = artifact_pair()
        report["owner_user_id"] = str(uuid.uuid4())
        with self.assertRaisesRegex(RuntimeError, "contract is invalid"):
            router.validate_artifacts(
                report=report,
                bundle=bundle,
                owner=OWNER,
                packet_id=PACKET,
            )
        report, bundle = artifact_pair()
        report["resolution_summary"]["unknown"] = 1
        with self.assertRaisesRegex(RuntimeError, "contract is invalid"):
            router.validate_artifacts(
                report=report,
                bundle=bundle,
                owner=OWNER,
                packet_id=PACKET,
            )

    def test_stable_ids_bind_owner_packet_route_and_artifacts(self) -> None:
        base = {
            "owner": OWNER,
            "packet_id": PACKET,
            "routing_basis_sha256": "1" * 64,
            "report_sha256": "2" * 64,
            "bundle_sha256": "3" * 64,
        }
        self.assertEqual(router.stable_ids(**base), router.stable_ids(**base))
        self.assertNotEqual(
            router.stable_ids(**base),
            router.stable_ids(**{**base, "bundle_sha256": "4" * 64}),
        )

    def test_postgres_jsonb_hashes_bind_the_stored_artifacts(self) -> None:
        report, bundle = artifact_pair()
        with mock.patch.object(router, "repository_commit_valid", return_value=True):
            artifact = router.validate_artifacts(
                report=report,
                bundle=bundle,
                owner=OWNER,
                packet_id=PACKET,
            )
        conn = mock.AsyncMock()
        conn.fetchval.side_effect = ["a" * 64, "b" * 64]
        result = asyncio.run(router.bind_postgres_artifact_hashes(conn, artifact))
        self.assertEqual(result["report_sha256"], "a" * 64)
        self.assertEqual(result["bundle_sha256"], "b" * 64)
        self.assertEqual(
            result["bundle"]["source_report"]["sha256"], "a" * 64
        )
        self.assertEqual(conn.fetchval.await_count, 2)
        for call in conn.fetchval.await_args_list:
            self.assertIn("$1::jsonb::text", call.args[0])

    def test_source_and_unit_have_no_filesystem_or_provider_authority(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "scripts/memory_v1_openai_v5_2_packet_router_v1.py").read_text(
            encoding="utf-8"
        )
        service = (
            root / "ops/systemd/memory-v1-openai-v5-2-packet-router.service"
        ).read_text(encoding="utf-8")
        self.assertNotIn("subprocess", source)
        self.assertNotIn("memory-v1-reviews", source)
        self.assertNotIn("openai.", source)
        self.assertNotIn("ReadWritePaths=", service)
        self.assertIn("ProtectHome=true", service)
        self.assertIn("IPAddressDeny=any", service)
        self.assertIn("--packet-id ${MEMORY_V1_OPENAI_PACKET_ID}", service)


if __name__ == "__main__":
    unittest.main()
