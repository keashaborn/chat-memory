from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import stat
import sys
import tempfile
import types
import unittest
from unittest import mock
import uuid


sys.modules.setdefault("asyncpg", types.ModuleType("asyncpg"))
scripts_package = types.ModuleType("scripts")
scripts_package.__path__ = []
disposition_module = types.ModuleType(
    "scripts.memory_v1_v5_local_packet_disposition"
)
disposition_module.canonical_owners = lambda values: [uuid.UUID(value) for value in values]
disposition_module.loopback_dsn = lambda value: value
disposition_module.sha256_text = lambda value: hashlib.sha256(
    value.encode("utf-8")
).hexdigest()
disposition_module.stable_json = lambda value: json.dumps(
    value, sort_keys=True, separators=(",", ":")
)
sys.modules.setdefault("scripts", scripts_package)
sys.modules.setdefault(
    "scripts.memory_v1_v5_local_packet_disposition",
    disposition_module,
)

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "memory_v1_v5_2_local_packet_router.py"
)
SPEC = importlib.util.spec_from_file_location("v5_2_router", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class V52LocalPacketRouterTest(unittest.TestCase):
    def test_terminal_allowlist_is_closed(self) -> None:
        self.assertEqual(
            MODULE.TERMINAL_REASON_CODES,
            {
                "structured_domain",
                "question_only",
                "transient_state",
                "insufficient_evidence",
            },
        )
        self.assertNotIn("ambiguous_transcription", MODULE.TERMINAL_REASON_CODES)

    def test_stable_ids_are_deterministic_and_route_bound(self) -> None:
        owner = uuid.UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
        packet = uuid.UUID("8bf28952-67a5-4a11-8cab-718d451fca4c")
        first = MODULE.stable_ids(
            owner=owner,
            packet_id=packet,
            routing_basis_sha256="1" * 64,
        )
        second = MODULE.stable_ids(
            owner=owner,
            packet_id=packet,
            routing_basis_sha256="1" * 64,
        )
        changed = MODULE.stable_ids(
            owner=owner,
            packet_id=packet,
            routing_basis_sha256="2" * 64,
        )
        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)

    @mock.patch.object(MODULE, "repository_commit_valid", return_value=True)
    def test_v5_2_artifact_pair_validates(self, _: mock.Mock) -> None:
        owner = uuid.UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
        packet = uuid.UUID("8bf28952-67a5-4a11-8cab-718d451fca4c")
        review_id = uuid.UUID("584aa45c-7483-4534-a388-a9ba4de0e0bd")
        request_id = uuid.UUID("df50c4f1-9d7f-46b5-80e6-c9e5719040d5")
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root).resolve()
            root.chmod(0o700)
            report_path = root / "review.json"
            bundle_path = root / "stage.json"
            report = {
                "contract_version": MODULE.REVIEW_CONTRACT,
                "mode": "owner_scoped_local_packet_review_zero_write",
                "owner_user_id": str(owner),
                "packet_id": str(packet),
                "review_id": str(review_id),
                "repository_commit": "a" * 40,
                "review_disposition": "manual_review_required",
                "derived_packet_sha256": "b" * 64,
                "resolution_packet_sha256": "c" * 64,
                "resolution_summary": {
                    "auto_link_eligible": 1,
                    "manual_review_required": 0,
                    "deferred": 0,
                    "rejected": 0,
                },
                "blocking_codes": [],
                "zero_write_proof": {
                    "database_writes": 0,
                    "qdrant_writes": 0,
                    "external_model_calls": 0,
                },
            }
            report_path.write_text(
                json.dumps(report, sort_keys=True) + "\n", encoding="utf-8"
            )
            report_path.chmod(0o600)
            report_sha = MODULE.sha256_file(report_path)
            bundle = {
                "contract_version": MODULE.BUNDLE_CONTRACT,
                "mode": "preflight_only_zero_write",
                "owner_user_id": str(owner),
                "case_id": f"local-packet-{packet}",
                "request_id": str(request_id),
                "database_writes": 0,
                "qdrant_writes": 0,
                "external_model_calls": 0,
                "authorized_stage": False,
                "source_report": {
                    "path": str(report_path),
                    "sha256": report_sha,
                },
                "extraction_packet_sha256": report["derived_packet_sha256"],
                "resolution_packet_sha256": report["resolution_packet_sha256"],
                "resolution_summary": report["resolution_summary"],
            }
            bundle_path.write_text(
                json.dumps(bundle, sort_keys=True) + "\n", encoding="utf-8"
            )
            bundle_path.chmod(0o600)
            self.assertEqual(
                stat.S_IMODE(report_path.stat().st_mode),
                0o600,
            )
            validated = MODULE.validate_artifacts(
                root=root,
                report_path=report_path,
                bundle_path=bundle_path,
                owner=owner,
                packet_id=packet,
            )
            self.assertEqual(validated["review_id"], review_id)
            self.assertEqual(validated["request_id"], request_id)
            self.assertEqual(validated["blocking_code_count"], 0)

    @mock.patch.object(MODULE, "repository_commit_valid", return_value=True)
    def test_legacy_contract_is_rejected(self, _: mock.Mock) -> None:
        owner = uuid.UUID("1240822d-ac9a-4096-95aa-e2b24d36ef50")
        packet = uuid.UUID("8bf28952-67a5-4a11-8cab-718d451fca4c")
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root).resolve()
            root.chmod(0o700)
            report_path = root / "review.json"
            bundle_path = root / "stage.json"
            report_path.write_text(
                json.dumps(
                    {
                        "contract_version": "memory_v1_v5_local_packet_review_v1"
                    }
                ),
                encoding="utf-8",
            )
            bundle_path.write_text("{}", encoding="utf-8")
            report_path.chmod(0o600)
            bundle_path.chmod(0o600)
            with self.assertRaisesRegex(
                RuntimeError, "V5.2 review report contract is invalid"
            ):
                MODULE.validate_artifacts(
                    root=root,
                    report_path=report_path,
                    bundle_path=bundle_path,
                    owner=owner,
                    packet_id=packet,
                )


if __name__ == "__main__":
    unittest.main()
