#!/usr/bin/env python3
"""Create deterministic synthetic local auto-stage clone fixtures."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any

from tests.memory_v1_v5_stage_batch_fixture import (
    fact_bundle,
    stable_json,
)


OWNER = "11111111-1111-4111-8111-111111111111"
PACKET_ID = "a3000000-0000-4000-8000-000000000001"
JOB_ID = "a3100000-0000-4000-8000-000000000001"
TERMINAL_ID = "a3200000-0000-4000-8000-000000000001"
ARTIFACT_ID = "a3300000-0000-4000-8000-000000000001"
ARTIFACT_OPERATION_ID = "a3400000-0000-4000-8000-000000000001"
REVIEW_ID = "a3500000-0000-4000-8000-000000000001"


def digest(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def write(path: Path, value: dict[str, Any]) -> str:
    raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
    return digest(raw)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review-root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    root = Path(args.review_root).resolve(strict=True)
    output = Path(args.output).resolve()
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    bundle = fact_bundle()
    extraction = json.loads(bundle["extraction_packet_text"])
    resolution = json.loads(bundle["resolution_packet_text"])
    extraction["source_envelope"]["job_id"] = JOB_ID
    resolution["source_envelope"]["job_id"] = JOB_ID
    resolution_body = {key: value for key, value in resolution.items() if key != "packet_sha256"}
    resolution["packet_sha256"] = digest(stable_json(resolution_body))
    extraction_text = stable_json(extraction)
    resolution_text = stable_json(resolution)
    bundle.update(
        {
            "extractor": "memory_v1_v5_local_packet_review",
            "extractor_version": head,
            "extraction_packet_text": extraction_text,
            "resolution_packet_text": resolution_text,
            "extraction_packet_sha256": digest(extraction_text),
            "resolution_packet_sha256": digest(resolution_text),
            "schemas": {
                "extraction_sha256": digest(
                    Path("specs/memory_v1_relational_extraction_v5.schema.json").read_bytes()
                ),
                "resolution_sha256": digest(
                    Path("specs/memory_v1_entity_resolution_review_v5.schema.json").read_bytes()
                ),
            },
        }
    )
    packet_hash = digest(PACKET_ID)
    report_path = root / f"local-router-{packet_hash}-review.json"
    bundle_path = root / f"local-router-{packet_hash}-stage.json"
    report = {
        "contract_version": "memory_v1_v5_local_packet_review_v1",
        "mode": "synthetic_clone_fixture",
        "owner_user_id": OWNER,
        "packet_id": PACKET_ID,
        "review_id": REVIEW_ID,
        "resolution_summary": bundle["resolution_summary"],
        "blocking_codes": [],
        "review_disposition": "manual_review_required",
    }
    report_sha = write(report_path, report)
    bundle["source_report"] = {"path": str(report_path), "sha256": report_sha}
    bundle_sha = write(bundle_path, bundle)
    metadata = {
        "owner_user_id": OWNER,
        "packet_id": PACKET_ID,
        "job_id": JOB_ID,
        "terminal_id": TERMINAL_ID,
        "artifact_id": ARTIFACT_ID,
        "artifact_operation_id": ARTIFACT_OPERATION_ID,
        "review_id": REVIEW_ID,
        "request_id": bundle["request_id"],
        "evidence_id": bundle["evidence_id"],
        "evidence_content_sha256": extraction["source_envelope"]["source_sha256"],
        "normalized_packet": extraction,
        "validator_packet_sha256": digest(stable_json(extraction)),
        "review_report_sha256": report_sha,
        "stage_bundle_sha256": bundle_sha,
        "repository_commit": head,
        "review_report": str(report_path),
        "stage_bundle": str(bundle_path),
    }
    write(output, metadata)
    print(
        stable_json(
            {
                "metadata": str(output),
                "review_report_sha256": report_sha,
                "stage_bundle_sha256": bundle_sha,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
