#!/usr/bin/env python3
"""Create an immutable bounded manifest from a reviewed V5.1 shadow report."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any


CONTRACT = "memory_v1_pattern_salience_snapshot_apply_manifest_v5_1"
REPORT_CONTRACT = "memory_v1_pattern_salience_shadow_report_v5_1"


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def secure_write(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    report = json.loads(args.report.read_text(encoding="utf-8"))
    if report.get("contract_version") != REPORT_CONTRACT:
        raise RuntimeError("shadow report contract mismatch")
    expected_report_hash = sha256_text(stable_json({
        key: value for key, value in report.items() if key != "report_sha256"
    }))
    if report.get("report_sha256") != expected_report_hash:
        raise RuntimeError("shadow report hash mismatch")
    if report.get("pattern_proposals") != []:
        raise RuntimeError("manifest generation requires zero pattern proposals")
    candidates = report.get("target_snapshot_candidates")
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 32:
        raise RuntimeError("candidate count is outside the bounded range")

    items: list[dict[str, Any]] = []
    targets: set[tuple[str, str, int]] = set()
    expected_links = 0
    for candidate in candidates:
        if set(candidate) != {"packet", "request_id"}:
            raise RuntimeError("candidate shape is not closed")
        packet = candidate["packet"]
        target = packet["target"]
        identity = (
            target["target_kind"], target["target_id"],
            target["target_revision_number"],
        )
        if identity in targets:
            raise RuntimeError("duplicate target in shadow report")
        targets.add(identity)
        expected_links += sum(
            len(packet["evidence_assessment"][key])
            for key in (
                "supporting_observation_ids", "opposing_observation_ids",
                "qualifying_observation_ids", "corrective_observation_ids",
            )
        )
        items.append({
            "request_id": candidate["request_id"],
            "packet_sha256": packet["packet_sha256"],
            "target_kind": target["target_kind"],
            "target_id": target["target_id"],
            "target_revision_number": target["target_revision_number"],
            "packet": packet,
        })

    count = len(items)
    manifest = {
        "contract_version": CONTRACT,
        "owner_user_id_sha256": report["owner_user_id_sha256"],
        "source_report_path": str(args.report),
        "source_report_file_sha256": sha256_file(args.report),
        "source_report_sha256": report["report_sha256"],
        "policy_version": report["policy_version"],
        "items": items,
        "expected_new_rows": {
            "epistemic_target_binding_v5_1": count,
            "epistemic_assessment_snapshot_v5_1": count,
            "epistemic_assessment_observation_link_v5_1": expected_links,
            "salience_feature_snapshot_v5_1": count,
            "epistemic_operation_request_v5_1": count,
            "retrieval_outcome_signal_v5_1": 0,
            "pattern_hypothesis_v5_1": 0,
            "pattern_hypothesis_revision_v5_1": 0,
            "pattern_observation_link_v5_1": 0,
            "pattern_review_v5_1": 0,
            "pattern_review_observation_v5_1": 0,
            "pattern_operation_request_v5_1": 0,
            "pattern_apply_event_v5_1": 0,
        },
        "write_budget": {
            "maximum_total_rows": count * 4 + expected_links,
            "other_owners": 0,
            "pattern_heads_or_revisions": 0,
            "retrieval_outcomes": 0,
            "qdrant": 0,
            "prompt_influence": 0,
        },
        "manifest_sha256": "",
    }
    manifest["manifest_sha256"] = sha256_text(stable_json({
        key: value for key, value in manifest.items() if key != "manifest_sha256"
    }))
    secure_write(args.output, manifest)
    print(stable_json({
        "outcome": "manifest_created",
        "manifest_sha256": manifest["manifest_sha256"],
        "candidate_count": count,
        "maximum_total_rows": manifest["write_budget"]["maximum_total_rows"],
    }))


if __name__ == "__main__":
    main()
